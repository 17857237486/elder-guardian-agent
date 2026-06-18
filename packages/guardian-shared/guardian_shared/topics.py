from dataclasses import dataclass


def elder_sensor_vital(elder_id: str) -> str:
    return f"elder/{elder_id}/sensor/vital"


def elder_sensor_env(elder_id: str) -> str:
    return f"elder/{elder_id}/sensor/env"


def elder_vision_event(elder_id: str) -> str:
    return f"elder/{elder_id}/vision/event"


def elder_hmi_prompt(elder_id: str) -> str:
    return f"elder/{elder_id}/hmi/prompt"


def elder_hmi_response(elder_id: str) -> str:
    return f"elder/{elder_id}/hmi/response"


def elder_hmi_status(elder_id: str) -> str:
    return f"elder/{elder_id}/hmi/status"


def elder_alert_event(elder_id: str) -> str:
    return f"elder/{elder_id}/alert/event"


def elder_agent_decision(elder_id: str) -> str:
    return f"elder/{elder_id}/agent/decision"


def elder_system_status(elder_id: str) -> str:
    return f"elder/{elder_id}/system/status"


def elder_device_telemetry(elder_id: str, device_id: str) -> str:
    return f"elder/{elder_id}/device/{device_id}/telemetry"


def home_device_set(room: str, device: str) -> str:
    return f"home/{room}/{device}/set"


def home_device_state(room: str, device: str) -> str:
    return f"home/{room}/{device}/state"


def home_device_ack(room: str, device: str) -> str:
    return f"home/{room}/{device}/ack"


@dataclass(frozen=True)
class TopicBuilder:
    elder_id: str

    @property
    def sensor_vital(self) -> str:
        return elder_sensor_vital(self.elder_id)

    @property
    def sensor_env(self) -> str:
        return elder_sensor_env(self.elder_id)

    @property
    def vision_event(self) -> str:
        return elder_vision_event(self.elder_id)

    @property
    def hmi_prompt(self) -> str:
        return elder_hmi_prompt(self.elder_id)

    @property
    def hmi_response(self) -> str:
        return elder_hmi_response(self.elder_id)

    @property
    def hmi_status(self) -> str:
        return elder_hmi_status(self.elder_id)

    @property
    def alert_event(self) -> str:
        return elder_alert_event(self.elder_id)

    @property
    def agent_decision(self) -> str:
        return elder_agent_decision(self.elder_id)

    @property
    def system_status(self) -> str:
        return elder_system_status(self.elder_id)

    def device_telemetry(self, device_id: str) -> str:
        return elder_device_telemetry(self.elder_id, device_id)


ELDER_TOPIC_PATTERNS = [
    "elder/+/sensor/vital",
    "elder/+/sensor/env",
    "elder/+/device/+/telemetry",
    "elder/+/vision/event",
    "elder/+/hmi/response",
]

HOME_TOPIC_PATTERNS = [
    "home/+/+/state",
    "home/+/+/ack",
]

