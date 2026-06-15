from __future__ import annotations

import json
import base64
import re
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
EVENT_MINIMUM_RISK = {
    "gas_leak": "P0",
    "spo2_low": "P1",
    "heart_rate_abnormal": "P1",
    "suspected_fall": "P1",
    "long_static": "P2",
    "night_abnormal_activity": "P2",
    "co2_high": "P3",
    "temperature_high": "P3",
    "temperature_low": "P3",
    "humidity_abnormal": "P3",
    "normal": "P4",
}
RISK_POLICY_PROMPT = (
    "风险等级政策：P0=紧急危险，包括燃气异常、血氧低于88%或有明确证据的即时生命危险；"
    "P1=高风险，包括疑似跌倒、血氧88%至91%、心率低于45或高于130；"
    "P2=中风险，包括长时间静止、北京时间22:00至次日06:00卧室持续无人5分钟的夜间异常活动；"
    "P3=低风险，包括CO2、温度、湿度等环境异常；P4=未发现风险，仅记录。"
    "输入事件的rule_risk_level以及上述事件类型等级都是最低风险等级。模型只能保持或升级，不能因画面模糊、"
    "证据矛盾、置信度较低或老人之后起身而降级。suspected_fall只能输出P1或P0；"
    "只有存在明确即时生命危险证据时才升级到P0，不能因一般担忧升级。"
)
OUTPUT_CONSTRAINTS_PROMPT = (
    "只输出一个合法JSON对象，不输出Markdown、思维过程、解释、工具调用或设备控制命令。"
    "event_semantics最多20个中文字符，family_summary最多30个中文字符；"
    "所有数组最多2项，每项最多20个中文字符。confidence是0到1之间的数字。"
    "risk_level必须且只能是P0、P1、P2、P3、P4中的一个精确字符串；"
    "禁止输出P1,P2、P1/P2、等级数组、等级范围或附加解释。"
)
MULTIMODAL_SYSTEM_PROMPT = (
    "你是居家老人安全系统的结构化分析器。必须先分析输入证据，再生成结论。"
    "只返回包含规定结果字段的顶层JSON对象。不要返回required_fields、output_template、schema或输入数据。"
    "不要照抄事件类型、检测器姿态或运动状态；它们只是需要与图片和传感器交叉验证的线索。"
)
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
    def __init__(
        self,
        message: str,
        *,
        raw_model_content: str | None = None,
        parsed_model_output: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_model_content = raw_model_content
        self.parsed_model_output = parsed_model_output


def _extract_named_json_object(content: str, field: str) -> dict[str, Any] | None:
    marker = f'"{field}"'
    marker_index = content.find(marker)
    if marker_index < 0:
        return None
    start = content.find("{", marker_index + len(marker))
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(content)):
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(content[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _extract_completed_multimodal_prefix(content: str) -> dict[str, Any] | None:
    """Recover a complete result followed by a truncated echo of the input."""
    for match in re.finditer(r',\s*"(?:event|sensor_evidence|device_states)"\s*:', content):
        try:
            parsed = json.loads(content[: match.start()] + "}")
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and MULTIMODAL_REQUIRED_FIELDS.issubset(parsed):
            return parsed
    return None


def _extract_json_object(content: str) -> dict[str, Any]:
    if not content.strip():
        raise LLMOutputError("LLM returned empty content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        nested = _extract_named_json_object(content, "output_template")
        if nested is not None:
            return {"output_template": nested}
        completed_prefix = _extract_completed_multimodal_prefix(content)
        if completed_prefix is not None:
            return completed_prefix
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


def _event_type(payload: dict[str, Any]) -> str | None:
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("event_type") is None:
        return None
    return str(event["event_type"]).split(".")[-1].lower()


def _minimum_allowed_risk(payload: dict[str, Any]) -> str:
    rule_risk = _event_risk_level(payload) or "P4"
    event_floor = EVENT_MINIMUM_RISK.get(_event_type(payload) or "", "P4")
    return event_floor if RISK_ORDER[event_floor] > RISK_ORDER.get(rule_risk, 0) else rule_risk


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
    nested = output.get("output_template")
    if isinstance(nested, dict) and MULTIMODAL_REQUIRED_FIELDS.issubset(nested):
        output = nested
    missing = sorted(MULTIMODAL_REQUIRED_FIELDS - output.keys())
    if missing:
        raise LLMOutputError(f"multimodal output missing required fields: {', '.join(missing)}")
    if _contains_forbidden_action_key(output):
        raise LLMOutputError("multimodal output contains forbidden device control fields")
    original = _minimum_allowed_risk(payload)
    reviewed = str(output.get("risk_level", original)).split(".")[-1].upper()
    if reviewed not in RISK_ORDER:
        raise LLMOutputError(f"invalid risk level: {reviewed}")
    if RISK_ORDER[reviewed] < RISK_ORDER[original]:
        raise LLMOutputError(f"model attempted to downgrade risk from {original} to {reviewed}")
    normalized = dict(output)
    normalized["risk_level"] = reviewed
    try:
        normalized["confidence"] = max(0.0, min(float(output.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError) as exc:
        raise LLMOutputError("multimodal confidence must be a number") from exc
    repaired_fields: list[str] = []
    for field in ["temporal_changes", "supporting_evidence", "contradictions", "missing_information", "recommended_followup"]:
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = [value] if value.strip() else []
            repaired_fields.append(field)
        elif value is None:
            normalized[field] = []
            repaired_fields.append(field)
        elif isinstance(value, list):
            if len(value) > 2:
                raise LLMOutputError(f"multimodal field {field} contains more than 2 items")
            normalized[field] = [str(item) for item in value if str(item).strip()]
        else:
            raise LLMOutputError(f"multimodal field {field} must be an array of strings")
    for field in ["event_semantics", "family_summary"]:
        if not isinstance(normalized.get(field), str):
            raise LLMOutputError(f"multimodal field {field} must be a string")
    if repaired_fields:
        normalized["schema_repaired_fields"] = repaired_fields
    return normalized


def _normalize_multimodal_response(payload: dict[str, Any], raw_content: str) -> dict[str, Any]:
    parsed_output: dict[str, Any] | None = None
    try:
        parsed_output = _extract_json_object(raw_content)
        return _normalize_multimodal_output(payload, parsed_output)
    except LLMOutputError as exc:
        raise LLMOutputError(
            str(exc),
            raw_model_content=raw_content,
            parsed_model_output=parsed_output,
        ) from exc


def _compact_local_case(event: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    rule_payload = event.get("rule_trace", {}).get("payload", {}) if isinstance(event.get("rule_trace"), dict) else {}
    compact_event = {
        "event_type": event.get("event_type"),
        "rule_risk_level": event.get("rule_risk_level") or event.get("risk_level"),
        "room": event.get("room"),
        "summary": event.get("summary"),
        "detector_confidence": event.get("confidence"),
        "posture": rule_payload.get("posture"),
        "motion_state": rule_payload.get("motion_state"),
    }
    sensor_keys = {
        "heart_rate",
        "spo2",
        "temperature",
        "humidity",
        "co2_ppm",
        "gas_ppm",
        "smoke_ppm",
        "room",
        "present",
        "device",
        "state",
    }
    sensor_wrapper = context.get("sensors", {})
    observations = sensor_wrapper.get("observations", []) if isinstance(sensor_wrapper, dict) else []
    sensor_evidence = []
    for observation in observations[:5]:
        if not isinstance(observation, dict):
            continue
        raw_payload = observation.get("payload", {})
        compact_payload = (
            {key: raw_payload[key] for key in sensor_keys if key in raw_payload}
            if isinstance(raw_payload, dict)
            else {}
        )
        sensor_evidence.append({"kind": observation.get("kind"), "payload": compact_payload})
    device_wrapper = context.get("devices", {})
    devices = device_wrapper.get("devices", []) if isinstance(device_wrapper, dict) else []
    device_states = [
        {key: device.get(key) for key in ("room", "device", "state", "value") if key in device}
        for device in devices[:8]
        if isinstance(device, dict)
    ]
    return {"event": compact_event, "sensor_evidence": sensor_evidence, "device_states": device_states}


def _multimodal_schema() -> dict[str, Any]:
    return {
        "required_fields": {
            "event_semantics": "string",
            "risk_level": {
                "type": "string",
                "allowed_values": ["P0", "P1", "P2", "P3", "P4"],
                "exactly_one": True,
            },
            "confidence": "number between 0 and 1",
            "temporal_changes": "array of strings",
            "supporting_evidence": "array of strings",
            "contradictions": "array of strings",
            "missing_information": "array of strings",
            "recommended_followup": "array of strings",
            "family_summary": "string",
        }
    }


def build_local_multimodal_content(
    event: dict[str, Any], context: dict[str, Any], contact_sheet: Path | None
) -> list[dict[str, Any]]:
    if contact_sheet and contact_sheet.is_file():
        analysis_instruction = (
            "这是视觉事件。按时间顺序比较联系表中的T-2、T-1、T、T+1、T+2五个位置，重点分析人体姿态、"
            "身体高度、画面位置、支撑物和运动连续性。区分突然倒地、正常坐下、主动躺卧、弯腰、遮挡和静止。"
            "不得仅根据event_type、posture或motion_state重复结论，必须与图片和传感器交叉验证。"
            "图像与触发线索矛盾时写入contradictions，不得通过输出多个风险等级表达不确定性。"
            "若无法确认，将疑点写入contradictions或missing_information，但必须保留事件最低风险等级。"
            "结合传感器和设备证据判断，不要仅凭单帧姿态下结论。"
        )
    else:
        analysis_instruction = (
            "这是非视觉事件，没有图片。只分析事件、传感器和设备上下文，不要声称看到了画面、姿态或连续动作。"
            "证据不足时写入missing_information，但必须保留事件最低风险等级。"
        )
    prompt = (
        "你是RK3588本地老人安全事件分析器。先遵守确定性规则，再进行语义分析。"
        + RISK_POLICY_PROMPT
        + analysis_instruction
        + OUTPUT_CONSTRAINTS_PROMPT
        + "必须完整输出required_fields列出的结果字段，但不要输出required_fields本身，不重复事实。\n"
        + json.dumps(
            {**_multimodal_schema(), **_compact_local_case(event, context)},
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if contact_sheet and contact_sheet.is_file():
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(contact_sheet)}})
    return content


def build_cloud_multimodal_content(
    event: dict[str, Any],
    local_result: dict[str, Any],
    context: dict[str, Any],
    image_frames: list[tuple[int, Path]],
) -> list[dict[str, Any]]:
    has_images = any(path.is_file() for _, path in image_frames[:5])
    modality_instruction = (
        "这是视觉事件。独立复核各张原始关键帧；每张图片前的文字是相对触发时刻的真实标签，缺失时间点不会补图。"
        "比较姿态、身体高度、位置和动作变化，区分突然倒地与正常坐下、主动躺卧、弯腰。"
        if has_images
        else "这是非视觉事件，没有图片。仅复核结构化传感器证据、规则结果和本地模型摘要。"
    )
    prompt = (
        "你是云端老人安全风险复核模型。规则处置和本地处置已经执行；你只能独立复核、升级风险、补充证据和家属说明，"
        "不能降级风险、撤销既有动作或直接控制设备。"
        + RISK_POLICY_PROMPT
        + modality_instruction
        + OUTPUT_CONSTRAINTS_PROMPT
        + "必须完整输出required_fields列出的结果字段，但不要输出required_fields本身。\n"
        + json.dumps(
            {
                **_multimodal_schema(),
                "event": event,
                "local_result": local_result,
                "context": _compact_value(context),
            },
            ensure_ascii=False,
            default=str,
        )
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for offset_ms, path in image_frames[:5]:
        if path.is_file():
            seconds = offset_ms / 1000
            label = "T" if offset_ms == 0 else f"T{seconds:+g}s"
            content.append({"type": "text", "text": f"关键帧时间标签：{label}（offset_ms={offset_ms}）"})
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
            "messages": [
                {"role": "system", "content": MULTIMODAL_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
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
        raw_content = response.json()["choices"][0]["message"].get("content") or ""
        return _normalize_multimodal_response({"event": event}, raw_content)


class CloudLLMClient:
    async def review(
        self,
        *,
        event: dict[str, Any],
        local_result: dict[str, Any],
        context: dict[str, Any],
        image_frames: list[tuple[int, Path]],
    ) -> dict[str, Any]:
        if not settings.cloud_llm_enabled:
            return {"status": "disabled"}
        if not settings.cloud_llm_base_url or not settings.cloud_llm_model:
            return {"status": "misconfigured"}
        content = build_cloud_multimodal_content(event, local_result, context, image_frames)
        body = {
            "model": settings.cloud_llm_model,
            "messages": [
                {"role": "system", "content": MULTIMODAL_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "max_tokens": settings.llm_max_tokens,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        raw_content = ""
        parsed_output: dict[str, Any] | None = None
        try:
            async with httpx.AsyncClient(timeout=settings.cloud_llm_timeout_sec) as client:
                response = await client.post(
                    settings.cloud_llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.cloud_llm_api_key}"},
                    json=body,
                )
                response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            raw_content = message.get("content") or message.get("reasoning_content") or ""
            parsed_output = _extract_json_object(raw_content)
            normalized = _normalize_multimodal_output(
                {"event": {**event, "risk_level": local_result.get("risk_level", event.get("risk_level"))}},
                parsed_output,
            )
            return {"status": "completed", **normalized}
        except Exception as exc:
            result: dict[str, Any] = {"status": "failed", "error": str(exc)}
            error_raw = getattr(exc, "raw_model_content", None) or raw_content
            error_parsed = getattr(exc, "parsed_model_output", None) or parsed_output
            if error_raw:
                result["rejected_model_content"] = error_raw
            if error_parsed is not None:
                result["rejected_model_output"] = error_parsed
            return result
