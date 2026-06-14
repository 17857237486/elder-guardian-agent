from __future__ import annotations

import json
import base64
from pathlib import Path
from typing import Any

import httpx

from app.config import settings


REQUIRED_FIELDS = {"summary", "relevant_facts", "risk_notes", "uncertainty", "next_step_hint"}
DECISION_REQUIRED_FIELDS = REQUIRED_FIELDS | {"reviewed_risk_level", "recommended_followup"}
STEP_TOKEN_BUDGETS = {
    "context_fetch_conversation": 768,
    "sensor_fusion_conversation": 768,
    "risk_decision_conversation": 1024,
    "advisory_conversation": 768,
}
STEP_TIMEOUT_BUDGETS = {
    "context_fetch_conversation": 35,
    "sensor_fusion_conversation": 35,
    "risk_decision_conversation": 45,
    "advisory_conversation": 45,
}
RISK_ORDER = {"P4": 0, "P3": 1, "P2": 2, "P1": 3, "P0": 4}
FORBIDDEN_DECISION_KEYS = {
    "commands",
    "command",
    "mqtt_topic",
    "mqtt",
    "device_action",
    "device_actions",
    "action_request",
    "action_commands",
}

MULTIMODAL_REQUIRED_FIELDS = {
    "event_semantics",
    "risk_level",
    "confidence",
    "temporal_changes",
    "supporting_evidence",
    "contradictions",
    "missing_information",
    "recommended_followup",
    "family_summary",
}


class LLMOutputError(ValueError):
    pass


def _extract_json_object(content: str) -> dict[str, Any]:
    if not content.strip():
        raise LLMOutputError("LLM returned empty content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise LLMOutputError("LLM response did not contain a JSON object")
        try:
            parsed = json.loads(content[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMOutputError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMOutputError("LLM JSON output must be an object")
    return parsed


def _risk_value(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).split(".")[-1].upper()
    return RISK_ORDER.get(text)


def _event_risk_level(payload: dict[str, Any]) -> str | None:
    event = payload.get("event")
    if isinstance(event, dict):
        value = event.get("risk_level")
        if value is not None:
            return str(value).split(".")[-1].upper()
    return None


def _contains_forbidden_action_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in FORBIDDEN_DECISION_KEYS:
                return True
            if _contains_forbidden_action_key(item):
                return True
    if isinstance(value, list):
        return any(_contains_forbidden_action_key(item) for item in value)
    return False


def _normalize_output(step_name: str, payload: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    repaired_fields: list[str] = []
    if step_name == "risk_decision_conversation":
        original_risk = _event_risk_level(payload)
        if "reviewed_risk_level" not in output and original_risk:
            output["reviewed_risk_level"] = original_risk
            repaired_fields.append("reviewed_risk_level")
        if "recommended_followup" not in output:
            hint = output.get("next_step_hint")
            output["recommended_followup"] = [str(hint)] if hint else []
            repaired_fields.append("recommended_followup")
    required = DECISION_REQUIRED_FIELDS if step_name == "risk_decision_conversation" else REQUIRED_FIELDS
    missing = sorted(field for field in required if field not in output)
    if missing:
        raise LLMOutputError(f"LLM output missing required fields: {', '.join(missing)}")
    normalized = dict(output)
    for field in ["relevant_facts", "risk_notes"]:
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = [value]
        elif value is None:
            normalized[field] = []
        elif not isinstance(value, list):
            raise LLMOutputError(f"LLM field {field} must be a list or string")
    for field in ["summary", "uncertainty", "next_step_hint"]:
        if normalized.get(field) is None:
            normalized[field] = ""
        if not isinstance(normalized[field], str):
            normalized[field] = str(normalized[field])
    if step_name == "risk_decision_conversation":
        original_risk = _event_risk_level(payload)
        reviewed_risk = str(normalized.get("reviewed_risk_level", "")).split(".")[-1].upper()
        original_value = _risk_value(original_risk)
        reviewed_value = _risk_value(reviewed_risk)
        if original_value is not None and reviewed_value is not None and reviewed_value < original_value:
            raise LLMOutputError(f"LLM attempted to downgrade risk from {original_risk} to {reviewed_risk}")
        if original_risk == "P1" and reviewed_risk in {"P3", "P4"}:
            raise LLMOutputError("LLM attempted to downgrade P1 to P3/P4")
        if _contains_forbidden_action_key(normalized):
            raise LLMOutputError("LLM decision output contains forbidden device control fields")
        normalized["reviewed_risk_level"] = reviewed_risk or original_risk
        if not isinstance(normalized.get("recommended_followup"), list):
            normalized["recommended_followup"] = [str(normalized.get("recommended_followup", ""))]
    if repaired_fields:
        normalized["schema_repaired_fields"] = repaired_fields
    normalized["step_name"] = step_name
    return normalized


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return str(value)[:200]
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 16:
                compact["truncated_keys"] = True
                break
            compact[str(key)] = _compact_value(item, depth=depth + 1)
        return compact
    if isinstance(value, list):
        return [_compact_value(item, depth=depth + 1) for item in value[:8]]
    if isinstance(value, str):
        return value[:500]
    return value


def _compact_payload(step_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event")
    compact: dict[str, Any] = {"event": _compact_value(event)}
    if step_name == "context_fetch_conversation":
        context = payload.get("context")
        devices = payload.get("devices")
        if isinstance(context, dict):
            compact["context"] = {
                "observations": _compact_value(context.get("observations", [])[:6] if isinstance(context.get("observations"), list) else context),
            }
        if devices is not None:
            compact["devices"] = _compact_value(devices)
    else:
        for key in ["context", "fusion", "decision", "baseline_execution"]:
            if key in payload:
                compact[key] = _compact_value(payload[key])
    return compact


class StepLLMClient:
    async def run_step(self, *, step_name: str, instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
        if settings.llm_mock:
            return {
                "step_name": step_name,
                "mock": True,
                "summary": instruction,
                "relevant_facts": sorted(payload.keys()),
                "risk_notes": [],
                "uncertainty": "mock mode",
                "next_step_hint": "mock output",
            }
        attempts = min(max(settings.llm_step_retries, 0) + 1, 2)
        errors: list[str] = []
        for attempt in range(attempts):
            try:
                attempt_payload = payload if attempt == 0 else _compact_payload(step_name, payload)
                return await self._run_attempt(
                    step_name=step_name,
                    instruction=instruction,
                    payload=attempt_payload,
                    strict_retry=attempt > 0,
                )
            except Exception as exc:
                errors.append(str(exc))
        raise LLMOutputError("; ".join(errors))

    async def _run_attempt(
        self,
        *,
        step_name: str,
        instruction: str,
        payload: dict[str, Any],
        strict_retry: bool,
    ) -> dict[str, Any]:
        url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        required = DECISION_REQUIRED_FIELDS if step_name == "risk_decision_conversation" else REQUIRED_FIELDS
        schema_hint = {
            "required_fields": sorted(required),
            "list_fields": ["relevant_facts", "risk_notes"],
            "output_template": {
                "summary": "一句中文摘要",
                "relevant_facts": ["事实1"],
                "risk_notes": ["风险说明1"],
                "uncertainty": "不确定性",
                "next_step_hint": "下一步提示",
                **(
                    {
                        "reviewed_risk_level": "保持原规则风险等级，例如 P3/P1/P0",
                        "recommended_followup": ["后续观察建议1"],
                    }
                    if step_name == "risk_decision_conversation"
                    else {}
                ),
            },
            "decision_rules": [
                "risk_decision_conversation 不得降低规则风险等级",
                "risk_decision_conversation 不得输出 commands/mqtt/device action 字段",
            ],
        }
        system = (
            "你是居家老人健康守护系统的单步分析器。每轮只完成当前步骤，必须输出一个 JSON object。"
            "不要输出 Markdown，不要调用或虚构工具，不要下达设备控制指令。"
            "固定输出字段：summary, relevant_facts, risk_notes, uncertainty, next_step_hint。"
            "risk_decision_conversation 还必须输出 reviewed_risk_level 和 recommended_followup。"
        )
        if strict_retry:
            system += " 这是重试：只输出合法 JSON，不要解释，不要省略 output_template 中任何字段，短句即可。"
        body = {
            "model": settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": "/no_think\n"
                    + json.dumps(
                        {"step": step_name, "instruction": instruction, "schema": schema_hint, "input": payload},
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": min(max(STEP_TOKEN_BUDGETS.get(step_name, 512), 128), max(settings.llm_max_tokens, 512)),
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        timeout = min(settings.llm_timeout_sec, STEP_TIMEOUT_BUDGETS.get(step_name, 45))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {settings.llm_api_key}"}, json=body)
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            content = message.get("content") or ""
        parsed = _extract_json_object(content)
        return _normalize_output(step_name, payload, parsed)


def _image_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _normalize_multimodal_output(payload: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(MULTIMODAL_REQUIRED_FIELDS - output.keys())
    if missing:
        raise LLMOutputError(f"multimodal output missing required fields: {', '.join(missing)}")
    if _contains_forbidden_action_key(output):
        raise LLMOutputError("multimodal output contains forbidden device control fields")
    original = _event_risk_level(payload) or "P4"
    reviewed = str(output.get("risk_level", original)).split(".")[-1].upper()
    if reviewed not in RISK_ORDER:
        raise LLMOutputError(f"invalid risk level: {reviewed}")
    if RISK_ORDER[reviewed] < RISK_ORDER[original]:
        raise LLMOutputError(f"model attempted to downgrade risk from {original} to {reviewed}")
    normalized = dict(output)
    normalized["risk_level"] = reviewed
    normalized["confidence"] = max(0.0, min(float(output.get("confidence", 0.0)), 1.0))
    for field in ["temporal_changes", "supporting_evidence", "contradictions", "missing_information", "recommended_followup"]:
        value = normalized.get(field)
        normalized[field] = value if isinstance(value, list) else ([] if value is None else [str(value)])
    return normalized


def build_local_multimodal_content(
    event: dict[str, Any], context: dict[str, Any], contact_sheet: Path | None
) -> list[dict[str, Any]]:
    schema = {field: "required" for field in sorted(MULTIMODAL_REQUIRED_FIELDS)}
    prompt = (
        "Analyze this elder-care event by comparing posture, position, and motion across "
        "T-4s, T-2s, T, T+2s, and T+4s. Cross-check the visual sequence against sensor "
        "and device evidence. Risk may only stay unchanged or increase. Do not output device "
        "control commands. Return only one JSON object.\n"
        + json.dumps({"schema": schema, "event": event, "context": _compact_value(context)}, ensure_ascii=False, default=str)
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if contact_sheet and contact_sheet.is_file():
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(contact_sheet)}})
    return content


def build_cloud_multimodal_content(
    event: dict[str, Any], local_result: dict[str, Any], context: dict[str, Any], image_paths: list[Path]
) -> list[dict[str, Any]]:
    prompt = (
        "Review this elder-care risk after local handling. Risk may only stay unchanged or "
        "increase. Do not output device control commands. Return only the strict multimodal "
        "JSON fields. Images are ordered by event-relative time.\n"
        + json.dumps(
            {"event": event, "local_result": local_result, "context": _compact_value(context)},
            ensure_ascii=False,
            default=str,
        )
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths[:5]:
        if path.is_file():
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(path)}})
    return content


class LocalMultimodalClient:
    async def analyze(self, *, event: dict[str, Any], context: dict[str, Any], contact_sheet: Path | None) -> dict[str, Any]:
        if settings.llm_mock:
            return {
                "event_semantics": event.get("summary", "mock visual analysis"),
                "risk_level": event.get("risk_level", "P4"),
                "confidence": 0.5,
                "temporal_changes": [],
                "supporting_evidence": [],
                "contradictions": [],
                "missing_information": [],
                "recommended_followup": [],
                "family_summary": event.get("summary", ""),
                "mock": True,
            }
        content = build_local_multimodal_content(event, context, contact_sheet)
        body = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": settings.llm_max_tokens,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        async with httpx.AsyncClient(timeout=settings.llm_timeout_sec) as client:
            response = await client.post(
                settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=body,
            )
            response.raise_for_status()
        output = _extract_json_object(response.json()["choices"][0]["message"].get("content") or "")
        return _normalize_multimodal_output({"event": event}, output)


class CloudLLMClient:
    async def review(
        self,
        *,
        event: dict[str, Any],
        local_result: dict[str, Any],
        context: dict[str, Any],
        image_paths: list[Path],
    ) -> dict[str, Any]:
        if not settings.cloud_llm_enabled:
            return {"status": "disabled"}
        if not settings.cloud_llm_base_url or not settings.cloud_llm_model:
            return {"status": "misconfigured"}
        content = build_cloud_multimodal_content(event, local_result, context, image_paths)
        body = {
            "model": settings.cloud_llm_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": settings.llm_max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=settings.cloud_llm_timeout_sec) as client:
                response = await client.post(
                    settings.cloud_llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.cloud_llm_api_key}"},
                    json=body,
                )
                response.raise_for_status()
            output = _extract_json_object(response.json()["choices"][0]["message"].get("content") or "")
            normalized = _normalize_multimodal_output({"event": {**event, "risk_level": local_result.get("risk_level", event.get("risk_level"))}}, output)
            return {"status": "completed", **normalized}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
