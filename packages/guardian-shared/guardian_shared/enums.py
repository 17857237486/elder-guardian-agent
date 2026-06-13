from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum as _Enum

    class StrEnum(str, _Enum):
        pass


class RiskLevel(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class EventType(StrEnum):
    NORMAL = "normal"
    CO2_HIGH = "co2_high"
    TEMPERATURE_HIGH = "temperature_high"
    TEMPERATURE_LOW = "temperature_low"
    GAS_LEAK = "gas_leak"
    SPO2_LOW = "spo2_low"
    HEART_RATE_ABNORMAL = "heart_rate_abnormal"
    LONG_STATIC = "long_static"
    SUSPECTED_FALL = "suspected_fall"
    NIGHT_ABNORMAL_ACTIVITY = "night_abnormal_activity"
    DEVICE_ACK_FAILED = "device_ack_failed"


class DeviceType(StrEnum):
    WINDOW = "window"
    AIR_CONDITIONER = "air_conditioner"
    FAN = "fan"
    LIGHT = "light"
    GAS_VALVE = "gas_valve"
    LOCAL_ALARM = "local_alarm"


class DeviceAction(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"
    SET_TEMPERATURE = "set_temperature"
    ALARM_ON = "alarm_on"
    ALARM_OFF = "alarm_off"


class ActionType(StrEnum):
    AUTO_CONTROL = "auto_control"
    ASK_ELDER = "ask_elder"
    NOTIFY_FAMILY = "notify_family"
    EMERGENCY_ALERT = "emergency_alert"
    RECORD_ONLY = "record_only"


class EventState(StrEnum):
    NORMAL = "normal"
    EVENT_DETECTED = "event_detected"
    RULE_CLASSIFIED = "rule_classified"
    ACTION_PLANNED = "action_planned"
    AUTO_CONTROL = "auto_control"
    ASK_ELDER = "ask_elder"
    WAIT_RESPONSE = "wait_response"
    FAMILY_ALERT = "family_alert"
    EMERGENCY_ALERT = "emergency_alert"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    RECORDED = "recorded"

