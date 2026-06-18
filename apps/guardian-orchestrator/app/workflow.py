from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from pathlib import Path
from typing import Any

import httpx

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
ENV_CONTEXT_TARGET_SAMPLES = 20
DETERMINISTIC_P3_EVENTS = {
    EventType.CO2_HIGH.value,
    EventType.TEMPERATURE_HIGH.value,
    EventType.TEMPERATURE_LOW.value,
    "humidity_abnormal",
}


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
        if self._is_deterministic_p3(event):
            await self._complete_deterministic_p3(workflow, event, saved_event, baseline)
            return
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
        manifest, contact_sheet, image_frames = await self._collect_frames(event)
        await self._record_step(
            workflow,
            event,
            "frame_collection",
            {"frame_set_id": event.frame_set_id},
            manifest or {"status": "not_applicable"},
        )

        sensors = await self.edge.get_recent_sensor_context(event.elder_id, limit=240)
        devices = await self.edge.get_home_device_snapshot(event.elder_id)
        context = self._build_local_context(event, saved_event, sensors, devices)
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
                image_frames=image_frames if risk_text(event.source_kind) == "VISION" and len(image_frames) >= 3 else [],
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
    ) -> tuple[dict[str, Any] | None, Path | None, list[tuple[int, Path]]]:
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
        contact_sheet_path = manifest.get("local_contact_sheet_path") or manifest["contact_sheet_path"]
        contact_sheet = root / contact_sheet_path
        image_frames = [
            (int(frame["offset_ms"]), root / frame["relative_path"])
            for frame in manifest.get("frames", [])
            if not frame.get("missing") and frame.get("relative_path") and frame.get("offset_ms") is not None
        ]
        return manifest, contact_sheet, image_frames

    @staticmethod
    def _higher_risk(first: str, second: str | None) -> str:
        if not second or second not in RISK_ORDER:
            return first
        return second if RISK_ORDER[second] > RISK_ORDER[first] else first

    @staticmethod
    def _is_deterministic_p3(event: NormalizedEventV2) -> bool:
        return risk_text(event.risk_level) == "P3" and str(event.event_type) in DETERMINISTIC_P3_EVENTS

    @staticmethod
    def _observation_time(observation: dict[str, Any]) -> datetime:
        value = observation.get("observed_at") or observation.get("created_at")
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                return datetime.min.replace(tzinfo=timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _payload(observation: dict[str, Any]) -> dict[str, Any]:
        payload = observation.get("payload")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _is_present(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "present"}
        return bool(value)

    @classmethod
    def _current_presence_room(cls, observations: list[dict[str, Any]]) -> dict[str, Any] | None:
        for observation in sorted(observations, key=cls._observation_time, reverse=True):
            payload = cls._payload(observation)
            room = payload.get("room")
            if not room:
                continue
            present = payload.get("present", payload.get("presence"))
            state = str(payload.get("state") or "").lower()
            if cls._is_present(present) or state == "present":
                return {
                    "current_room": str(room),
                    "source": payload.get("device") or observation.get("kind") or "presence",
                    "observed_at": observation.get("observed_at"),
                    "observation_id": observation.get("observation_id"),
                }
        return None

    @classmethod
    def _build_local_context(
        cls,
        event: NormalizedEventV2,
        saved_event: dict[str, Any],
        sensors: dict[str, Any],
        devices: dict[str, Any],
    ) -> dict[str, Any]:
        observations = sensors.get("observations", []) if isinstance(sensors, dict) else []
        observations = [item for item in observations if isinstance(item, dict)]
        observations_desc = sorted(observations, key=cls._observation_time, reverse=True)
        location = cls._current_presence_room(observations_desc)

        env_candidates = [
            item
            for item in observations_desc
            if str(item.get("kind") or "") == "environment" and cls._is_present(cls._payload(item).get("presence"))
        ]
        if not env_candidates and location:
            env_candidates = [
                item
                for item in observations_desc
                if str(item.get("kind") or "") == "environment"
                and str(cls._payload(item).get("room") or "") == location["current_room"]
            ]
        if not env_candidates:
            env_candidates = [item for item in observations_desc if str(item.get("kind") or "") == "environment"]

        selected_env_desc = env_candidates[:ENV_CONTEXT_TARGET_SAMPLES]
        selected_env = list(reversed(selected_env_desc))
        selected_rooms = [str(cls._payload(item).get("room")) for item in selected_env if cls._payload(item).get("room")]
        room_sequence = list(dict.fromkeys(selected_rooms))

        environment_samples = []
        for item in selected_env:
            payload = cls._payload(item)
            environment_samples.append(
                {
                    "observation_id": item.get("observation_id"),
                    "observed_at": item.get("observed_at"),
                    "room": payload.get("room"),
                    "temperature": payload.get("temperature"),
                    "humidity": payload.get("humidity"),
                    "co2_ppm": payload.get("co2_ppm"),
                    "gas_ppm": payload.get("gas_ppm"),
                    "smoke_ppm": payload.get("smoke_ppm"),
                    "illuminance_lux": payload.get("illuminance_lux"),
                    "presence": payload.get("presence"),
                    "snapshot_id": payload.get("snapshot_id"),
                }
            )

        trigger_ids = set(saved_event.get("trigger_observation_ids") or event.trigger_observation_ids or [])
        selected_ids = {str(item.get("observation_id")) for item in selected_env if item.get("observation_id")}
        selected_room_set = set(selected_rooms)
        local_observations: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in observations_desc:
            observation_id = str(item.get("observation_id") or "")
            payload = cls._payload(item)
            keep = False
            if observation_id and observation_id in trigger_ids:
                keep = True
            if observation_id and observation_id in selected_ids:
                keep = True
            if str(item.get("kind") or "") == "device_state" and payload.get("room") in selected_room_set:
                keep = True
            if keep and observation_id not in seen_ids:
                local_observations.append(item)
                seen_ids.add(observation_id)
        local_observations = sorted(local_observations, key=cls._observation_time)

        return {
            "sensors": {
                "elder_id": sensors.get("elder_id") if isinstance(sensors, dict) else event.elder_id,
                "observations": local_observations,
            },
            "devices": devices,
            "elder_location": location
            or {
                "current_room": event.room,
                "source": "event_room" if event.room else "unknown",
                "observed_at": None,
            },
            "environment_context": {
                "target_samples": ENV_CONTEXT_TARGET_SAMPLES,
                "actual_samples": len(environment_samples),
                "selection_policy": "presence_timeline_current_room_then_previous_rooms",
                "room_sequence": room_sequence,
                "samples": environment_samples,
            },
        }

    async def _complete_deterministic_p3(
        self,
        workflow: WorkflowV2,
        event: NormalizedEventV2,
        saved_event: dict[str, Any],
        baseline: dict[str, Any],
    ) -> None:
        rule_risk = risk_text(event.risk_level)
        skipped = {
            "status": "skipped",
            "reason": "deterministic_p3_rule",
            "event_semantics": event.summary,
            "risk_level": rule_risk,
            "confidence": event.confidence,
            "fallback": False,
        }
        await self._record_step(
            workflow,
            event,
            "local_multiframe_analysis",
            {"event": saved_event},
            skipped,
            status=StepStatus.SKIPPED,
            model=None,
        )
        await self._record_step(
            workflow,
            event,
            "local_policy_execution",
            skipped,
            {"status": "baseline_retained", "baseline": baseline},
        )
        cloud = {"status": "not_required", "reason": "deterministic_p3_rule"}
        await self._record_step(
            workflow,
            event,
            "cloud_review",
            skipped,
            cloud,
            status=StepStatus.SKIPPED,
            model=None,
        )
        await self.edge.update_event_analysis(
            event.event_id,
            {
                "local_risk_level": None,
                "local_semantics": None,
                "cloud_risk_level": None,
                "final_risk_level": rule_risk,
                "decision_source": "rule",
                "confidence": event.confidence,
                "summary": event.summary,
            },
        )
        await self._record_step(
            workflow,
            event,
            "final_advisory",
            {"local": skipped, "cloud": cloud},
            {
                "final_risk_level": rule_risk,
                "family_summary": event.summary,
                "decision_source": "rule",
            },
            model=None,
        )

    @staticmethod
    def _fallback_result(event: NormalizedEventV2, error: Exception | str) -> dict[str, Any]:
        fallback_type = "request_failed"
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 503:
            fallback_type = "service_unavailable"
        elif isinstance(error, (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError)):
            fallback_type = "timeout"
        elif getattr(error, "raw_model_content", None) is not None or getattr(error, "parsed_model_output", None) is not None:
            fallback_type = "safety_rejected"
        elif error.__class__.__name__ == "LLMOutputError":
            fallback_type = "safety_rejected"
        result = {
            "fallback": True,
            "fallback_type": fallback_type,
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
        *,
        status: StepStatus = StepStatus.COMPLETED,
        model: str | None = settings.llm_model,
    ) -> None:
        await self.edge.record_step(
            WorkflowStepV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                step_name=step_name,
                status=status,
                model=model,
                input=input_payload,
                output=output_payload,
                completed_at=datetime.now(timezone.utc),
            )
        )
