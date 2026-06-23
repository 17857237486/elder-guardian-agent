from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from guardian_shared.v2 import NormalizedEventV2

from app.config import settings
from app.event_cooldown import P3EnvironmentCooldown
from app.rules import classify_observation
from app.workflow import WorkflowRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

runner = WorkflowRunner()
p3_environment_cooldown = P3EnvironmentCooldown(settings.p3_environment_cooldown_sec)


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
    cooldown = p3_environment_cooldown.check(event)
    if cooldown.suppressed:
        logger.info(
            "suppressed duplicate P3 environment event",
            extra={
                "event_type": str(event.event_type),
                "room": event.room,
                "observation_id": observation.get("observation_id"),
                "dedupe_key": cooldown.dedupe_key,
                "remaining_sec": round(cooldown.remaining_sec, 3),
            },
        )
        return {
            "ok": True,
            "triggered": False,
            "suppressed": True,
            "suppressed_reason": "p3_environment_cooldown",
            "dedupe_key": cooldown.dedupe_key,
            "remaining_sec": round(cooldown.remaining_sec, 3),
        }
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


@app.post("/api/v2/orchestrator/daily-health-summary")
async def handle_daily_health_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("daily_health_summary") if isinstance(payload.get("daily_health_summary"), dict) else payload
    result = await runner.cloud_llm.daily_health_summary(summary)
    return {"ok": True, "cloud_summary_result": result}

