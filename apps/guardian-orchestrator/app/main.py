from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from guardian_shared.v2 import NormalizedEventV2

from app.rules import classify_observation
from app.workflow import WorkflowRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

runner = WorkflowRunner()


app = FastAPI(title="Elder Guardian Orchestrator", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "guardian-orchestrator"}


@app.post("/api/v2/orchestrator/observations")
async def handle_observation(observation: dict[str, Any]) -> dict[str, Any]:
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


@app.post("/api/v2/orchestrator/candidates")
async def handle_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    result = await runner.run_candidate(candidate)
    return {"ok": True, **result}

