from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from guardian_shared.v2 import (
    ActionRequestV2,
    AiReviewCandidateV2,
    AlertRequestV2,
    BehaviorSegmentV2,
    DeviceReadingV2,
    HmiPromptV2,
    HmiResponseV2,
    NormalizedEventV2,
    PersonalBaselineV2,
    RawObservationV2,
    WorkflowNoteV2,
    WorkflowStepV2,
    WorkflowV2,
)

from app.behavior_worker import BehaviorAnalyticsWorker
from app.config import settings
from app.database import SessionLocal, init_db
from app import repository
from app.mcp_tools import build_mcp
from app.mqtt_bridge import MqttBridge
from app.tool_service import EdgeToolService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Elder Guardian Edge MCP Server", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
mqtt_bridge = MqttBridge()
tool_service = EdgeToolService(mqtt_bridge)
behavior_worker = BehaviorAnalyticsWorker()
mcp = build_mcp(tool_service)
if mcp is not None and hasattr(mcp, "streamable_http_app"):
    app.mount("/mcp", mcp.streamable_http_app())


@app.on_event("startup")
async def startup() -> None:
    init_db()
    mqtt_bridge.start(asyncio.get_running_loop())
    behavior_worker.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await behavior_worker.close()
    mqtt_bridge.stop()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "edge-mcp-server",
        "elder_id": settings.elder_id,
        "mqtt_connected": mqtt_bridge.connected,
        "mcp_enabled": mcp is not None,
    }


@app.get("/api/v2/dashboard/state")
async def dashboard_state(elder_id: str | None = None) -> dict[str, Any]:
    with SessionLocal() as db:
        return repository.dashboard_state(db, elder_id or settings.elder_id)


@app.post("/api/v2/dashboard/clear")
async def clear_dashboard_history(payload: dict[str, Any] | None = None, elder_id: str | None = None) -> dict[str, Any]:
    target_elder_id = elder_id or (payload or {}).get("elder_id") or settings.elder_id
    with SessionLocal() as db:
        counts = repository.clear_demo_runtime_history(db, str(target_elder_id))
    return {"ok": True, "elder_id": target_elder_id, "deleted": counts}


@app.post("/api/v2/hmi/clear")
async def clear_hmi_history(payload: dict[str, Any] | None = None, elder_id: str | None = None) -> dict[str, Any]:
    target_elder_id = elder_id or (payload or {}).get("elder_id") or settings.elder_id
    with SessionLocal() as db:
        counts = repository.clear_demo_runtime_history(db, str(target_elder_id))
    return {"ok": True, "elder_id": target_elder_id, "deleted": counts}


@app.get("/api/v2/observations")
async def list_observations(elder_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"observations": repository.list_observations(db, elder_id or settings.elder_id, limit)}


@app.post("/api/v2/device-readings")
async def create_device_reading(reading: DeviceReadingV2) -> dict[str, Any]:
    with SessionLocal() as db:
        record = repository.create_device_reading(db, reading)
    return {"ok": True, "device_reading": record}


@app.get("/api/v2/device-readings")
async def list_device_readings(elder_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"device_readings": repository.list_device_readings(db, elder_id or settings.elder_id, limit)}


@app.get("/api/v2/device-readings/latest")
async def latest_device_readings(elder_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"device_readings_latest": repository.latest_device_readings(db, elder_id or settings.elder_id, limit)}


async def forward_candidate_to_orchestrator(record: dict[str, Any]) -> None:
    if not settings.orchestrator_url:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{settings.orchestrator_url.rstrip('/')}/api/v2/orchestrator/candidates", json=record)
            response.raise_for_status()
    except Exception:
        logger.exception("Failed to forward AI review candidate to orchestrator")


@app.get("/api/v2/behavior-segments")
async def list_behavior_segments(elder_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"behavior_segments": repository.list_behavior_segments(db, elder_id or settings.elder_id, limit)}


@app.post("/api/v2/behavior-segments")
async def create_behavior_segment(segment: BehaviorSegmentV2) -> dict[str, Any]:
    with SessionLocal() as db:
        record = repository.create_behavior_segment(db, segment)
    return {"ok": True, "behavior_segment": record}


@app.get("/api/v2/personal-baselines")
async def list_personal_baselines(elder_id: str | None = None) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"personal_baselines": repository.list_personal_baselines(db, elder_id or settings.elder_id)}


@app.post("/api/v2/personal-baselines")
async def create_personal_baseline(baseline: PersonalBaselineV2) -> dict[str, Any]:
    with SessionLocal() as db:
        record = repository.create_personal_baseline(db, baseline)
    return {"ok": True, "personal_baseline": record}


@app.get("/api/v2/ai-review-candidates")
async def list_ai_review_candidates(elder_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"ai_review_candidates": repository.list_ai_review_candidates(db, elder_id or settings.elder_id, limit)}


@app.post("/api/v2/ai-review-candidates")
async def create_ai_review_candidate(candidate: AiReviewCandidateV2) -> dict[str, Any]:
    with SessionLocal() as db:
        record = repository.create_ai_review_candidate(db, candidate)
    if settings.orchestrator_url:
        asyncio.create_task(forward_candidate_to_orchestrator(record))
        return {"ok": True, "ai_review_candidate": record, "orchestrator": {"status": "queued"}}
    return {"ok": True, "ai_review_candidate": record, "orchestrator": {"status": "disabled"}}


@app.patch("/api/v2/ai-review-candidates/{candidate_id}")
async def update_ai_review_candidate(candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with SessionLocal() as db:
        record = repository.update_ai_review_candidate(db, candidate_id, payload)
    return {"ok": record is not None, "ai_review_candidate": record or {"candidate_id": candidate_id, "status": "not_found"}}


async def forward_observation_to_orchestrator(record: dict[str, Any]) -> None:
    if not settings.orchestrator_url:
        return
    try:
        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(f"{settings.orchestrator_url.rstrip('/')}/api/v2/orchestrator/observations", json=record)
            response.raise_for_status()
    except Exception:
        logger.exception("Failed to forward observation to orchestrator")


@app.post("/api/v2/observations")
async def ingest_observation(observation: RawObservationV2) -> dict[str, Any]:
    with SessionLocal() as db:
        record = repository.create_observation(db, observation)
    if settings.orchestrator_url:
        asyncio.create_task(forward_observation_to_orchestrator(record))
        return {"ok": True, "observation": record, "orchestrator": {"status": "queued"}}
    return {"ok": True, "observation": record, "orchestrator": {"status": "disabled"}}


@app.get("/api/v2/workflows")
async def list_workflows(elder_id: str | None = None, limit: int = 30) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"workflows": repository.list_workflows(db, elder_id or settings.elder_id, limit)}


@app.get("/api/v2/workflow-steps")
async def list_workflow_steps(elder_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"workflow_steps": repository.list_workflow_steps(db, elder_id or settings.elder_id, limit)}


@app.get("/api/v2/tool-calls")
async def list_tool_calls(elder_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"tool_calls": repository.list_tool_calls(db, elder_id or settings.elder_id, limit)}


@app.get("/api/v2/action-executions")
async def list_action_executions(elder_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"action_executions": repository.list_action_executions(db, elder_id or settings.elder_id, limit)}


@app.get("/api/v2/alerts")
async def list_alerts(elder_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"alerts": repository.list_alerts(db, elder_id or settings.elder_id, limit)}


@app.post("/api/v2/events")
async def create_event(event: NormalizedEventV2) -> dict[str, Any]:
    return tool_service.create_normalized_event(event)


@app.get("/api/v2/events/{event_id}")
async def get_event(event_id: str) -> dict[str, Any]:
    return tool_service.get_current_event(event_id)


@app.patch("/api/v2/events/{event_id}/analysis")
async def update_event_analysis(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return tool_service.update_event_analysis(event_id, payload)


@app.post("/api/v2/workflows")
async def create_workflow(workflow: WorkflowV2) -> dict[str, Any]:
    return tool_service.create_workflow(workflow)


@app.post("/api/v2/workflows/{workflow_id}/steps")
async def record_workflow_step(workflow_id: str, step: WorkflowStepV2) -> dict[str, Any]:
    step = step.model_copy(update={"workflow_id": workflow_id})
    return tool_service.record_workflow_step(step)


@app.post("/api/v2/hmi/prompts")
async def create_hmi_prompt(prompt: HmiPromptV2) -> dict[str, Any]:
    return tool_service.create_hmi_prompt(prompt)


@app.get("/api/v2/hmi/current")
async def get_current_hmi_prompt(elder_id: str | None = None) -> dict[str, Any]:
    return tool_service.get_current_hmi_prompt(elder_id or settings.elder_id)


@app.post("/api/v2/hmi/respond")
async def respond_hmi(response: HmiResponseV2) -> dict[str, Any]:
    return tool_service.respond_hmi(response)


@app.get("/api/v2/tools/recent-sensor-context/{elder_id}")
async def get_recent_sensor_context(elder_id: str, limit: int = 30) -> dict[str, Any]:
    return tool_service.get_recent_sensor_context(elder_id, limit)


@app.get("/api/v2/tools/device-snapshot/{elder_id}")
async def get_home_device_snapshot(elder_id: str) -> dict[str, Any]:
    return tool_service.get_home_device_snapshot(elder_id)


@app.post("/api/v2/tools/request-home-action")
async def request_home_action(request: ActionRequestV2) -> dict[str, Any]:
    return tool_service.request_home_action(request)


@app.post("/api/v2/tools/raise-family-alert")
async def raise_family_alert(request: AlertRequestV2) -> dict[str, Any]:
    return tool_service.raise_family_alert(request)


@app.post("/api/v2/tools/record-workflow-note")
async def record_workflow_note(note: WorkflowNoteV2) -> dict[str, Any]:
    return tool_service.record_workflow_note(note)
