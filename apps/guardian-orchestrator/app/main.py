from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from guardian_shared.v2 import NormalizedEventV2

from app.rules import classify_observation
from app.workflow import WorkflowRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Elder Guardian Orchestrator", version="0.1.0")
runner = WorkflowRunner()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "guardian-orchestrator"}


@app.post("/api/v2/orchestrator/observations")
async def handle_observation(observation: dict[str, Any]) -> dict[str, Any]:
    event = classify_observation(observation)
    if event is None:
        return {"ok": True, "triggered": False}
    result = await runner.run(event)
    return {"ok": True, "triggered": True, **result}


@app.post("/api/v2/orchestrator/events")
async def handle_event(event: NormalizedEventV2) -> dict[str, Any]:
    result = await runner.run(event)
    return {"ok": True, **result}

