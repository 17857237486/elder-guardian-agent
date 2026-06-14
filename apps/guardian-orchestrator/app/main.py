from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from guardian_shared.v2 import NormalizedEventV2

from app.config import settings
from app.night_activity import NightActivityMonitor
from app.rules import classify_observation
from app.workflow import WorkflowRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

runner = WorkflowRunner()
night_monitor = NightActivityMonitor(runner.run)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        observations = await runner.edge.get_observations(settings.elder_id)
        await night_monitor.restore(observations)
    except Exception:
        logging.getLogger(__name__).exception("failed to restore night activity state")
    try:
        yield
    finally:
        await night_monitor.close()


app = FastAPI(title="Elder Guardian Orchestrator", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "guardian-orchestrator"}


@app.post("/api/v2/orchestrator/observations")
async def handle_observation(observation: dict[str, Any]) -> dict[str, Any]:
    await night_monitor.observe(observation)
    event = classify_observation(observation)
    if event is None:
        return {"ok": True, "triggered": False}
    payload = observation.get("payload") if isinstance(observation.get("payload"), dict) else {}
    evidence = [{"kind": observation.get("kind"), "observation_id": observation.get("observation_id"), "payload": payload}]
    event = event.model_copy(
        update={
            "source_kind": observation.get("kind"),
            "evidence": evidence,
            "frame_set_id": payload.get("frame_set_id"),
            "rule_risk_level": event.risk_level,
            "local_risk_level": event.risk_level,
            "final_risk_level": event.risk_level,
            "confidence": max(event.confidence, event.risk_score, float(payload.get("confidence") or 0)),
        }
    )
    result = await runner.run(event)
    return {"ok": True, "triggered": True, **result}


@app.post("/api/v2/orchestrator/events")
async def handle_event(event: NormalizedEventV2) -> dict[str, Any]:
    result = await runner.run(event)
    return {"ok": True, **result}

