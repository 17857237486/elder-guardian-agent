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
from guardian_shared.schemas import new_id
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
ENV_CONTEXT_TARGET_SAMPLES = 30
VITAL_CONTEXT_TARGET_SAMPLES = 30
DETERMINISTIC_P3_EVENTS = {
    EventType.CO2_HIGH.value,
    EventType.TEMPERATURE_HIGH.value,
    EventType.TEMPERATURE_LOW.value,
    "humidity_abnormal",
}
DETERMINISTIC_VITAL_EVENTS = {
    EventType.HEART_RATE_ABNORMAL.value,
    EventType.SPO2_LOW.value,
}
HMI_OPTIONS = ["我没事", "需要帮助", "联系家属"]


def risk_text(value: Any) -> str:
    return str(value).split(".")[-1].upper()


class WorkflowRunner:
    def __init__(self) -> None:
        self.edge = EdgeClient()
        self.local_llm = LocalMultimodalClient()
        self.cloud_llm = CloudLLMClient()
        self.llm_semaphore = asyncio.Semaphore(settings.llm_max_concurrent_workflows)
        self.local_model_lock = asyncio.Lock()

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

        if self._defer_rule_policy_until_local_review(event):
            baseline = {"status": "deferred_until_local_review", "reason": "long_static_local_review_can_downgrade"}
        elif risk_text(event.risk_level) == "P0":
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
        if self._is_deterministic_vital(event):
            await self._complete_deterministic_vital(workflow, event, saved_event, baseline)
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
        segments, baselines = await self._get_behavior_context_sources(event.elder_id)
        context = self._build_local_context(event, saved_event, sensors, devices, segments=segments, baselines=baselines)
        await self._record_step(workflow, event, "local_context_fusion", {"event": saved_event}, context)

        local_started = perf_counter()
        try:
            async with self.local_model_lock:
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
        local_risk = self._resolve_local_risk(event, rule_risk, risk_text(local.get("risk_level", rule_risk)), local)
        local_execution: dict[str, Any] = {"status": "baseline_retained", "baseline": baseline}
        if baseline.get("status") == "deferred_until_local_review":
            if local.get("fallback"):
                local_execution = await self._execute_policy(workflow, event, local)
            elif local_risk == "P4":
                local_execution = {
                    "status": "downgraded_record_only",
                    "reason": "long_static_local_model_low_risk",
                    "baseline": baseline,
                }
            else:
                reviewed_event = event.model_copy(
                    update={"risk_level": local_risk, "summary": local.get("event_semantics", event.summary)}
                )
                local_execution = await self._execute_policy(workflow, reviewed_event, local)
        elif RISK_ORDER[local_risk] > RISK_ORDER[rule_risk]:
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

    async def run_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(candidate.get("candidate_id") or new_id("cand"))
        elder_id = str(candidate.get("elder_id") or settings.elder_id)
        event = NormalizedEventV2(
            event_id=candidate_id,
            elder_id=elder_id,
            event_type=str(candidate.get("candidate_type") or "ai_review_candidate"),
            risk_level=RiskLevel.P4,
            summary=str(candidate.get("reason") or "AI review candidate"),
            source_kind="ai_review_candidate",
            evidence=[{"candidate": candidate}],
            rule_risk_level=RiskLevel.P4,
            local_risk_level=RiskLevel.P4,
            final_risk_level=RiskLevel.P4,
            decision_source="candidate",
            confidence=0.0,
        )
        workflow = WorkflowV2(
            event_id=candidate_id,
            elder_id=elder_id,
            status=WorkflowStatus.RUNNING,
            current_step="candidate_received",
            model=settings.llm_model,
        )
        await self.edge.create_workflow(workflow)
        await self._record_step(workflow, event, "candidate_received", {"candidate": candidate}, {"accepted": True})

        await self.edge.update_ai_review_candidate(
            candidate_id,
            {"status": "reviewing", "reviewed_at": datetime.now(timezone.utc).isoformat()},
        )

        sensors = await self.edge.get_recent_sensor_context(elder_id, limit=40)
        segments, baselines = await self._get_behavior_context_sources(elder_id)
        saved_event = {
            "event_id": candidate_id,
            "event_type": event.event_type,
            "risk_level": "P4",
            "summary": event.summary,
            "source_kind": "ai_review_candidate",
            "candidate": candidate,
        }
        context = self._build_candidate_context(event, candidate, sensors, segments=segments, baselines=baselines)
        await self._record_step(workflow, event, "local_context_fusion", {"candidate": candidate}, context)

        queue_started = perf_counter()
        started = queue_started
        queue_wait_ms = 0.0
        try:
            async with self.local_model_lock:
                queue_wait_ms = round((perf_counter() - queue_started) * 1000, 1)
                started = perf_counter()
                local = await self._analyze_candidate_with_busy_retry(saved_event, context)
        except Exception as exc:
            if queue_wait_ms == 0.0:
                queue_wait_ms = round((perf_counter() - queue_started) * 1000, 1)
            local = self._fallback_result(event, exc)
        local["latency_ms"] = round((perf_counter() - started) * 1000, 1)
        local["queue_wait_ms"] = queue_wait_ms
        await self._record_step(workflow, event, "local_multiframe_analysis", {"candidate": candidate}, local)

        local_risk = risk_text(local.get("risk_level", "P4"))
        if local.get("fallback"):
            status = "failed"
            promoted_event_id = None
            output = {"status": status, "reason": local.get("error"), "risk_level": local_risk}
        elif local_risk in {"P4", "P3"}:
            status = "dismissed"
            promoted_event_id = None
            output = {"status": status, "reason": "local_model_low_risk", "risk_level": local_risk}
        else:
            promoted = NormalizedEventV2(
                elder_id=elder_id,
                event_type=str(candidate.get("candidate_type") or "ai_review_candidate"),
                risk_level=local_risk,
                summary=local.get("family_summary") or local.get("event_semantics") or event.summary,
                source_kind="ai_review_candidate",
                evidence=[{"candidate": candidate, "local_result": local}],
                rule_risk_level=RiskLevel.P4,
                local_risk_level=local_risk,
                final_risk_level=local_risk,
                decision_source="local",
                confidence=float(local.get("confidence") or 0.0),
            )
            saved = await self.edge.create_event(promoted)
            await self._execute_policy(workflow, promoted, local)
            status = "promoted"
            promoted_event_id = str(saved.get("event_id") or promoted.event_id)
            output = {"status": status, "promoted_event_id": promoted_event_id, "risk_level": local_risk}

        await self.edge.update_ai_review_candidate(
            candidate_id,
            {
                "status": status,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "promoted_event_id": promoted_event_id,
                "features": {**(candidate.get("features") if isinstance(candidate.get("features"), dict) else {}), "local_result": local},
            },
        )
        await self._record_step(workflow, event, "candidate_decision", local, output)
        return {"candidate_id": candidate_id, "workflow_id": workflow.workflow_id, **output}

    async def _analyze_candidate_with_busy_retry(self, event: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        retry_delays = [1.5, 3.0]
        for attempt in range(len(retry_delays) + 1):
            try:
                return await self.local_llm.analyze(event=event, context=context, contact_sheet=None)
            except httpx.HTTPStatusError as exc:
                if not self._is_local_model_busy(exc) or attempt >= len(retry_delays):
                    raise
                await asyncio.sleep(retry_delays[attempt])
        raise RuntimeError("candidate local model retry exhausted")

    @staticmethod
    def _is_local_model_busy(error: httpx.HTTPStatusError) -> bool:
        response = error.response
        if response.status_code != 503:
            return False
        text = response.text.lower()
        return "busy" in text and "worker" in text

    async def _get_behavior_context_sources(self, elder_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            segments, baselines = await asyncio.gather(
                self.edge.get_behavior_segments(elder_id, limit=50),
                self.edge.get_personal_baselines(elder_id),
            )
            return segments, baselines
        except Exception:
            logger.exception("failed to fetch behavior context sources")
            return [], []

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
    def _defer_rule_policy_until_local_review(event: NormalizedEventV2) -> bool:
        return str(event.event_type) == EventType.LONG_STATIC.value and risk_text(event.risk_level) == "P2"

    @staticmethod
    def _resolve_local_risk(event: NormalizedEventV2, rule_risk: str, reviewed_risk: str, local: dict[str, Any]) -> str:
        if (
            str(event.event_type) == EventType.LONG_STATIC.value
            and rule_risk == "P2"
            and reviewed_risk == "P4"
            and not local.get("fallback")
        ):
            return "P4"
        return WorkflowRunner._higher_risk(rule_risk, reviewed_risk)

    @staticmethod
    def _is_deterministic_p3(event: NormalizedEventV2) -> bool:
        return risk_text(event.risk_level) == "P3" and str(event.event_type) in DETERMINISTIC_P3_EVENTS

    @staticmethod
    def _is_deterministic_vital(event: NormalizedEventV2) -> bool:
        return (
            risk_text(event.risk_level) in {"P0", "P1"}
            and str(event.source_kind) == "vital"
            and str(event.event_type) in DETERMINISTIC_VITAL_EVENTS
        )

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
        *,
        segments: list[dict[str, Any]] | None = None,
        baselines: list[dict[str, Any]] | None = None,
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

        vital_candidates = [
            item
            for item in observations_desc
            if str(item.get("kind") or "") == "vital"
        ][:VITAL_CONTEXT_TARGET_SAMPLES]
        recent_vital_samples = []
        for item in reversed(vital_candidates):
            payload = cls._payload(item)
            recent_vital_samples.append(
                {
                    "observation_id": item.get("observation_id"),
                    "observed_at": item.get("observed_at"),
                    "heart_rate": payload.get("heart_rate"),
                    "spo2": payload.get("spo2"),
                    "systolic_bp": payload.get("systolic_bp"),
                    "diastolic_bp": payload.get("diastolic_bp"),
                    "body_temperature": payload.get("body_temperature"),
                    "room": payload.get("room"),
                }
            )

        trigger_ids = set(saved_event.get("trigger_observation_ids") or event.trigger_observation_ids or [])
        selected_ids = {str(item.get("observation_id")) for item in selected_env if item.get("observation_id")}
        vital_ids = {str(item.get("observation_id")) for item in vital_candidates if item.get("observation_id")}
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
            if observation_id and observation_id in vital_ids:
                keep = True
            if str(item.get("kind") or "") == "device_state" and payload.get("room") in selected_room_set:
                keep = True
            if keep and observation_id not in seen_ids:
                local_observations.append(item)
                seen_ids.add(observation_id)
        local_observations = sorted(local_observations, key=cls._observation_time)

        behavior_segments = segments or []
        personal_baselines = baselines or []
        recent_segments = sorted(behavior_segments, key=lambda item: str(item.get("start_at") or ""), reverse=True)[:20]
        baseline_context = {
            str(item.get("baseline_type")): item
            for item in personal_baselines
            if isinstance(item, dict) and item.get("baseline_type")
        }

        result = {
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
            "recent_vital_samples": {
                "target_samples": VITAL_CONTEXT_TARGET_SAMPLES,
                "actual_samples": len(recent_vital_samples),
                "samples": recent_vital_samples,
            },
            "behavior_context": {
                "recent_segments": recent_segments,
                "night_wake": next((item for item in recent_segments if item.get("segment_type") == "night_wake"), None),
                "bathroom_stay": next((item for item in recent_segments if item.get("segment_type") == "bathroom_stay"), None),
                "room_sequence": room_sequence,
            },
            "baseline_context": baseline_context,
        }
        if risk_text(event.source_kind) == "VISION":
            result["vision_context"] = {
                "frame_set_id": event.frame_set_id,
                "event_type": event.event_type,
                "risk_level": risk_text(event.risk_level),
                "summary": event.summary,
                "local_frame_policy": "middle_three",
                "cloud_frame_policy": "five_original_frames",
                "environment_samples": len(environment_samples),
                "vital_samples": len(recent_vital_samples),
            }
        return result

    @classmethod
    def _build_candidate_context(
        cls,
        event: NormalizedEventV2,
        candidate: dict[str, Any],
        sensors: dict[str, Any],
        *,
        segments: list[dict[str, Any]] | None = None,
        baselines: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        source_ids = {
            str(item)
            for item in (candidate.get("source_segment_ids") or [])
            if item is not None
        }
        recent_segments = sorted(segments or [], key=lambda item: str(item.get("start_at") or ""), reverse=True)
        matched_segments = [
            item
            for item in recent_segments
            if isinstance(item, dict) and str(item.get("segment_id")) in source_ids
        ]
        if not matched_segments and isinstance(candidate.get("features"), dict):
            embedded = candidate["features"].get("segment")
            if isinstance(embedded, dict):
                matched_segments = [embedded]

        segment_summary = cls._candidate_segment_summary(matched_segments)
        baseline_summary = cls._candidate_baseline_summary(candidate, baselines or [])
        candidate_input = {
            "t": candidate.get("candidate_type"),
            "r": candidate.get("reason"),
            **segment_summary,
            **baseline_summary,
            **cls._candidate_feature_summary(candidate),
        }
        if str(candidate.get("candidate_type") or "") == "bathroom_stay_anomaly":
            observations_desc = sorted(
                (sensors.get("observations") if isinstance(sensors, dict) else []) or [],
                key=lambda item: str(item.get("observed_at") or ""),
                reverse=True,
            )
            env_summary = cls._latest_environment_summary(observations_desc, {"current_room": "bathroom"})
            vital_summary = cls._latest_vital_summary(observations_desc)
            if env_summary:
                candidate_input["env"] = env_summary
            if vital_summary:
                candidate_input["vital"] = vital_summary
        candidate_input = {key: value for key, value in candidate_input.items() if value not in (None, [], {})}
        return {"candidate_local_input": candidate_input}

    @classmethod
    def _latest_environment_summary(
        cls, observations_desc: list[dict[str, Any]], location: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        location_room = str(location.get("current_room")) if location and location.get("current_room") else None
        env_items = [item for item in observations_desc if str(item.get("kind") or "") == "environment"]
        if location_room:
            env_items = [
                item
                for item in env_items
                if str(cls._payload(item).get("room") or "") == location_room
            ] or env_items
        if not env_items:
            return None
        item = env_items[0]
        payload = cls._payload(item)
        return {
            key: value
            for key, value in {
                "temp": payload.get("temperature"),
                "hum": payload.get("humidity"),
            }.items()
            if value is not None
        }

    @classmethod
    def _latest_vital_summary(cls, observations_desc: list[dict[str, Any]]) -> dict[str, Any] | None:
        item = next((item for item in observations_desc if str(item.get("kind") or "") == "vital"), None)
        if not item:
            return None
        payload = cls._payload(item)
        return {
            key: value
            for key, value in {
                "hr": payload.get("heart_rate"),
                "spo2": payload.get("spo2"),
            }.items()
            if value is not None
        }

    @staticmethod
    def _candidate_feature_summary(candidate: dict[str, Any]) -> dict[str, Any]:
        features = candidate.get("features") if isinstance(candidate.get("features"), dict) else {}
        key_map = {
            "duration_seconds": "dur",
            "baseline_p90_seconds": "p90s",
            "baseline_p90": "bp90",
            "baseline_p10": "bp10",
            "latest_value": "latest",
            "metric": "metric",
            "direction": "dir",
            "min": "min",
            "max": "max",
            "p10": "p10",
            "p90": "p90",
            "sample_count": "n",
            "window_seconds": "win_s",
            "returned_to_bedroom": "ret",
            "bathroom_stay_seconds": "bath_s",
            "room": "room",
        }
        return {short: features[key] for key, short in key_map.items() if key in features}

    @staticmethod
    def _candidate_segment_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
        if not segments:
            return {}
        segment = segments[0]
        features = segment.get("features") if isinstance(segment.get("features"), dict) else {}
        summary = {
            "dur": segment.get("duration_seconds"),
            "room": segment.get("room"),
            "rooms": features.get("rooms"),
            "ret": features.get("returned_to_bedroom"),
            "bath_s": features.get("bathroom_stay_seconds"),
            "metric": features.get("metric"),
            "latest": features.get("latest_value"),
            "max": features.get("max"),
            "p10": features.get("p10"),
            "p90": features.get("p90"),
        }
        return {key: value for key, value in summary.items() if value is not None}

    @staticmethod
    def _candidate_baseline_summary(candidate: dict[str, Any], baselines: list[dict[str, Any]]) -> dict[str, Any]:
        candidate_type = str(candidate.get("candidate_type") or "")
        features = candidate.get("features") if isinstance(candidate.get("features"), dict) else {}
        if (
            (candidate_type in {"bathroom_stay_anomaly", "night_behavior_anomaly"} and "baseline_p90_seconds" in features)
            or (candidate_type == "vital_baseline_anomaly" and ("baseline_p90" in features or "baseline_p10" in features))
        ):
            return {}
        if candidate_type == "bathroom_stay_anomaly":
            wanted = {"bathroom_routine"}
        elif candidate_type == "night_behavior_anomaly":
            wanted = {"night_routine", "bathroom_routine"}
        else:
            wanted = {"heart_rate_daily", "spo2_daily"}
        result: dict[str, Any] = {}
        for baseline in baselines:
            baseline_type = str(baseline.get("baseline_type") or "")
            if baseline_type not in wanted:
                continue
            metrics = baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) else {}
            prefix = {
                "night_routine": "night",
                "bathroom_routine": "bathroom",
                "heart_rate_daily": "heart_rate",
                "spo2_daily": "spo2",
            }.get(baseline_type, baseline_type)
            for key in (
                "night_wake_count_p90",
                "night_wake_duration_p90_sec",
                "bathroom_stay_p90_sec",
                "returned_to_bedroom_rate",
                "p10",
                "p90",
                "daily_avg",
                "avg",
            ):
                if key in metrics:
                    short_key = {
                        "night_wake_count_p90": "wake_p90",
                        "night_wake_duration_p90_sec": "p90s",
                        "bathroom_stay_p90_sec": "bath_p90s",
                        "returned_to_bedroom_rate": "ret_rate",
                        "daily_avg": "avg",
                    }.get(key, key)
                    result[f"{prefix}_{short_key}"] = metrics[key]
        return result

    async def _complete_deterministic_vital(
        self,
        workflow: WorkflowV2,
        event: NormalizedEventV2,
        saved_event: dict[str, Any],
        baseline: dict[str, Any],
    ) -> None:
        rule_risk = risk_text(event.risk_level)
        skipped = {
            "status": "skipped",
            "reason": "deterministic_vital_rule",
            "event_semantics": event.summary,
            "risk_level": rule_risk,
            "confidence": event.confidence,
            "fallback": False,
            "latency_ms": 0,
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
        cloud = {"status": "not_required", "reason": "deterministic_vital_rule"}
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
                "local_semantics": event.summary,
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

    @staticmethod
    def _event_payload(event: NormalizedEventV2) -> dict[str, Any]:
        for evidence in event.evidence or []:
            if isinstance(evidence, dict) and isinstance(evidence.get("payload"), dict):
                return evidence["payload"]
        return {}

    @staticmethod
    def _room_label(room: str | None) -> str:
        return {
            "bedroom": "卧室",
            "bathroom": "卫生间",
            "living_room": "客厅",
            "kitchen": "厨房",
            "local": "本地",
        }.get(str(room or ""), room or "当前房间")

    @staticmethod
    def _format_number(value: Any, suffix: str = "") -> str:
        if isinstance(value, (int, float)):
            return f"{value:g}{suffix}"
        return "异常"

    @classmethod
    def _p0_hmi_message(cls, event: NormalizedEventV2) -> str:
        if str(event.event_type) == EventType.GAS_LEAK.value:
            room = cls._room_label(event.room)
            return f"检测到{room}燃气异常，系统已打开窗户、关闭燃气阀并通知家属。请尽快远离厨房，确认您现在是否安全。"
        if str(event.event_type) == EventType.SPO2_LOW.value:
            return "检测到血氧严重偏低，系统已启动紧急告警。请确认您现在是否需要帮助。"
        return "检测到紧急风险，系统已启动紧急处置并通知家属。请确认您现在是否需要帮助。"

    @classmethod
    def _family_alert_message(cls, event: NormalizedEventV2, alert_level: RiskLevel) -> str:
        payload = cls._event_payload(event)
        event_type = str(event.event_type)
        if event_type == EventType.GAS_LEAK.value:
            gas = payload.get("gas_ppm")
            gas_text = f" {gas:g} ppm" if isinstance(gas, (int, float)) else ""
            room = cls._room_label(event.room)
            return f"{room}检测到燃气{gas_text}，系统已关闭燃气阀、打开窗户并启动本地报警。"
        if event_type == EventType.HEART_RATE_ABNORMAL.value:
            heart_rate = payload.get("heart_rate")
            value = cls._format_number(heart_rate, " bpm")
            return f"检测到老人心率 {value}，超过安全阈值，已在 HMI 询问老人。"
        if event_type == EventType.SPO2_LOW.value:
            spo2 = payload.get("spo2")
            value = cls._format_number(spo2, "%")
            if alert_level == RiskLevel.P0:
                return f"检测到老人血氧 {value}，严重低于安全阈值，系统已启动紧急告警。"
            return f"检测到老人血氧 {value}，低于安全阈值，已在 HMI 询问老人。"
        if event_type == "vital_baseline_anomaly":
            features = cls._candidate_features(event)
            metric = str(features.get("metric") or "")
            direction = str(features.get("direction") or "")
            latest = features.get("latest_value", features.get("latest"))
            if metric == "heart_rate":
                label = "高于" if direction == "high" else "低于"
                return f"检测到老人心率 {cls._format_number(latest, ' bpm')}，{label}个人参考范围，已在 HMI 询问老人。"
            if metric == "spo2":
                return f"检测到老人血氧 {cls._format_number(latest, '%')}，低于个人参考范围，已在 HMI 询问老人。"
            return "检测到老人的生命体征与平时相比有异常，已在 HMI 询问老人。"
        if event_type == "bathroom_stay_anomaly":
            features = cls._candidate_features(event)
            duration = features.get("duration_seconds", features.get("dur"))
            limit = features.get("baseline_p90_seconds", features.get("p90s"))
            if isinstance(duration, (int, float)) and isinstance(limit, (int, float)):
                return f"检测到老人在卫生间停留 {duration:g} 秒，超过个人参考上限 {limit:g} 秒，已在 HMI 询问老人。"
            return "检测到老人在卫生间停留时间较长，已在 HMI 询问老人。"
        if alert_level == RiskLevel.P0:
            return "检测到紧急风险事件，系统已启动紧急处置并通知家属。"
        return "检测到高风险事件，系统已在 HMI 询问老人并通知家属。"

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
                message=self._family_alert_message(event, RiskLevel.P0),
            )
        )
        prompt = await self._create_hmi_prompt(workflow, event, self._p0_hmi_message(event))
        return {"action": action, "alert": alert, "prompt": prompt}

    async def _create_hmi_prompt(self, workflow: WorkflowV2, event: NormalizedEventV2, message: str) -> dict[str, Any]:
        return await self.edge.create_hmi_prompt(
            HmiPromptV2(
                workflow_id=workflow.workflow_id,
                event_id=event.event_id,
                elder_id=event.elder_id,
                risk_level=event.risk_level,
                event_type=str(event.event_type),
                message=message,
                options=HMI_OPTIONS,
                timeout_sec=30,
            )
        )

    @staticmethod
    def _p3_hmi_message(event: NormalizedEventV2) -> str:
        event_type = str(event.event_type)
        room = event.room or "当前房间"
        if event_type == EventType.TEMPERATURE_HIGH.value:
            return f"{room} 室温偏高，系统已为您打开空调。您现在感觉还好吗？"
        if event_type == EventType.TEMPERATURE_LOW.value:
            return f"{room} 室温偏低，系统已为您打开空调。您现在感觉还好吗？"
        if event_type == EventType.CO2_HIGH.value:
            return f"{room} 空气质量偏闷，系统已为您开窗通风。您现在感觉还好吗？"
        if event_type == "humidity_abnormal":
            return f"{room} 湿度异常，系统已记录。您现在是否需要帮助？"
        return "系统检测到环境需要关注，您现在感觉还好吗？"

    @staticmethod
    def _candidate_features(event: NormalizedEventV2) -> dict[str, Any]:
        for evidence in event.evidence or []:
            if not isinstance(evidence, dict):
                continue
            candidate = evidence.get("candidate")
            if isinstance(candidate, dict) and isinstance(candidate.get("features"), dict):
                return candidate["features"]
        return {}

    @classmethod
    def _risk_hmi_message(cls, event: NormalizedEventV2) -> str:
        event_type = str(event.event_type)
        if event_type == EventType.HEART_RATE_ABNORMAL.value:
            return "检测到心率明显异常，系统想确认您现在是否舒服？"
        if event_type == EventType.SPO2_LOW.value:
            return "检测到血氧偏低，系统想确认您现在是否舒服？"
        if event_type == EventType.SUSPECTED_FALL.value:
            return "检测到疑似跌倒，请确认您现在是否安全。"
        if event_type == EventType.LONG_STATIC.value:
            return "检测到您较长时间没有活动，请确认您现在是否安全。"
        if event_type == "vital_baseline_anomaly":
            features = cls._candidate_features(event)
            metric = str(features.get("metric") or "")
            direction = str(features.get("direction") or "")
            if metric == "heart_rate":
                if direction == "low":
                    return "检测到心率比平时偏低，系统想确认您现在是否舒服？"
                return "检测到心率比平时偏高，系统想确认您现在是否舒服？"
            if metric == "spo2":
                return "检测到血氧比平时偏低，系统想确认您现在是否舒服？"
            return "检测到生命体征与平时相比有些异常，系统想确认您现在是否舒服？"
        if event_type == "bathroom_stay_anomaly":
            return "检测到您在卫生间停留时间较长，请确认现在是否安全。"
        if event_type == "night_behavior_anomaly":
            return "检测到夜间活动与平时不同，请确认您现在是否安全。"
        if event_type in DETERMINISTIC_P3_EVENTS:
            return cls._p3_hmi_message(event)
        return "系统检测到需要关注的情况，请确认您现在是否安全。"

    async def _execute_p3_with_hmi(self, workflow: WorkflowV2, event: NormalizedEventV2) -> dict[str, Any]:
        event_type = str(event.event_type)
        action: dict[str, Any] | None = None
        if event_type == EventType.CO2_HIGH.value:
            action = await self.edge.request_home_action(
                ActionRequestV2(
                    workflow_id=workflow.workflow_id,
                    event_id=event.event_id,
                    elder_id=event.elder_id,
                    commands=[
                        ActionCommandV2(
                            room=event.room or "living_room",
                            device=DeviceType.WINDOW,
                            action=DeviceAction.OPEN,
                            reason="CO2 偏高，自动通风。",
                        )
                    ],
                    reason=event.summary,
                    priority=RiskLevel.P3,
                )
            )
        elif event_type in {EventType.TEMPERATURE_HIGH.value, EventType.TEMPERATURE_LOW.value}:
            target = 26 if event_type == EventType.TEMPERATURE_HIGH.value else 24
            action = await self.edge.request_home_action(
                ActionRequestV2(
                    workflow_id=workflow.workflow_id,
                    event_id=event.event_id,
                    elder_id=event.elder_id,
                    commands=[
                        ActionCommandV2(
                            room=event.room or "living_room",
                            device=DeviceType.AIR_CONDITIONER,
                            action=DeviceAction.SET_TEMPERATURE,
                            value=target,
                            reason="室温异常。",
                        )
                    ],
                    reason=event.summary,
                    priority=RiskLevel.P3,
                )
            )
        prompt = await self._create_hmi_prompt(workflow, event, self._p3_hmi_message(event))
        result: dict[str, Any] = {"status": "p3_hmi_prompted", "prompt": prompt}
        if action is not None:
            result["action"] = action
        return result

    async def _execute_policy(
        self, workflow: WorkflowV2, event: NormalizedEventV2, decision: dict[str, Any]
    ) -> dict[str, Any]:
        risk = risk_text(event.risk_level)
        if risk == "P0":
            return await self._execute_p0(workflow, event)
        if risk == "P3" and str(event.event_type) in DETERMINISTIC_P3_EVENTS:
            return await self._execute_p3_with_hmi(workflow, event)
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
            prompt = await self._create_hmi_prompt(workflow, event, self._risk_hmi_message(event))
            alert = None
            if risk == "P1":
                alert = await self.edge.raise_family_alert(
                    AlertRequestV2(
                        workflow_id=workflow.workflow_id,
                        event_id=event.event_id,
                        elder_id=event.elder_id,
                        alert_level=RiskLevel.P1,
                        message=self._family_alert_message(event, RiskLevel.P1),
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
