from __future__ import annotations

from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - lets HTTP APIs run without MCP installed in dev shells
    FastMCP = None  # type: ignore[assignment]

from guardian_shared.v2 import ActionRequestV2, AlertRequestV2, WorkflowNoteV2

from app.tool_service import EdgeToolService


def build_mcp(service: EdgeToolService) -> Any:
    if FastMCP is None:
        return None

    mcp = FastMCP("elder-guardian-edge")

    @mcp.tool()
    def get_current_event(event_id: str) -> dict[str, Any]:
        return service.get_current_event(event_id)

    @mcp.tool()
    def get_recent_sensor_context(elder_id: str, limit: int = 30) -> dict[str, Any]:
        return service.get_recent_sensor_context(elder_id, limit)

    @mcp.tool()
    def get_home_device_snapshot(elder_id: str) -> dict[str, Any]:
        return service.get_home_device_snapshot(elder_id)

    @mcp.tool()
    def request_home_action(request: dict[str, Any]) -> dict[str, Any]:
        return service.request_home_action(ActionRequestV2(**request))

    @mcp.tool()
    def raise_family_alert(request: dict[str, Any]) -> dict[str, Any]:
        return service.raise_family_alert(AlertRequestV2(**request))

    @mcp.tool()
    def record_workflow_note(note: dict[str, Any]) -> dict[str, Any]:
        return service.record_workflow_note(WorkflowNoteV2(**note))

    return mcp

