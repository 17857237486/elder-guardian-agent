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
    "co2_high": "P3",
    "temperature_high": "P3",
    "temperature_low": "P3",
    "humidity_abnormal": "P3",
    "normal": "P4",
}
RISK_POLICY_PROMPT = (
    "风险等级政策：P0=紧急危险，包括燃气异常、血氧低于88%或有明确证据的即时生命危险；"
    "P1=高风险，包括疑似跌倒、血氧88%至91%、心率低于45或高于130；"
    "P2=中风险，包括长时间静止；"
    "P3=低风险，包括CO2、温度、湿度等环境异常；P4=未发现风险，仅记录。"
    "输入事件的rule_risk_level以及上述事件类型等级都是最低风险等级。模型只能保持或升级，不能因画面模糊、"
    "证据矛盾、置信度较低或老人之后起身而降级。suspected_fall只能输出P1或P0；"
    "只有存在明确即时生命危险证据时才升级到P0，不能因一般担忧升级。"
)
LOCAL_RISK_POLICY_PROMPT = (
    "风险含义：P0即时生命危险，P1严重人身风险，P2需关注，P3环境轻度异常，P4无风险。"
    "风险只能保持或升级minimum_risk_level；仅有明确即时生命危险证据才可升级到P0。"
)
LOCAL_OUTPUT_CONTRACT = (
    "只输出顶层JSON，不输出Markdown、解释、输入数据或控制命令。字段："
    "event_semantics:string,risk_level:P0|P1|P2|P3|P4,confidence:0..1,"
    "supporting_evidence:string[<=2],family_summary:string。"
    "event_semantics和family_summary都用短中文，不超过28字；supporting_evidence每项不超过18字。"
    "risk_level必须是单一值。"
)
LOCAL_VISUAL_INSTRUCTION = (
    "视觉：比较T-1、T、T+1的姿态、高度、位置、支撑和动作连续性；"
    "区分跌倒、坐下、主动躺卧、弯腰、遮挡和静止。触发参数仅是线索，须与图像和传感器交叉验证；"
    "证据不足时降低confidence，但不得降低minimum_risk_level。"
)
LOCAL_TEXT_INSTRUCTION = (
    "非视觉：只分析事件、传感器和设备上下文，不得声称看到图像；证据不足写入missing_information。"
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
LOCAL_MULTIMODAL_REQUIRED_FIELDS = {
    "event_semantics",
    "risk_level",
    "confidence",
    "supporting_evidence",
    "family_summary",
}
CANDIDATE_LOCAL_REQUIRED_FIELDS = {
    "event_semantics",
    "risk_level",
    "confidence",
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


def _repair_truncated_top_level_object(content: str) -> dict[str, Any] | None:
    """Recover a JSON object that is complete except for the final closing brace."""
    start = content.find("{")
    if start < 0:
        return None
    fragment = content[start:].strip()
    if fragment.endswith("}"):
        return None

    depth = 0
    in_string = False
    escaped = False
    for char in fragment:
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
    if in_string or depth != 1:
        return None
    try:
        parsed = json.loads(fragment + "}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


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
        repaired_top_level = _repair_truncated_top_level_object(content)
        if repaired_top_level is not None:
            return repaired_top_level
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


def _is_candidate_payload(payload: dict[str, Any]) -> bool:
    event = payload.get("event")
    return isinstance(event, dict) and str(event.get("source_kind") or "") == "ai_review_candidate"


def _candidate_local_input(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("context")
    if isinstance(context, dict) and isinstance(context.get("candidate_local_input"), dict):
        return context["candidate_local_input"]
    return {}


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spo2_candidate_adjusted_risk(payload: dict[str, Any], reviewed: str) -> tuple[str, str | None]:
    if reviewed not in {"P0", "P1"} or not _is_candidate_payload(payload):
        return reviewed, None
    compact = _candidate_local_input(payload)
    if str(compact.get("t") or "") != "vital_baseline_anomaly":
        return reviewed, None
    if str(compact.get("metric") or "") != "spo2" or str(compact.get("dir") or "") != "low":
        return reviewed, None

    latest = _number(compact.get("latest"))
    minimum = _number(compact.get("min"))
    p10 = _number(compact.get("p10"))
    baseline_low = _number(compact.get("bp10"))
    sample_count = _number(compact.get("n")) or 0
    observed_low = min(value for value in (latest, minimum, p10) if value is not None) if any(
        value is not None for value in (latest, minimum, p10)
    ) else None

    if observed_low is None:
        return "P2", "spo2_candidate_missing_numeric_evidence"
    if observed_low < 92:
        return reviewed, None
    if baseline_low is None:
        return "P2", "spo2_candidate_missing_baseline"
    if latest is not None and latest >= baseline_low:
        return "P2", "spo2_candidate_not_below_low_reference"
    drop = baseline_low - observed_low
    if sample_count >= 24 and drop >= 2.0:
        return reviewed, None
    return "P2", "spo2_candidate_drop_not_severe_enough_for_p1"


def _minimum_allowed_risk(payload: dict[str, Any]) -> str:
    rule_risk = _event_risk_level(payload) or "P4"
    event_floor = EVENT_MINIMUM_RISK.get(_event_type(payload) or "", "P4")
    return event_floor if RISK_ORDER[event_floor] > RISK_ORDER.get(rule_risk, 0) else rule_risk


def _text_contains_any(text: str, terms: set[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _contains_unnegated_danger(text: str, terms: set[str]) -> bool:
    lowered = text.lower()
    negation_prefixes = ("无", "未见", "没有", "无明显", "未发现", "not ", "no ")
    for term in terms:
        marker = term.lower()
        start = 0
        while True:
            index = lowered.find(marker, start)
            if index < 0:
                break
            prefix = lowered[max(0, index - 6):index]
            if not any(prefix.endswith(item.lower()) for item in negation_prefixes):
                return True
            start = index + len(marker)
    return False


def _latest_vital_sample_from_context(context: dict[str, Any]) -> dict[str, Any] | None:
    recent = context.get("recent_vital_samples")
    if isinstance(recent, dict):
        samples = recent.get("samples")
        if isinstance(samples, list):
            for sample in reversed(samples):
                if isinstance(sample, dict):
                    return sample
    sensors = context.get("sensors")
    observations = sensors.get("observations") if isinstance(sensors, dict) else None
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, dict) or str(observation.get("kind") or "") != "vital":
                continue
            payload = observation.get("payload")
            if isinstance(payload, dict):
                return payload
    return None


def _baseline_metric(context: dict[str, Any], baseline_type: str, key: str) -> float | None:
    baseline_context = context.get("baseline_context")
    baselines = baseline_context.get("baselines") if isinstance(baseline_context, dict) else None
    if not isinstance(baselines, list):
        return None
    for baseline in baselines:
        if not isinstance(baseline, dict) or str(baseline.get("baseline_type") or "") != baseline_type:
            continue
        metrics = baseline.get("metrics") or baseline.get("metrics_json")
        if not isinstance(metrics, dict):
            continue
        value = _number(metrics.get(key))
        if value is not None:
            return value
    return None


def _context_vitals_support_rest(payload: dict[str, Any]) -> bool:
    context = payload.get("context")
    if not isinstance(context, dict):
        return False
    latest = _latest_vital_sample_from_context(context)
    if not latest:
        return False
    heart_rate = _number(latest.get("heart_rate"))
    spo2 = _number(latest.get("spo2"))
    if heart_rate is None or spo2 is None:
        return False
    if heart_rate < 45 or heart_rate > 130 or spo2 < 92:
        return False
    baseline_low = _baseline_metric(context, "heart_rate_daily", "p10")
    baseline_high = _baseline_metric(context, "heart_rate_daily", "p90")
    spo2_low = _baseline_metric(context, "spo2_daily", "p10")
    if baseline_low is not None and heart_rate < baseline_low:
        return False
    if baseline_high is not None and heart_rate > baseline_high:
        return False
    if spo2_low is not None and spo2 < spo2_low:
        return False
    return True


def _long_static_rest_downgrade_supported(payload: dict[str, Any], output: dict[str, Any] | None) -> bool:
    if not isinstance(output, dict):
        return False
    evidence = output.get("supporting_evidence")
    evidence_text = " ".join(str(item) for item in evidence) if isinstance(evidence, list) else str(evidence or "")
    text = " ".join(
        str(output.get(field) or "")
        for field in ("event_semantics", "family_summary")
    )
    text = f"{text} {evidence_text}"
    danger_terms = {"跌倒", "摔倒", "倒地", "疼痛", "痛苦", "呼吸困难", "无法移动", "呼救", "危险"}
    if _contains_unnegated_danger(text, danger_terms):
        return False
    vital_normal_terms = {"生命体征正常", "生命体征平稳", "心率正常", "血氧正常", "vitals normal", "vital signs normal"}
    visual_normal_terms = {
        "视觉无异常",
        "姿态正常",
        "姿态稳定",
        "姿态自然",
        "自然睡姿",
        "无异常姿态",
        "无跌倒",
        "无痛苦表情",
        "无疼痛表情",
        "表情正常",
        "表情平静",
        "面部放松",
        "stable posture",
        "normal expression",
    }
    rest_terms = {
        "正常休息",
        "休息状态",
        "短暂静止",
        "安静坐卧",
        "正常坐卧",
        "睡觉",
        "睡眠",
        "入睡",
        "闭眼休息",
        "静卧休息",
        "resting",
        "sleeping",
    }
    visual_support = _text_contains_any(text, visual_normal_terms) and _text_contains_any(text, rest_terms)
    vital_support = _text_contains_any(text, vital_normal_terms) or _context_vitals_support_rest(payload)
    return visual_support and vital_support


def _allows_local_long_static_downgrade(payload: dict[str, Any], reviewed: str, output: dict[str, Any] | None = None) -> bool:
    if _is_candidate_payload(payload):
        return False
    return _event_type(payload) == "long_static" and reviewed == "P4" and _long_static_rest_downgrade_supported(payload, output)


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


def _compact_environment_samples(samples: Any) -> list[Any]:
    if not isinstance(samples, list):
        return []
    return [_compact_value(item) for item in samples[:20]]


def _compact_vital_samples(samples: Any) -> list[Any]:
    if not isinstance(samples, list):
        return []
    return [_compact_value(item) for item in samples[:20]]


def _compact_samples(samples: Any, limit: int) -> list[Any]:
    if not isinstance(samples, list):
        return []
    return [_compact_value(item) for item in samples[:limit]]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _cloud_vital_summary(samples: list[Any], baseline_context: dict[str, Any]) -> dict[str, Any]:
    compact_samples = [item for item in samples if isinstance(item, dict)]
    heart_values = [_num(item.get("heart_rate")) for item in compact_samples]
    heart_values = [value for value in heart_values if value is not None]
    spo2_values = [_num(item.get("spo2")) for item in compact_samples]
    spo2_values = [value for value in spo2_values if value is not None]
    heart_baseline = baseline_context.get("heart_rate_daily") if isinstance(baseline_context.get("heart_rate_daily"), dict) else {}
    spo2_baseline = baseline_context.get("spo2_daily") if isinstance(baseline_context.get("spo2_daily"), dict) else {}
    heart_metrics = heart_baseline.get("metrics") if isinstance(heart_baseline.get("metrics"), dict) else {}
    spo2_metrics = spo2_baseline.get("metrics") if isinstance(spo2_baseline.get("metrics"), dict) else {}
    heart_low = _num(heart_metrics.get("p10"))
    heart_high = _num(heart_metrics.get("p90"))
    spo2_low = _num(spo2_metrics.get("p10"))
    latest = compact_samples[-1] if compact_samples else {}
    latest_heart = _num(latest.get("heart_rate"))
    latest_spo2 = _num(latest.get("spo2"))
    max_heart = max(heart_values) if heart_values else None
    min_heart = min(heart_values) if heart_values else None
    min_spo2 = min(spo2_values) if spo2_values else None
    return {
        "heart_rate": {
            "latest": latest_heart,
            "min": min_heart,
            "max": max_heart,
            "avg": _avg(heart_values),
            "baseline_low_ref": heart_low,
            "baseline_high_ref": heart_high,
            "status": (
                "above_personal_high_ref"
                if heart_high is not None and max_heart is not None and max_heart > heart_high
                else "below_personal_low_ref"
                if heart_low is not None and min_heart is not None and min_heart < heart_low
                else "within_personal_ref"
                if heart_values
                else "no_data"
            ),
        },
        "spo2": {
            "latest": latest_spo2,
            "min": min_spo2,
            "avg": _avg(spo2_values),
            "baseline_low_ref": spo2_low,
            "status": (
                "below_personal_low_ref"
                if spo2_low is not None and min_spo2 is not None and min_spo2 < spo2_low
                else "within_personal_ref"
                if spo2_values
                else "no_data"
            ),
        },
    }


def _cloud_sensor_context_summary(context: dict[str, Any]) -> dict[str, Any]:
    environment_context = context.get("environment_context") if isinstance(context.get("environment_context"), dict) else {}
    recent_vital = context.get("recent_vital_samples") if isinstance(context.get("recent_vital_samples"), dict) else {}
    elder_location = context.get("elder_location") if isinstance(context.get("elder_location"), dict) else {}
    baseline_context = context.get("baseline_context") if isinstance(context.get("baseline_context"), dict) else {}
    vital_samples = _compact_samples(recent_vital.get("samples", []), 30)
    compact_baseline = _compact_baseline_context(baseline_context)
    return {
        "elder_location": _compact_value(elder_location),
        "environment": {
            "actual_samples": environment_context.get("actual_samples"),
            "room_sequence": environment_context.get("room_sequence", []),
            "samples": _compact_samples(environment_context.get("samples", []), 30),
        },
        "vital": {
            "actual_samples": recent_vital.get("actual_samples"),
            "samples": vital_samples,
            "summary": _cloud_vital_summary(vital_samples, compact_baseline),
        },
        "baseline": compact_baseline,
    }


def _compact_segment_summary(segment: Any) -> dict[str, Any] | None:
    if not isinstance(segment, dict):
        return None
    features = segment.get("features") if isinstance(segment.get("features"), dict) else {}
    return {
        "segment_id": segment.get("segment_id"),
        "segment_type": segment.get("segment_type"),
        "start_at": segment.get("start_at"),
        "end_at": segment.get("end_at"),
        "duration_seconds": segment.get("duration_seconds"),
        "room": segment.get("room"),
        "status": segment.get("status"),
        "features": {
            key: features.get(key)
            for key in (
                "rooms",
                "returned_to_bedroom",
                "bathroom_stay_seconds",
                "night_key",
                "metric",
                "avg",
                "min",
                "max",
                "p10",
                "p50",
                "p90",
                "latest_value",
                "sample_count",
                "abnormal_count",
            )
            if key in features
        },
    }


def _compact_baseline_context(baseline_context: Any) -> dict[str, Any]:
    if not isinstance(baseline_context, dict):
        return {}
    metric_keys = {
        "usual_sleep_start",
        "usual_sleep_end",
        "night_wake_count_p90",
        "night_wake_duration_p90_sec",
        "returned_to_bedroom_rate",
        "bathroom_stay_p90_sec",
        "night_bathroom_visits_avg",
        "daily_avg",
        "night_avg",
        "avg",
        "p10",
        "p50",
        "p90",
        "low_count_avg_per_day",
        "fallback",
    }
    compact: dict[str, Any] = {}
    for key, item in baseline_context.items():
        if not isinstance(item, dict):
            continue
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        compact[str(key)] = {
            "quality": item.get("quality"),
            "sample_count": item.get("sample_count"),
            "metrics": {metric: metrics.get(metric) for metric in metric_keys if metric in metrics},
        }
    return compact


def _compact_candidate(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    features = candidate.get("features") if isinstance(candidate.get("features"), dict) else {}
    compact_features = {
        key: features.get(key)
        for key in (
            "duration_seconds",
            "baseline_p90_seconds",
            "baseline_p90",
            "latest_value",
            "metric",
            "room",
            "returned_to_bedroom",
            "bathroom_stay_seconds",
        )
        if key in features
    }
    return {
        "candidate_id": candidate.get("candidate_id"),
        "candidate_type": candidate.get("candidate_type"),
        "priority": candidate.get("priority"),
        "reason": candidate.get("reason"),
        "features": compact_features,
        "source_segment_ids": candidate.get("source_segment_ids", []),
    }


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


def _normalize_multimodal_output(
    payload: dict[str, Any],
    output: dict[str, Any],
    *,
    array_limits: dict[str, int] | None = None,
) -> dict[str, Any]:
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
    limits = {
        "temporal_changes": 2,
        "supporting_evidence": 2,
        "contradictions": 2,
        "missing_information": 2,
        "recommended_followup": 2,
    }
    if array_limits:
        limits.update(array_limits)
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
            limit = limits[field]
            if len(value) > limit:
                value = value[:limit]
                repaired_fields.append(field)
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


def _normalize_local_multimodal_output(payload: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    nested = output.get("output_template")
    if isinstance(nested, dict) and (
        LOCAL_MULTIMODAL_REQUIRED_FIELDS.issubset(nested) or CANDIDATE_LOCAL_REQUIRED_FIELDS.issubset(nested)
    ):
        output = nested
    candidate_mode = _is_candidate_payload(payload)
    required = CANDIDATE_LOCAL_REQUIRED_FIELDS if candidate_mode else LOCAL_MULTIMODAL_REQUIRED_FIELDS
    missing = sorted(required - output.keys())
    if missing:
        raise LLMOutputError(f"local multimodal output missing required fields: {', '.join(missing)}")
    if _contains_forbidden_action_key(output):
        raise LLMOutputError("local multimodal output contains forbidden device control fields")
    original = _minimum_allowed_risk(payload)
    reviewed = str(output.get("risk_level", original)).split(".")[-1].upper()
    if reviewed not in RISK_ORDER:
        raise LLMOutputError(f"invalid risk level: {reviewed}")
    if RISK_ORDER[reviewed] < RISK_ORDER[original] and not _allows_local_long_static_downgrade(payload, reviewed, output):
        raise LLMOutputError(f"model attempted to downgrade risk from {original} to {reviewed}")
    reviewed, risk_adjustment = _spo2_candidate_adjusted_risk(payload, reviewed)
    evidence = output.get("supporting_evidence")
    repaired_fields: list[str] = []
    try:
        confidence = max(0.0, min(float(output.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError) as exc:
        if not candidate_mode:
            raise LLMOutputError("local multimodal confidence must be a number") from exc
        confidence_text = str(output.get("confidence") or "").strip().lower()
        confidence_map = {"high": 0.8, "medium": 0.6, "med": 0.6, "low": 0.4}
        if confidence_text not in confidence_map:
            raise LLMOutputError("local multimodal confidence must be a number") from exc
        confidence = confidence_map[confidence_text]
        repaired_fields.append("confidence")
    if isinstance(evidence, str):
        evidence = [evidence] if evidence.strip() else []
        repaired_fields.append("supporting_evidence")
    elif evidence is None:
        if candidate_mode:
            event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            fallback_evidence = str(event.get("summary") or "candidate reason").strip()
            evidence = [fallback_evidence] if fallback_evidence else []
        else:
            evidence = []
        repaired_fields.append("supporting_evidence")
    elif not isinstance(evidence, list):
        raise LLMOutputError("local multimodal supporting_evidence must be an array of strings")
    if len(evidence) > 2:
        raise LLMOutputError("local multimodal supporting_evidence contains more than 2 items")
    for field in ("event_semantics", "family_summary"):
        if not isinstance(output.get(field), str):
            raise LLMOutputError(f"local multimodal field {field} must be a string")
    normalized = {
        "event_semantics": output["event_semantics"],
        "risk_level": reviewed,
        "confidence": confidence,
        "temporal_changes": [],
        "supporting_evidence": [str(item) for item in evidence if str(item).strip()],
        "contradictions": [],
        "missing_information": [],
        "recommended_followup": [],
        "family_summary": output["family_summary"],
    }
    if _allows_local_long_static_downgrade(payload, reviewed, output):
        normalized["risk_guardrail_adjustment"] = "long_static_local_downgrade_to_p4"
    if risk_adjustment:
        normalized["risk_guardrail_adjustment"] = risk_adjustment
        repaired_fields.append("risk_level")
    if repaired_fields:
        normalized["schema_repaired_fields"] = repaired_fields
    return normalized


def _normalize_local_multimodal_response(payload: dict[str, Any], raw_content: str) -> dict[str, Any]:
    parsed_output: dict[str, Any] | None = None
    try:
        parsed_output = _extract_json_object(raw_content)
        return _normalize_local_multimodal_output(payload, parsed_output)
    except LLMOutputError as exc:
        raise LLMOutputError(
            str(exc),
            raw_model_content=raw_content,
            parsed_model_output=parsed_output,
        ) from exc


def _compact_local_case(event: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    candidate_local_input = context.get("candidate_local_input")
    if isinstance(candidate_local_input, dict):
        return {"candidate_review": _compact_value(candidate_local_input)}

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
    environment_context = context.get("environment_context") if isinstance(context.get("environment_context"), dict) else {}
    elder_location = context.get("elder_location") if isinstance(context.get("elder_location"), dict) else {}
    compact_environment_context = {
        "target_samples": environment_context.get("target_samples"),
        "actual_samples": environment_context.get("actual_samples"),
        "selection_policy": environment_context.get("selection_policy"),
        "room_sequence": environment_context.get("room_sequence", []),
        "samples": _compact_environment_samples(environment_context.get("samples", [])),
    }
    recent_vital = context.get("recent_vital_samples") if isinstance(context.get("recent_vital_samples"), dict) else {}
    behavior_context = context.get("behavior_context") if isinstance(context.get("behavior_context"), dict) else {}
    baseline_context = context.get("baseline_context") if isinstance(context.get("baseline_context"), dict) else {}
    candidate = _compact_candidate(context.get("candidate"))
    is_vision_event = str(event.get("source_kind") or "").lower() == "vision"
    if is_vision_event:
        vital_samples = _compact_vital_samples(recent_vital.get("samples", []))
        environment_samples = _compact_environment_samples(environment_context.get("samples", []))
        return {
            "event": compact_event,
            "vision_context": _compact_value(context.get("vision_context", {})),
            "latest_vital_sample": vital_samples[-1] if vital_samples else None,
            "latest_environment_sample": environment_samples[-1] if environment_samples else None,
            "elder_location": _compact_value(elder_location),
        }
    return {
        "event": compact_event,
        "candidate": candidate,
        "elder_location": _compact_value(elder_location),
        "environment_context": compact_environment_context,
        "recent_vital_samples": {
            "target_samples": recent_vital.get("target_samples"),
            "actual_samples": recent_vital.get("actual_samples"),
            "samples": _compact_vital_samples(recent_vital.get("samples", [])),
        },
        "behavior_context": {
            "night_wake": _compact_segment_summary(behavior_context.get("night_wake")),
            "bathroom_stay": _compact_segment_summary(behavior_context.get("bathroom_stay")),
            "room_sequence": _compact_value(behavior_context.get("room_sequence", [])),
            "recent_segments": [
                item
                for item in (
                    _compact_segment_summary(segment)
                    for segment in (behavior_context.get("recent_segments", []) if isinstance(behavior_context.get("recent_segments"), list) else [])[:6]
                )
                if item
            ],
            "candidate_segments": [
                item
                for item in (
                    _compact_segment_summary(segment)
                    for segment in (
                        behavior_context.get("candidate_segments", [])
                        if isinstance(behavior_context.get("candidate_segments"), list)
                        else []
                    )[:3]
                )
                if item
            ],
        },
        "baseline_context": _compact_baseline_context(baseline_context),
        "sensor_evidence": sensor_evidence,
        "device_states": device_states,
    }


def _build_candidate_local_prompt(event: dict[str, Any], context: dict[str, Any]) -> str:
    candidate_input = context.get("candidate_local_input")
    compact = candidate_input if isinstance(candidate_input, dict) else {}
    return (
        "判断老人安全candidate候选事件是否升级。"
        "等级:P0=即时生命危险;P1=严重风险;P2=需要关注;P3=轻微记录;P4=无风险。"
        "血氧基线候选规则:spo2>=92不是硬规则低血氧;spo2>=bp10时不得仅凭基线异常输出P1/P0;"
        "spo2<bp10且>=92通常为P2,只有明显持续下降或伴随其他危险证据才可P1。"
        "只输出JSON:{event_semantics,risk_level,confidence,family_summary};"
        "risk_level只能是P0/P1/P2/P3/P4之一,禁止复合等级和设备控制。"
        "输入:"
        + json.dumps(
            {
                "t": event.get("event_type"),
                "min": event.get("risk_level", "P4"),
                "c": compact,
            },
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    )


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


def _cloud_output_constraints(temporal_limit: int) -> str:
    return (
        "只输出一个合法JSON对象，不输出Markdown、思维过程、解释、工具调用或设备控制命令。"
        "event_semantics最多20个中文字符，family_summary最多30个中文字符；"
        f"temporal_changes最多{temporal_limit}项，其他数组最多2项，每项最多20个中文字符。"
        "confidence是0到1之间的数字。risk_level必须且只能是P0、P1、P2、P3、P4中的一个精确字符串；"
        "禁止输出P1,P2、P1/P2、等级数组、等级范围或附加解释。"
    )


def build_local_multimodal_content(
    event: dict[str, Any], context: dict[str, Any], contact_sheet: Path | None
) -> list[dict[str, Any]]:
    if str(event.get("source_kind") or "") == "ai_review_candidate" or "candidate_local_input" in context:
        return [{"type": "text", "text": _build_candidate_local_prompt(event, context)}]

    has_image = bool(contact_sheet and contact_sheet.is_file())
    minimum_risk = _minimum_allowed_risk({"event": event})
    analysis_instruction = LOCAL_VISUAL_INSTRUCTION if has_image else LOCAL_TEXT_INSTRUCTION
    downgrade_note = (
        "例外：event_type=long_static时，若图像显示老人自然睡姿/闭眼休息、姿态稳定、无跌倒、无痛苦表情，且输入生命体征正常，可输出P4=休息状态；其他事件不得降级。"
        if str(event.get("event_type") or "") == "long_static"
        else ""
    )
    local_scope_note = (
        "本地模型主要分析图片；long_static可结合输入中的latest_vital_sample判断是否为正常睡觉/休息。"
        if str(event.get("event_type") or "") == "long_static"
        else "本地视觉模型只分析图片；生命体征和环境数据交给云端复核。"
    )
    prompt = (
        "分析老人安全事件，先分析证据再生成结论。"
        + LOCAL_RISK_POLICY_PROMPT
        + f"minimum_risk_level={minimum_risk}。"
        + downgrade_note
        + local_scope_note
        + analysis_instruction
        + LOCAL_OUTPUT_CONTRACT
        + "\n输入："
        + json.dumps(
            _compact_local_case(event, context),
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
    available_frame_count = sum(path.is_file() for _, path in image_frames[:5])
    has_images = available_frame_count > 0
    temporal_limit = available_frame_count or 2
    modality_instruction = (
        "这是视觉事件。独立复核各张原始关键帧；每张图片前的文字是相对触发时刻的真实标签，缺失时间点不会补图。"
        "先概括图像主证据，再结合sensor_context_summary里的最近生命体征、环境和所在房间摘要。"
        "重点识别老人异常状态：跌倒/失衡、长时间静止、疼痛或不适、呼吸或行动困难、异常支撑、姿态异常、环境危险。"
        "如果图像显示疼痛、不适或异常姿态，event_semantics、supporting_evidence和family_summary必须保留该视觉线索；"
        "生命体征正常只能说明暂未见生命体征恶化，不能覆盖或删除图像中的异常状态证据。"
        if has_images
        else "这是非视觉事件，没有图片。仅复核结构化传感器证据、规则结果和本地模型摘要。"
    )
    prompt = (
        "你是云端老人安全风险复核模型。规则处置和本地处置已经执行；你只能独立复核、升级风险、补充证据和家属说明，"
        "不能降级风险、撤销既有动作或直接控制设备。"
        + RISK_POLICY_PROMPT
        + modality_instruction
        + _cloud_output_constraints(temporal_limit)
        + "必须完整输出required_fields列出的结果字段，但不要输出required_fields本身。\n"
        + json.dumps(
            {
                **_multimodal_schema(),
                "event": event,
                "local_result": local_result,
                "sensor_context_summary": _cloud_sensor_context_summary(context),
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
        is_candidate = str(event.get("source_kind") or "") == "ai_review_candidate" or "candidate_local_input" in context
        body = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": 64 if is_candidate else min(settings.llm_max_tokens, 192),
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
        return _normalize_local_multimodal_response({"event": event, "context": context}, raw_content)


class CloudLLMClient:
    async def monthly_health_trend(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not settings.cloud_llm_enabled:
            return {"status": "cloud_disabled", "error": "CLOUD_LLM_ENABLED=false"}
        if not settings.cloud_llm_base_url or not settings.cloud_llm_model:
            return {"status": "misconfigured", "error": "cloud llm base url or model is empty"}
        local_trend = payload.get("local_trend") if isinstance(payload.get("local_trend"), dict) else {}
        daily_summaries = payload.get("daily_summaries") if isinstance(payload.get("daily_summaries"), list) else []
        instruction = (
            "你是居家老人健康守护系统的云端月度趋势复核模型。"
            "只根据给定的近30天本地统计摘要，生成家属可读中文趋势报告。"
            "不要输出设备控制命令、MQTT、用药或诊断结论。"
            "风险等级只能保持或高于本地月度趋势风险，不能降低。"
            "只输出JSON，字段为trend_status,risk_level,trend_findings,body_condition_trend,"
            "family_message,recommended_followup,data_quality_note。"
            "family_message不超过100个中文字符；trend_findings最多5项；recommended_followup最多3项。"
        )
        local_risk = str(local_trend.get("highest_risk") or "P4")
        body = {
            "model": settings.cloud_llm_model,
            "messages": [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "elder_id": payload.get("elder_id"),
                            "days": payload.get("days"),
                            "local_risk_level": local_risk,
                            "local_trend": local_trend,
                            "daily_summaries": daily_summaries[:30],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": max(settings.llm_max_tokens, 768),
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        raw_content = ""
        try:
            async with httpx.AsyncClient(timeout=settings.cloud_llm_timeout_sec) as client:
                response = await client.post(
                    settings.cloud_llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.cloud_llm_api_key}"},
                    json=body,
                )
                response.raise_for_status()
            raw_content = response.json()["choices"][0]["message"].get("content") or ""
            parsed = _extract_json_object(raw_content)
            forbidden = any(key in parsed for key in ["device_actions", "commands", "mqtt", "control"])
            if forbidden:
                raise ValueError("cloud monthly trend included forbidden device control fields")
            risk = str(parsed.get("risk_level") or local_risk)
            if risk not in RISK_ORDER:
                risk = local_risk
            if RISK_ORDER[risk] < RISK_ORDER.get(local_risk, 0):
                risk = local_risk
            findings = parsed.get("trend_findings") if isinstance(parsed.get("trend_findings"), list) else []
            followup = parsed.get("recommended_followup") if isinstance(parsed.get("recommended_followup"), list) else []
            return {
                "status": "completed",
                "trend_status": str(parsed.get("trend_status") or "已生成"),
                "risk_level": risk,
                "trend_findings": [str(item)[:50] for item in findings[:5]],
                "body_condition_trend": str(parsed.get("body_condition_trend") or "")[:200],
                "family_message": str(parsed.get("family_message") or "")[:140],
                "recommended_followup": [str(item)[:50] for item in followup[:3]],
                "data_quality_note": str(parsed.get("data_quality_note") or ""),
                "model": settings.cloud_llm_model,
            }
        except Exception as exc:
            result = {"status": "cloud_failed", "error": str(exc)}
            if raw_content:
                result["rejected_model_content"] = raw_content
            return result

    async def daily_health_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        if not settings.cloud_llm_enabled:
            return {"status": "cloud_disabled", "error": "CLOUD_LLM_ENABLED=false"}
        if not settings.cloud_llm_base_url or not settings.cloud_llm_model:
            return {"status": "misconfigured", "error": "cloud llm base url or model is empty"}
        local_stats = summary.get("local_stats") if isinstance(summary.get("local_stats"), dict) else {}
        local_risk = str(summary.get("risk_level") or local_stats.get("events", {}).get("highest_risk") or "P4")
        instruction = (
            "你是居家老人健康守护系统的云端每日摘要复核模型。"
            "只根据给定统计摘要生成家属可读中文报告，不输出设备控制命令。"
            "风险等级只能保持或高于本地统计风险，不能降低。"
            "只输出JSON，字段为overall_status,risk_level,key_findings,family_message,recommended_followup,data_quality_note。"
            "family_message不超过80个中文字符；key_findings最多4项；recommended_followup最多3项。"
        )
        body = {
            "model": settings.cloud_llm_model,
            "messages": [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "local_risk_level": local_risk,
                            "summary_date": summary.get("summary_date"),
                            "elder_id": summary.get("elder_id"),
                            "local_stats": local_stats,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": max(settings.llm_max_tokens, 512),
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        raw_content = ""
        try:
            async with httpx.AsyncClient(timeout=settings.cloud_llm_timeout_sec) as client:
                response = await client.post(
                    settings.cloud_llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.cloud_llm_api_key}"},
                    json=body,
                )
                response.raise_for_status()
            raw_content = response.json()["choices"][0]["message"].get("content") or ""
            parsed = _extract_json_object(raw_content)
            risk = str(parsed.get("risk_level") or local_risk)
            if risk not in RISK_ORDER:
                risk = local_risk
            if RISK_ORDER[risk] < RISK_ORDER.get(local_risk, 0):
                risk = local_risk
            findings = parsed.get("key_findings") if isinstance(parsed.get("key_findings"), list) else []
            followup = parsed.get("recommended_followup") if isinstance(parsed.get("recommended_followup"), list) else []
            forbidden = any(key in parsed for key in ["device_actions", "commands", "mqtt", "control"])
            if forbidden:
                raise ValueError("cloud daily summary included forbidden device control fields")
            return {
                "status": "completed",
                "overall_status": str(parsed.get("overall_status") or "已生成"),
                "risk_level": risk,
                "key_findings": [str(item)[:40] for item in findings[:4]],
                "family_message": str(parsed.get("family_message") or "")[:120],
                "recommended_followup": [str(item)[:40] for item in followup[:3]],
                "data_quality_note": str(parsed.get("data_quality_note") or ""),
                "model": settings.cloud_llm_model,
            }
        except Exception as exc:
            result = {"status": "cloud_failed", "error": str(exc)}
            if raw_content:
                result["rejected_model_content"] = raw_content
            return result

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
        available_frame_count = sum(path.is_file() for _, path in image_frames[:5])
        body = {
            "model": settings.cloud_llm_model,
            "messages": [
                {"role": "system", "content": MULTIMODAL_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "max_tokens": max(settings.llm_max_tokens, 1024),
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        raw_content = ""
        parsed_output: dict[str, Any] | None = None
        finish_reason: str | None = None
        try:
            async with httpx.AsyncClient(timeout=settings.cloud_llm_timeout_sec) as client:
                response = await client.post(
                    settings.cloud_llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.cloud_llm_api_key}"},
                    json=body,
                )
                response.raise_for_status()
            choice = response.json()["choices"][0]
            finish_reason = choice.get("finish_reason")
            message = choice["message"]
            raw_content = message.get("content") or message.get("reasoning_content") or ""
            parsed_output = _extract_json_object(raw_content)
            normalized = _normalize_multimodal_output(
                {"event": {**event, "risk_level": local_result.get("risk_level", event.get("risk_level"))}},
                parsed_output,
                array_limits={"temporal_changes": available_frame_count or 2},
            )
            return {"status": "completed", **normalized}
        except Exception as exc:
            result: dict[str, Any] = {"status": "failed", "error": str(exc)}
            if finish_reason:
                result["finish_reason"] = finish_reason
            error_raw = getattr(exc, "raw_model_content", None) or raw_content
            error_parsed = getattr(exc, "parsed_model_output", None) or parsed_output
            if error_raw:
                result["rejected_model_content"] = error_raw
            if error_parsed is not None:
                result["rejected_model_output"] = error_parsed
            return result
