from __future__ import annotations

import asyncio
import logging
from typing import Any

from guardian_shared.enums import DeviceAction, DeviceType, EventType, RiskLevel
from guardian_shared.v2 import (
    ActionCommandV2,
    ActionRequestV2,
    AlertRequestV2,
    HmiPromptV2,
    NormalizedEventV2,
    StepStatus,
    WorkflowStatus,
    WorkflowStepV2,
    WorkflowV2,
)

from app.config import settings
from app.edge_client import EdgeClient
from app.llm_client import StepLLMClient


logger = logging.getLogger(__name__)


class WorkflowRunner:
    def __init__(self) -> None:
        self.edge = EdgeClient()
        self.llm = StepLLMClient()
        self.llm_semaphore = asyncio.Semaphore(settings.llm_max_concurrent_workflows)

    async def run(self, event: NormalizedEventV2) -> dict[str, Any]:
        saved_event = await self.edge.create_event(event)
        workflow = WorkflowV2(
            event_id=event.event_id,
            elder_id=event.elder_id,
            status=WorkflowStatus.RUNNING,
            current_step="rule_gate",
            model=settings.llm_model,
        )
        await self.edge.create_workflow(workflow)
        await self._record_step(workflow, event, "rule_gate", {"event": saved_event}, {"accepted": True})

        if event.risk_level == RiskLevel.P0:
            execution = await self._execute_p0(workflow, event)
            return {"event": saved_event, "workflow_id": workflow.workflow_id, "execution": execution}

        baseline_execution = await self._execute_non_p0(
            workflow,
            event,
            {
                "summary": event.summary,
                "source": "rule_first_baseline",
                "reason": "Qwen3.5-4B 延迟和结构化输出稳定性不足，非 P0 先按规则/HMI 完成安全闭环。",
            },
        )
        self._schedule_llm_chain(workflow, event, saved_event, baseline_execution)
        return {
            "event": saved_event,
            "workflow_id": workflow.workflow_id,
            "execution": baseline_execution,
            "llm_chain": {"status": "queued"},
        }

    def _schedule_llm_chain(
        self,
        workflow: WorkflowV2,
        event: NormalizedEventV2,
        saved_event: dict[str, Any],
        baseline_execution: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(self._run_llm_chain(workflow, event, saved_event, baseline_execution))
        task.add_done_callback(self._log_background_task_result)

    def _log_background_task_result(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("LLM chain task cancelled")
        except Exception:
            logger.exception("LLM chain task failed")

    async def _run_llm_chain(
        self,
        workflow: WorkflowV2,
        event: NormalizedEventV2,
        saved_event: dict[str, Any],
        baseline_execution: dict[str, Any],
    ) -> None:
        async with self.llm_semaphore:
            try:
                await asyncio.wait_for(
                    self._run_llm_chain_inner(workflow, event, saved_event, baseline_execution),
                    timeout=settings.llm_chain_timeout_sec,
                )
            except TimeoutError:
                await self._record_step(
                    workflow,
                    event,
                    "llm_chain_timeout",
                    {"event": saved_event},
                    {
                        "fallback": True,
                        "summary": f"LLM 多步分析链超过 {settings.llm_chain_timeout_sec} 秒，安全闭环已由规则完成。",
                        "error": "llm_chain_timeout",
                    },
                )

    async def _run_llm_chain_inner(
        self,
        workflow: WorkflowV2,
        event: NormalizedEventV2,
        saved_event: dict[str, Any],
        baseline_execution: dict[str, Any],
    ) -> None:
        context = await self.edge.get_recent_sensor_context(event.elder_id, limit=8)
        devices = await self.edge.get_home_device_snapshot(event.elder_id)
        context_output = await self._llm_step(
            step_name="context_fetch_conversation",
            instruction="只整理当前事件需要的传感器、历史和设备上下文，不做风险决策，不生成动作。",
            payload={"event": saved_event, "context": context, "devices": devices},
        )
        await self._record_step(workflow, event, "context_fetch_conversation", {"event": saved_event}, context_output)

        fusion_output = await self._llm_step(
            step_name="sensor_fusion_conversation",
            instruction="把当前事件和上下文整理为事实摘要，只列出支持/矛盾/缺失信息，不生成动作。",
            payload={"event": saved_event, "context": context_output},
        )
        await self._record_step(workflow, event, "sensor_fusion_conversation", context_output, fusion_output)

        decision_output = await self._llm_step(
            step_name="risk_decision_conversation",
            instruction="只做风险复盘和后续观察建议。不得降低规则风险等级，不得输出设备控制命令或 MQTT 指令。",
            payload={"event": saved_event, "fusion": fusion_output, "baseline_execution": baseline_execution},
        )
        await self._record_step(workflow, event, "risk_decision_conversation", fusion_output, decision_output)

        advisory_output = await self._llm_step(
            step_name="advisory_conversation",
            instruction=(
                "只基于已完成的规则处置生成复盘解释、家属可读摘要和后续观察建议。"
                "不得修改风险等级，不得新增设备控制命令，不得取消已触发的 HMI 或告警。"
            ),
            payload={"event": saved_event, "decision": decision_output, "baseline_execution": baseline_execution},
        )
        await self._record_step(
            workflow,
            event,
            "advisory_conversation",
            {"event": saved_event, "decision": decision_output, "baseline_execution": baseline_execution},
            advisory_output,
        )

    async def _execute_p0(self, workflow: WorkflowV2, event: NormalizedEventV2) -> dict[str, Any]:
        commands: list[ActionCommandV2] = []
        if str(event.event_type) == EventType.GAS_LEAK.value:
            commands = [
                ActionCommandV2(room="living_room", device=DeviceType.WINDOW, action=DeviceAction.OPEN, reason="燃气泄漏，打开窗户。"),
                ActionCommandV2(room="kitchen", device=DeviceType.GAS_VALVE, action=DeviceAction.CLOSE, reason="燃气泄漏，关闭燃气阀门。"),
                ActionCommandV2(room="local", device=DeviceType.LOCAL_ALARM, action=DeviceAction.ALARM_ON, reason="燃气泄漏，启动本地报警。"),
            ]
        else:
            commands = [
                ActionCommandV2(room="local", device=DeviceType.LOCAL_ALARM, action=DeviceAction.ALARM_ON, reason="P0 紧急风险，启动本地报警。")
            ]
        action_result = await self.edge.request_home_action(
            ActionRequestV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                commands=commands,
                reason=event.summary,
                priority=RiskLevel.P0,
            )
        )
        alert_result = await self.edge.raise_family_alert(
            AlertRequestV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                alert_level=RiskLevel.P0,
                message=f"紧急告警：{event.summary}",
            )
        )
        await self._record_step(workflow, event, "action_request_conversation", {"event": event.model_dump(mode="json")}, {"action": action_result, "alert": alert_result})
        return {"action": action_result, "alert": alert_result}

    async def _llm_step(self, *, step_name: str, instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.llm.run_step(step_name=step_name, instruction=instruction, payload=payload)
        except Exception as exc:
            return {
                "step_name": step_name,
                "fallback": True,
                "error": str(exc),
                "summary": f"{step_name} 失败，按规则保守降级继续闭环。",
            }

    async def _execute_non_p0(self, workflow: WorkflowV2, event: NormalizedEventV2, decision: dict[str, Any]) -> dict[str, Any]:
        if event.risk_level == RiskLevel.P3 and str(event.event_type) == EventType.CO2_HIGH.value:
            request = ActionRequestV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                commands=[
                    ActionCommandV2(room=event.room or "living_room", device=DeviceType.WINDOW, action=DeviceAction.OPEN, reason="CO2 偏高，自动通风。")
                ],
                reason=event.summary,
                priority=RiskLevel.P3,
            )
            result = await self.edge.request_home_action(request)
            await self._record_step(workflow, event, "action_request_conversation", decision, result)
            return result
        if event.risk_level == RiskLevel.P3 and str(event.event_type) in {EventType.TEMPERATURE_HIGH.value, EventType.TEMPERATURE_LOW.value}:
            target = 26 if str(event.event_type) == EventType.TEMPERATURE_HIGH.value else 24
            request = ActionRequestV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                commands=[
                    ActionCommandV2(
                        room=event.room or "living_room",
                        device=DeviceType.AIR_CONDITIONER,
                        action=DeviceAction.SET_TEMPERATURE,
                        value=target,
                        reason="室温异常，自动调整空调目标温度。",
                    )
                ],
                reason=event.summary,
                priority=RiskLevel.P3,
            )
            result = await self.edge.request_home_action(request)
            await self._record_step(workflow, event, "action_request_conversation", decision, result)
            return result
        if event.risk_level in {RiskLevel.P1, RiskLevel.P2}:
            prompt = await self.edge.create_hmi_prompt(
                HmiPromptV2(
                    workflow_id=workflow.workflow_id,
                    event_id=event.event_id,
                    elder_id=event.elder_id,
                    risk_level=event.risk_level,
                    event_type=str(event.event_type),
                    message=f"{event.summary} 您现在安全吗？",
                    timeout_sec=30,
                )
            )
            alert = None
            if event.risk_level == RiskLevel.P1:
                alert = await self.edge.raise_family_alert(
                    AlertRequestV2(
                        workflow_id=workflow.workflow_id,
                        event_id=event.event_id,
                        elder_id=event.elder_id,
                        alert_level=RiskLevel.P1,
                        message=f"高风险事件已本地询问老人：{event.summary}",
                    )
                )
            result = {"status": "waiting_hmi", "prompt": prompt, "alert": alert}
            await self._record_step(workflow, event, "hmi_followup", decision, result)
            return result
        result = {"status": "record_only", "message": "无需自动动作。"}
        await self._record_step(workflow, event, "action_request_conversation", decision, result)
        return result

    async def _record_step(
        self,
        workflow: WorkflowV2,
        event: NormalizedEventV2,
        step_name: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
    ) -> None:
        await self.edge.record_step(
            WorkflowStepV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                step_name=step_name,
                status=StepStatus.COMPLETED,
                model=settings.llm_model,
                input=input_payload,
                output=output_payload,
            )
        )
