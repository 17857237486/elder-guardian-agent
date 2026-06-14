from __future__ import annotations

from typing import Any

import httpx

from guardian_shared.utils import model_to_dict
from guardian_shared.v2 import ActionRequestV2, AlertRequestV2, HmiPromptV2, NormalizedEventV2, WorkflowStepV2, WorkflowV2

from app.config import settings


class EdgeClient:
    def __init__(self) -> None:
        self.base = settings.edge_api_base.rstrip("/")

    async def create_event(self, event: NormalizedEventV2) -> dict[str, Any]:
        return await self._post("/api/v2/events", model_to_dict(event))

    async def create_workflow(self, workflow: WorkflowV2) -> dict[str, Any]:
        return await self._post("/api/v2/workflows", model_to_dict(workflow))

    async def record_step(self, step: WorkflowStepV2) -> dict[str, Any]:
        return await self._post(f"/api/v2/workflows/{step.workflow_id}/steps", model_to_dict(step))

    async def get_current_event(self, event_id: str) -> dict[str, Any]:
        return await self._get(f"/api/v2/events/{event_id}")

    async def update_event_analysis(self, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._patch(f"/api/v2/events/{event_id}/analysis", payload)

    async def get_recent_sensor_context(self, elder_id: str, limit: int = 30) -> dict[str, Any]:
        return await self._get(f"/api/v2/tools/recent-sensor-context/{elder_id}?limit={limit}")

    async def get_home_device_snapshot(self, elder_id: str) -> dict[str, Any]:
        return await self._get(f"/api/v2/tools/device-snapshot/{elder_id}")

    async def request_home_action(self, request: ActionRequestV2) -> dict[str, Any]:
        return await self._post("/api/v2/tools/request-home-action", model_to_dict(request))

    async def raise_family_alert(self, request: AlertRequestV2) -> dict[str, Any]:
        return await self._post("/api/v2/tools/raise-family-alert", model_to_dict(request))

    async def create_hmi_prompt(self, prompt: HmiPromptV2) -> dict[str, Any]:
        return await self._post("/api/v2/hmi/prompts", model_to_dict(prompt))

    async def _get(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(self.base + path)
            response.raise_for_status()
            return response.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(self.base + path, json=payload)
            response.raise_for_status()
            return response.json()

    async def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.patch(self.base + path, json=payload)
            response.raise_for_status()
            return response.json()
