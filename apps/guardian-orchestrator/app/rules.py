from __future__ import annotations

from typing import Any

from guardian_shared.enums import EventState, EventType, RiskLevel
from guardian_shared.v2 import NormalizedEventV2


def classify_observation(observation: dict[str, Any]) -> NormalizedEventV2 | None:
    kind = observation.get("kind")
    payload = observation.get("payload", {})
    elder_id = observation.get("elder_id") or payload.get("elder_id")
    observation_id = observation.get("observation_id")
    if not elder_id or not isinstance(payload, dict):
        return None
    if kind == "environment":
        gas_ppm = int(payload.get("gas_ppm") or 0)
        co2_ppm = int(payload.get("co2_ppm") or 0)
        temperature_raw = payload.get("temperature")
        temperature = float(temperature_raw) if temperature_raw is not None else None
        humidity = float(payload.get("humidity") or 0)
        room = payload.get("room") or "living_room"
        trace = {"payload": payload, "observation_id": observation_id}
        if gas_ppm >= 100:
            return NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.GAS_LEAK,
                risk_level=RiskLevel.P0,
                risk_score=1.0,
                state=EventState.RULE_CLASSIFIED,
                room=room,
                summary=f"{room} 检测到燃气异常，直接进入 P0。",
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
    return None
