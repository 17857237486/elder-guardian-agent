from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from pathlib import Path
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
from app.llm_client import CloudLLMClient, LocalMultimodalClient


logger = logging.getLogger(__name__)
RISK_ORDER = {"P4": 0, "P3": 1, "P2": 2, "P1": 3, "P0": 4}


def risk_text(value: Any) -> str:
    return str(value).split(".")[-1].upper()


class WorkflowRunner:
    def __init__(self) -> None:
        self.edge = EdgeClient()
        self.local_llm = LocalMultimodalClient()
        self.cloud_llm = CloudLLMClient()
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

        if risk_text(event.risk_level) == "P0":
            baseline = await self._execute_p0(workflow, event)
        else:
            baseline = await self._execute_policy(workflow, event, {"source": "rule_first_baseline"})

        task = asyncio.create_task(self._run_analysis(workflow, event, saved_event, baseline))
        task.add_done_callback(self._log_background_task_result)
        return {
            "event": saved_event,
            "workflow_id": workflow.workflow_id,
            "execution": baseline,
            "analysis": {"status": "queued"},
        }

    def _log_background_task_result(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("analysis task cancelled")
        except Exception:
            logger.exception("three-level analysis failed")

    async def _run_analysis(
        self,
        workflow: WorkflowV2,
        event: NormalizedEventV2,
        saved_event: dict[str, Any],
        baseline: dict[str, Any],
    ) -> None:
        async with self.llm_semaphore:
            try:
                await asyncio.wait_for(
                    self._run_analysis_inner(workflow, event, saved_event, baseline),
                    timeout=settings.llm_chain_timeout_sec,
                )
            except TimeoutError:
                await self._record_step(
                    workflow,
                    event,
                    "analysis_timeout",
                    {"event": saved_event},
                    {"fallback": True, "final_risk_level": risk_text(event.risk_level)},
                )

    async def _run_analysis_inner(
        self,
        workflow: WorkflowV2,
        event: NormalizedEventV2,
        saved_event: dict[str, Any],
        baseline: dict[str, Any],
    ) -> None:
        manifest, contact_sheet, image_paths = await self._collect_frames(event)
        await self._record_step(
            workflow,
            event,
            "frame_collection",
            {"frame_set_id": event.frame_set_id},
            manifest or {"status": "not_applicable"},
        )

        sensors = await self.edge.get_recent_sensor_context(event.elder_id, limit=30)
        devices = await self.edge.get_home_device_snapshot(event.elder_id)
        context = {"sensors": sensors, "devices": devices}
        await self._record_step(workflow, event, "local_context_fusion", {"event": saved_event}, context)

        local_started = perf_counter()
        try:
            local = await self.local_llm.analyze(event=saved_event, context=context, contact_sheet=contact_sheet)
        except Exception as exc:
            local = self._fallback_result(event, exc)
        local["latency_ms"] = round((perf_counter() - local_started) * 1000, 1)
        await self._record_step(
            workflow,
            event,
            "local_multiframe_analysis",
            {"event": saved_event, "manifest": manifest},
            local,
        )

        rule_risk = risk_text(event.risk_level)
        local_risk = self._higher_risk(rule_risk, risk_text(local.get("risk_level", rule_risk)))
        local_execution: dict[str, Any] = {"status": "baseline_retained", "baseline": baseline}
        if RISK_ORDER[local_risk] > RISK_ORDER[rule_risk]:
            escalated = event.model_copy(
                update={"risk_level": local_risk, "summary": local.get("event_semantics", event.summary)}
            )
            local_execution = await self._execute_policy(workflow, escalated, local)
        await self._record_step(workflow, event, "local_policy_execution", local, local_execution)

        cloud: dict[str, Any] = {"status": "not_required"}
        if local_risk in {"P0", "P1", "P2"}:
            cloud_started = perf_counter()
            cloud = await self.cloud_llm.review(
                event={**saved_event, "risk_level": local_risk},
                local_result=local,
                context=context,
                image_paths=image_paths if risk_text(event.source_kind) == "VISION" and len(image_paths) >= 3 else [],
            )
            cloud["latency_ms"] = round((perf_counter() - cloud_started) * 1000, 1)
        await self._record_step(workflow, event, "cloud_review", local, cloud)

        cloud_risk = risk_text(cloud["risk_level"]) if cloud.get("status") == "completed" else None
        final_risk = self._higher_risk(local_risk, cloud_risk) if cloud_risk else local_risk
        if cloud_risk and RISK_ORDER[final_risk] > RISK_ORDER[local_risk]:
            escalated = event.model_copy(
                update={"risk_level": final_risk, "summary": cloud.get("event_semantics", event.summary)}
            )
            await self._execute_policy(workflow, escalated, cloud)

        image_refs = manifest.get("image_refs", []) if manifest else []
        summary = cloud.get("family_summary") or local.get("family_summary") or event.summary
        await self.edge.update_event_analysis(
            event.event_id,
            {
                "local_risk_level": local_risk,
                "local_semantics": local.get("event_semantics"),
                "cloud_risk_level": cloud_risk,
                "final_risk_level": final_risk,
                "decision_source": (
                    "cloud" if cloud_risk and final_risk == cloud_risk else "local" if local_risk != rule_risk else "rule"
                ),
                "confidence": max(event.confidence, float(local.get("confidence", 0))),
                "image_refs": image_refs,
                "frame_set_id": event.frame_set_id,
                "summary": summary,
            },
        )
        await self._record_step(
            workflow,
            event,
            "final_advisory",
            {"local": local, "cloud": cloud},
            {"final_risk_level": final_risk, "family_summary": summary},
        )

    async def _collect_frames(
        self, event: NormalizedEventV2
    ) -> tuple[dict[str, Any] | None, Path | None, list[Path]]:
        if risk_text(event.source_kind) != "VISION" or not event.frame_set_id:
            return None, None, []
        root = Path(settings.snapshot_root)
        deadline = asyncio.get_running_loop().time() + settings.vision_frame_wait_sec
        manifest_path: Path | None = None
        while asyncio.get_running_loop().time() < deadline:
            matches = list(root.glob(f"*/*/*/*/{event.frame_set_id}/manifest.json"))
            if matches:
                manifest_path = matches[0]
                break
            await asyncio.sleep(0.25)
        if manifest_path is None:
            return {"frame_set_id": event.frame_set_id, "status": "timeout", "frames": []}, None, []
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        contact_sheet = root / manifest["contact_sheet_path"]
        image_paths = [
            root / frame["relative_path"]
            for frame in manifest.get("frames", [])
            if not frame.get("missing") and frame.get("relative_path")
        ]
        return manifest, contact_sheet, image_paths

    @staticmethod
    def _higher_risk(first: str, second: str | None) -> str:
        if not second or second not in RISK_ORDER:
            return first
        return second if RISK_ORDER[second] > RISK_ORDER[first] else first

    @staticmethod
    def _fallback_result(event: NormalizedEventV2, error: Exception | str) -> dict[str, Any]:
        result = {
            "fallback": True,
            "error": str(error),
            "event_semantics": event.summary,
            "risk_level": risk_text(event.risk_level),
            "confidence": event.confidence,
            "temporal_changes": [],
            "supporting_evidence": [],
            "contradictions": [],
            "missing_information": ["local model unavailable"],
            "recommended_followup": [],
            "family_summary": event.summary,
        }
        raw_content = getattr(error, "raw_model_content", None)
        parsed_output = getattr(error, "parsed_model_output", None)
        if raw_content is not None:
            result["rejected_model_content"] = raw_content
        if parsed_output is not None:
            result["rejected_model_output"] = parsed_output
        return result

    async def _execute_p0(self, workflow: WorkflowV2, event: NormalizedEventV2) -> dict[str, Any]:
        if str(event.event_type) == EventType.GAS_LEAK.value:
            commands = [
                ActionCommandV2(room="living_room", device=DeviceType.WINDOW, action=DeviceAction.OPEN, reason="燃气泄漏，打开窗户。"),
                ActionCommandV2(room="kitchen", device=DeviceType.GAS_VALVE, action=DeviceAction.CLOSE, reason="燃气泄漏，关闭燃气阀。"),
                ActionCommandV2(room="local", device=DeviceType.LOCAL_ALARM, action=DeviceAction.ALARM_ON, reason="燃气泄漏，启动本地报警。"),
            ]
        else:
            commands = [
                ActionCommandV2(room="local", device=DeviceType.LOCAL_ALARM, action=DeviceAction.ALARM_ON, reason="P0 紧急风险。")
            ]
        action = await self.edge.request_home_action(
            ActionRequestV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                commands=commands,
                reason=event.summary,
                priority=RiskLevel.P0,
            )
        )
        alert = await self.edge.raise_family_alert(
            AlertRequestV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                alert_level=RiskLevel.P0,
                message=f"紧急告警：{event.summary}",
            )
        )
        return {"action": action, "alert": alert}

    async def _execute_policy(
        self, workflow: WorkflowV2, event: NormalizedEventV2, decision: dict[str, Any]
    ) -> dict[str, Any]:
        risk = risk_text(event.risk_level)
        if risk == "P0":
            return await self._execute_p0(workflow, event)
        if risk == "P3" and str(event.event_type) == EventType.CO2_HIGH.value:
            request = ActionRequestV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                commands=[ActionCommandV2(room=event.room or "living_room", device=DeviceType.WINDOW, action=DeviceAction.OPEN, reason="CO2 偏高，自动通风。")],
                reason=event.summary,
                priority=RiskLevel.P3,
            )
            return await self.edge.request_home_action(request)
        if risk == "P3" and str(event.event_type) in {EventType.TEMPERATURE_HIGH.value, EventType.TEMPERATURE_LOW.value}:
            target = 26 if str(event.event_type) == EventType.TEMPERATURE_HIGH.value else 24
            request = ActionRequestV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                commands=[ActionCommandV2(room=event.room or "living_room", device=DeviceType.AIR_CONDITIONER, action=DeviceAction.SET_TEMPERATURE, value=target, reason="室温异常。")],
                reason=event.summary,
                priority=RiskLevel.P3,
            )
            return await self.edge.request_home_action(request)
        if risk in {"P1", "P2"}:
            prompt = await self.edge.create_hmi_prompt(
                HmiPromptV2(
                    workflow_id=workflow.workflow_id,
                    event_id=event.event_id,
                    elder_id=event.elder_id,
                    risk_level=risk,
                    event_type=str(event.event_type),
                    message=f"{event.summary} 您现在安全吗？",
                    timeout_sec=30,
                )
            )
            alert = None
            if risk == "P1":
                alert = await self.edge.raise_family_alert(
                    AlertRequestV2(
                        workflow_id=workflow.workflow_id,
                        event_id=event.event_id,
                        elder_id=event.elder_id,
                        alert_level=RiskLevel.P1,
                        message=f"高风险事件：{event.summary}",
                    )
                )
            return {"status": "waiting_hmi", "prompt": prompt, "alert": alert}
        return {"status": "record_only", "decision": decision}

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
                completed_at=datetime.now(timezone.utc),
            )
        )
