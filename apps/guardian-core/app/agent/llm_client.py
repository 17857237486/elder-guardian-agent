from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from guardian_shared.enums import EventType, RiskLevel
from guardian_shared.schemas import AgentDecision
from guardian_shared.utils import model_to_dict

from app.agent.prompt_templates import SYSTEM_PROMPT, build_user_prompt
from app.config import settings

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _pick(value: Any, keys: list[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        return None
    return {key: _json_safe(value.get(key)) for key in keys if key in value}


class LLMClient:
    def __init__(self) -> None:
        self.mock = settings.llm_mock

    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        if self.mock:
            return model_to_dict(self._mock_decision(context))
        return await self._call_openai_compatible(context)

    def _mock_decision(self, context: dict[str, Any]) -> AgentDecision:
        rule = context.get("rule_result", {})
        risk_level = RiskLevel(rule.get("risk_level", "P4"))
        event_type = rule.get("event_type", EventType.NORMAL.value)
        summary = rule.get("summary", "未发现异常。")
        recommended: list[str]
        need_elder = risk_level in {RiskLevel.P1, RiskLevel.P2}
        need_family = risk_level in {RiskLevel.P0, RiskLevel.P1}
        if risk_level == RiskLevel.P0:
            recommended = ["立即告警", "通知家属", "执行安全联动"]
        elif risk_level == RiskLevel.P1:
            recommended = ["本地询问老人", "同步通知家属", "持续观察"]
        elif risk_level == RiskLevel.P2:
            recommended = ["本地询问老人", "超时升级通知家属"]
        elif risk_level == RiskLevel.P3:
            recommended = ["自动调节环境", "记录事件"]
        else:
            recommended = ["记录正常状态"]
        return AgentDecision(
            risk_level=risk_level,
            risk_score=float(rule.get("risk_score", 0.0)),
            event_type=str(event_type),
            reasoning_summary=summary,
            recommended_actions=recommended,
            need_elder_confirmation=need_elder,
            need_family_notification=need_family,
            alert_priority=risk_level,
            device_actions=[],
        )

    async def _call_openai_compatible(self, context: dict[str, Any]) -> dict[str, Any]:
        url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
        compact_context = self._compact_context(context)
        payload = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(compact_context)},
            ],
            "temperature": 0.2,
            "max_tokens": settings.llm_max_tokens,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=settings.llm_timeout_sec) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        logger.info("LLM response received")
        return {"raw": content}

    def _compact_context(self, context: dict[str, Any]) -> dict[str, Any]:
        rule = context.get("rule_result", {})
        event = context.get("current_event", {})
        source = context.get("source_payload", {})
        elder_profile = context.get("elder_profile", {})
        recent_vital = context.get("recent_vital")
        recent_environment = context.get("recent_environment")
        recent_vision = context.get("vision_summary", [])
        devices = context.get("device_states", [])

        return _json_safe(
            {
                "elder_profile": {
                    "elder_id": elder_profile.get("elder_id"),
                    "name": elder_profile.get("name"),
                    "age": elder_profile.get("age"),
                    "conditions": elder_profile.get("conditions", []),
                },
                "current_event": {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "risk_level": event.get("risk_level"),
                    "risk_score": event.get("risk_score"),
                    "room": event.get("room"),
                    "summary": event.get("summary"),
                },
                "rule_result": {
                    "event_type": rule.get("event_type"),
                    "risk_level": rule.get("risk_level"),
                    "risk_score": rule.get("risk_score"),
                    "summary": rule.get("summary"),
                    "source": rule.get("source"),
                    "room": rule.get("room"),
                    "trace": rule.get("trace"),
                },
                "source_payload": source,
                "recent_vital": _pick(
                    recent_vital,
                    ["heart_rate", "spo2", "systolic_bp", "diastolic_bp", "body_temperature", "timestamp"],
                ),
                "recent_environment": _pick(
                    recent_environment,
                    ["room", "temperature", "humidity", "co2_ppm", "gas_ppm", "smoke_ppm", "timestamp"],
                ),
                "recent_vision": [
                    _pick(item, ["event_type", "room", "confidence", "posture", "motion_state", "timestamp"])
                    for item in recent_vision[:2]
                ]
                if isinstance(recent_vision, list)
                else [],
                "device_states": [
                    _pick(item, ["room", "device", "state", "value", "online", "timestamp"]) for item in devices[:8]
                ]
                if isinstance(devices, list)
                else [],
            }
        )
