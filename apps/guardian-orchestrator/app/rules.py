from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from guardian_shared.enums import EventState, EventType, RiskLevel
from guardian_shared.v2 import NormalizedEventV2


HISTORY: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=300))
LAST_COMPOSITE_EVENT: dict[tuple[str, str], datetime] = {}


def _observed_at(observation: dict[str, Any]) -> datetime:
    raw = observation.get("observed_at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _record_history(elder_id: str, observation: dict[str, Any]) -> None:
    now = _observed_at(observation)
    HISTORY[elder_id].append({**observation, "_at": now})
    cutoff = now - timedelta(minutes=30)
    while HISTORY[elder_id] and HISTORY[elder_id][0]["_at"] < cutoff:
        HISTORY[elder_id].popleft()


def _recent(elder_id: str, minutes: int = 20) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return [item for item in HISTORY[elder_id] if item["_at"] >= cutoff]


def _allow_composite(elder_id: str, event_type: str, cooldown_min: int = 10) -> bool:
    key = (elder_id, event_type)
    now = datetime.now(timezone.utc)
    previous = LAST_COMPOSITE_EVENT.get(key)
    if previous and now - previous < timedelta(minutes=cooldown_min):
        return False
    LAST_COMPOSITE_EVENT[key] = now
    return True


def _classify_composite(elder_id: str, observation_id: str | None) -> NormalizedEventV2 | None:
    recent = _recent(elder_id, 20)
    if not recent:
        return None
    now = datetime.now(timezone.utc)
    local_now = datetime.now()
    night = local_now.hour >= 22 or local_now.hour < 6
    bedroom_absent = False
    bedroom_absent_since: datetime | None = None
    bathroom_present = False
    bathroom_light_on = False
    bathroom_light_on_since: datetime | None = None
    exit_open = False
    elevated_hr = False
    latest_ids: list[str] = []
    evidence: list[dict[str, Any]] = []
    for item in recent:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        kind = str(item.get("kind") or "")
        oid = item.get("observation_id")
        if oid:
            latest_ids.append(str(oid))
        room = str(payload.get("room") or "")
        if kind == "vital" and int(payload.get("heart_rate") or 0) >= 105:
            elevated_hr = True
            evidence.append({"kind": kind, "heart_rate": payload.get("heart_rate")})
        if kind in {"device_state", "vision"}:
            device = str(payload.get("device") or "")
            state = str(payload.get("state") or "").lower()
            present = payload.get("present")
            if room == "bedroom" and (present is False or state in {"absent", "off"}):
                bedroom_absent = True
                bedroom_absent_since = bedroom_absent_since or item["_at"]
                evidence.append({"kind": kind, "room": room, "state": "absent", "at": item["_at"].isoformat()})
            if room == "bedroom" and (present is True or state == "present"):
                bedroom_absent = False
                bedroom_absent_since = None
            if room == "bathroom" and (present is True or state == "present"):
                bathroom_present = True
                evidence.append({"kind": kind, "room": room, "state": "present", "at": item["_at"].isoformat()})
            if room == "bathroom" and (present is False or state == "absent"):
                bathroom_present = False
            if room == "bathroom" and device in {"light", "lighting"} and state in {"on", "open"}:
                bathroom_light_on = True
                bathroom_light_on_since = bathroom_light_on_since or item["_at"]
                evidence.append({"kind": kind, "room": room, "device": device, "state": state, "at": item["_at"].isoformat()})
            if room == "bathroom" and device in {"light", "lighting"} and state in {"off", "closed"}:
                bathroom_light_on = False
                bathroom_light_on_since = None
            if device in {"door", "window"} and state == "open":
                exit_open = True
    absent_long_enough = bool(bedroom_absent_since and now - bedroom_absent_since >= timedelta(minutes=18))
    light_long_enough = bool(bathroom_light_on_since and now - bathroom_light_on_since >= timedelta(minutes=10))
    if night and bedroom_absent and absent_long_enough and bathroom_present and bathroom_light_on and light_long_enough and elevated_hr:
        event_type = "night_bathroom_not_returned"
        if _allow_composite(elder_id, event_type):
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=event_type,
                risk_level=RiskLevel.P2,
                risk_score=0.72,
                room="bathroom",
                summary="老人夜间离开卧室后仍在卫生间，灯光持续开启且心率升高，需要关注。",
                trigger_observation_ids=latest_ids[-12:] or ([observation_id] if observation_id else []),
                rule_trace={"composite": event_type, "window_minutes": 20, "evidence": evidence},
            )
    if night and bedroom_absent and exit_open:
        event_type = "night_exit_open"
        if _allow_composite(elder_id, event_type):
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=event_type,
                risk_level=RiskLevel.P2,
                risk_score=0.68,
                summary="夜间检测到老人离开卧室且门窗处于开启状态。",
                trigger_observation_ids=latest_ids[-10:] or ([observation_id] if observation_id else []),
                rule_trace={"composite": event_type, "window_minutes": 20},
            )
    return None


def classify_observation(observation: dict[str, Any]) -> NormalizedEventV2 | None:
    kind = observation.get("kind")
    payload = observation.get("payload", {})
    elder_id = observation.get("elder_id") or payload.get("elder_id")
    observation_id = observation.get("observation_id")
    if not elder_id or not isinstance(payload, dict):
        return None
    _record_history(str(elder_id), observation)

    if kind == "environment":
        gas_ppm = int(payload.get("gas_ppm") or 0)
        smoke_ppm = int(payload.get("smoke_ppm") or 0)
        co2_ppm = int(payload.get("co2_ppm") or 0)
        temperature_raw = payload.get("temperature")
        temperature = float(temperature_raw) if temperature_raw is not None else None
        humidity = float(payload.get("humidity") or 0)
        room = payload.get("room") or "living_room"
        trace = {"payload": payload, "observation_id": observation_id}
        if gas_ppm >= 100 or smoke_ppm >= 80:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.GAS_LEAK,
                risk_level=RiskLevel.P0,
                risk_score=1.0,
                state=EventState.RULE_CLASSIFIED,
                room=room,
                summary=f"{room} 检测到燃气/烟雾异常，直接进入 P0。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )
        if co2_ppm >= 1500:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.CO2_HIGH,
                risk_level=RiskLevel.P3,
                risk_score=0.42,
                room=room,
                summary=f"{room} CO2 {co2_ppm} ppm 偏高，建议通风。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )
        if temperature is not None and temperature >= 30:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.TEMPERATURE_HIGH,
                risk_level=RiskLevel.P3,
                risk_score=0.38,
                room=room,
                summary=f"{room} 温度 {temperature:.1f} 摄氏度偏高。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )
        if temperature is not None and temperature <= 16:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.TEMPERATURE_LOW,
                risk_level=RiskLevel.P3,
                risk_score=0.38,
                room=room,
                summary=f"{room} 温度 {temperature:.1f} 摄氏度偏低。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )

        if humidity and (humidity < 25 or humidity > 75):
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type="humidity_abnormal",
                risk_level=RiskLevel.P3,
                risk_score=0.35,
                room=room,
                summary=f"{room} humidity {humidity:.1f}% is outside the safe comfort range.",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )

    if kind == "vital":
        spo2 = int(payload.get("spo2") or 100)
        heart_rate = int(payload.get("heart_rate") or 75)
        trace = {"payload": payload, "observation_id": observation_id}
        if spo2 < 88:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.SPO2_LOW,
                risk_level=RiskLevel.P0,
                risk_score=0.98,
                summary=f"血氧 {spo2}% 低于 88%，直接进入 P0。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )
        if spo2 < 92 or heart_rate < 45 or heart_rate > 130:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.SPO2_LOW if spo2 < 92 else EventType.HEART_RATE_ABNORMAL,
                risk_level=RiskLevel.P1,
                risk_score=0.85,
                summary="生命体征明显异常，需要本地确认并同步家属。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )

    if kind == "vision":
        event_type = str(payload.get("event_type") or "normal")
        room = payload.get("room") or "living_room"
        confidence = float(payload.get("confidence") or 0)
        trace = {"payload": payload, "observation_id": observation_id}
        if event_type == EventType.SUSPECTED_FALL.value:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.SUSPECTED_FALL,
                risk_level=RiskLevel.P1,
                risk_score=max(0.78, confidence),
                room=room,
                summary=f"{room} 发现疑似跌倒。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )
        if event_type == EventType.LONG_STATIC.value:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.LONG_STATIC,
                risk_level=RiskLevel.P2,
                risk_score=max(0.62, confidence),
                room=room,
                summary=f"{room} 长时间静止，先本地询问老人。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )
        if event_type == EventType.NIGHT_ABNORMAL_ACTIVITY.value:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.NIGHT_ABNORMAL_ACTIVITY,
                risk_level=RiskLevel.P1,
                risk_score=max(0.74, confidence),
                room=room,
                summary=f"{room} 检测到夜间异常活动。",
                trigger_observation_ids=[observation_id] if observation_id else [],
                rule_trace=trace,
            )
    return _classify_composite(str(elder_id), observation_id)
