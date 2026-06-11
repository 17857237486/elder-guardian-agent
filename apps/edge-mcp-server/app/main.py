from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import FastAPI

from guardian_shared.v2 import ActionRequestV2, AlertRequestV2, HmiPromptV2, HmiResponseV2, NormalizedEventV2, RawObservationV2, WorkflowNoteV2, WorkflowStepV2, WorkflowV2

from app.config import settings
from app.database import SessionLocal, init_db
from app import repository
from app.mcp_tools import build_mcp
from app.mqtt_bridge import MqttBridge
from app.tool_service import EdgeToolService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Elder Guardian Edge MCP Server", version="0.1.0")
mqtt_bridge = MqttBridge()
tool_service = EdgeToolService(mqtt_bridge)
mcp = build_mcp(tool_service)
if mcp is not None and hasattr(mcp, "streamable_http_app"):
    app.mount("/mcp", mcp.streamable_http_app())


@app.on_event("startup")
async def startup() -> None:
    init_db()
    mqtt_bridge.start(asyncio.get_running_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
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


@app.get("/api/v2/observations")
async def list_observations(elder_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"observations": repository.list_observations(db, elder_id or settings.elder_id, limit)}


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
