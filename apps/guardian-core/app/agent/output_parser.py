from __future__ import annotations

import json
import re
from typing import Any

from guardian_shared.schemas import AgentDecision


class OutputParser:
    def parse(self, payload: dict[str, Any]) -> AgentDecision:
        if "raw" in payload:
            payload = json.loads(self._clean_raw_json(payload["raw"]))
        payload = self._normalize(payload)
        return AgentDecision(**payload)

    def _clean_raw_json(self, raw: str) -> str:
        text = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return text

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        risk_level = str(payload.get("risk_level", "P4")).upper()
        if risk_level not in {"P0", "P1", "P2", "P3", "P4"}:
            risk_level = "P4"
        payload["risk_level"] = risk_level

        alert_priority = str(payload.get("alert_priority", risk_level)).upper()
        priority_aliases = {"紧急": "P0", "高": "P1", "中": "P2", "低": "P3", "正常": "P4"}
        alert_priority = priority_aliases.get(alert_priority, alert_priority)
        if alert_priority not in {"P0", "P1", "P2", "P3", "P4"}:
            alert_priority = risk_level
        payload["alert_priority"] = alert_priority

        recommended = payload.get("recommended_actions", [])
        if isinstance(recommended, str):
            recommended = [recommended]
        payload["recommended_actions"] = recommended if isinstance(recommended, list) else []

        device_actions = payload.get("device_actions", [])
        if isinstance(device_actions, dict):
            device_actions = [device_actions]
        elif isinstance(device_actions, str):
            device_actions = []
        payload["device_actions"] = device_actions if isinstance(device_actions, list) else []

        payload["need_elder_confirmation"] = bool(payload.get("need_elder_confirmation", False))
        payload["need_family_notification"] = bool(payload.get("need_family_notification", False))
        payload["event_type"] = str(payload.get("event_type", "normal"))
        payload["reasoning_summary"] = str(payload.get("reasoning_summary", ""))
        return payload
