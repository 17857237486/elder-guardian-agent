from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import paho.mqtt.client as mqtt
import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))

from Background_MQTT.generate_scenario_data import EVENT_LABELS, SCENE_LABELS, build_event_samples, to_standard_samples
from guardian_shared.schemas import HomeDeviceState, SensorEnvSample, SensorVitalSample
from guardian_shared.topics import elder_sensor_env, elder_sensor_vital, home_device_ack, home_device_state
from guardian_shared.utils import model_to_json

MQTT_HOST = os.getenv("BACKGROUND_MQTT_HOST", os.getenv("MQTT_HOST", "localhost"))
MQTT_PORT = int(os.getenv("BACKGROUND_MQTT_PORT", os.getenv("MQTT_PORT", "1883")))
MQTT_TOPICS = ("elder/+/sensor/vital", "elder/+/sensor/env", "elder/+/vision/event", "home/+/+/set", "home/+/+/state", "home/+/+/ack")
GUARDIAN_CORE_URL = os.getenv("GUARDIAN_CORE_URL", "http://localhost:8000").rstrip("/")
EDGE_API_BASE = os.getenv("EDGE_API_BASE", "http://edge-mcp-server:8010").rstrip("/")
VISION_SERVICE_URL = os.getenv("VISION_SERVICE_URL", "http://vision-service:8101").rstrip("/")
ELDER_ID = os.getenv("ELDER_ID", "elder_001")
MAX_RECORDS = int(os.getenv("BACKGROUND_MAX_RECORDS", "3100"))
ROOM_KEYS = ("bedroom", "kitchen", "living_room", "bathroom")
DEFAULT_ROOM_ENV: dict[str, dict[str, float | int]] = {
    "bedroom": {"temperature": 24.0, "humidity": 50.0, "co2_ppm": 820},
    "kitchen": {"temperature": 25.0, "humidity": 52.0, "co2_ppm": 880},
    "living_room": {"temperature": 24.5, "humidity": 49.0, "co2_ppm": 850},
    "bathroom": {"temperature": 24.0, "humidity": 58.0, "co2_ppm": 780},
}

APP_ROOT = Path(__file__).resolve().parent

app = FastAPI(title="Background MQTT Sensor Monitor", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
connections: set[WebSocket] = set()
mqtt_client: mqtt.Client | None = None
mqtt_connected = False
event_loop: asyncio.AbstractEventLoop | None = None
record_counter = 0
device_states: dict[str, dict[str, Any]] = {}
device_log: deque[dict[str, Any]] = deque(maxlen=100)
DEVICE_ACTION_LOG_TYPES = {"device_command", "manual_command"}
room_env_states: dict[str, dict[str, Any]] = {}
bathroom_stay_monitor: dict[str, Any] = {}
downgraded_env_overrides: dict[str, dict[str, Any]] = {}
scenario_task: asyncio.Task[None] | None = None
FAST_SCENARIO_SAMPLE_DELAY_SEC = 0.1
scenario_job: dict[str, Any] = {
    "run_id": None,
    "status": "idle",
    "scene": None,
    "event_type": None,
    "event_room": None,
    "total_samples": 0,
    "sent_samples": 0,
    "published_messages": 0,
    "stop_requested": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
}


class ScenarioPublishRequest(BaseModel):
    scene: str = Field(default="dinner")
    event_type: str = Field(default="gas_leak")
    event_room: str = Field(default="living_room")
    trigger_second: int = Field(default=60, ge=0, le=120)
    elder_id: str = Field(default="elder_001")
    duration_sec: int = Field(default=120, ge=5, le=600)
    interval_sec: int = Field(default=5, ge=1, le=60)
    realtime_interval_sec: int = Field(default=2, ge=1, le=60)
    realtime: bool = False


class DeviceCommandRequest(BaseModel):
    room: str
    device: str
    action: str
    value: Any = None
    reason: str = "Background MQTT dashboard control"


class ManualRiskEventRequest(BaseModel):
    event_type: str
    elder_id: str = "elder_001"
    room: str = "living_room"


class PersonalBaselineRequest(BaseModel):
    elder_id: str = "elder_001"
    baseline_type: str
    scope: str = "default"
    timezone: str = "Asia/Shanghai"
    lookback_days: int = 14
    sample_count: int = 1
    quality: str = "stable"
    metrics: dict[str, Any] = Field(default_factory=dict)


class AutoVitalsBaselineRequest(BaseModel):
    elder_id: str = "elder_001"
    sample_count: int = Field(default=3000, ge=24, le=5000)
    logical_interval_sec: int = Field(default=5, ge=1, le=60)
    publish_mqtt: bool = True
    rebuild_delay_sec: float = Field(default=2.0, ge=0.0, le=10.0)


class AutoBathroomBaselineRequest(BaseModel):
    elder_id: str = "elder_001"
    stay_count: int = Field(default=20, ge=3, le=80)
    avg_stay_sec: int = Field(default=180, ge=10, le=3600)
    p90_stay_sec: int = Field(default=480, ge=20, le=7200)
    rebuild_delay_sec: float = Field(default=1.0, ge=0.0, le=10.0)


class BathroomStayDemoRequest(BaseModel):
    elder_id: str = "elder_001"
    duration_seconds: int = Field(default=600, ge=10, le=7200)
    logical_interval_sec: int = Field(default=5, ge=1, le=60)
    rebuild_delay_sec: float = Field(default=1.0, ge=0.0, le=10.0)


class DailyHealthSummaryProxyRequest(BaseModel):
    elder_id: str = "elder_001"
    date: str | None = None
    timezone: str = "Asia/Shanghai"
    use_cloud: bool = True
    generated_by: str = "background_mqtt"


class VisionCaptureProxyRequest(BaseModel):
    elder_id: str = "elder_001"
    camera_id: str = "living_room"
    room: str = "living_room"
    trigger_source: str = "background_mqtt"
    reason: str = "manual_capture"


class VisionCaptureClearProxyRequest(BaseModel):
    elder_id: str = "elder_001"
    camera_id: str = "living_room"


DEFAULT_DEVICES: list[dict[str, Any]] = [
    {"room": "bedroom", "device": "air_conditioner", "state": "off", "value": None},
    {"room": "bedroom", "device": "fan", "state": "off", "value": None},
    {"room": "bedroom", "device": "window", "state": "closed", "value": None},
    {"room": "bedroom", "device": "light", "state": "off", "value": None},
    {"room": "kitchen", "device": "window", "state": "closed", "value": None},
    {"room": "kitchen", "device": "light", "state": "off", "value": None},
    {"room": "kitchen", "device": "fan", "state": "off", "value": None},
    {"room": "kitchen", "device": "gas_valve", "state": "open", "value": None},
    {"room": "living_room", "device": "air_conditioner", "state": "off", "value": None},
    {"room": "living_room", "device": "window", "state": "closed", "value": None},
    {"room": "living_room", "device": "light", "state": "off", "value": None},
    {"room": "bathroom", "device": "window", "state": "closed", "value": None},
    {"room": "bathroom", "device": "light", "state": "off", "value": None},
    {"room": "bathroom", "device": "heater", "state": "off", "value": None},
    {"room": "local", "device": "local_alarm", "state": "off", "value": None},
]
DEVICE_ORDER = {f"{item['room']}/{item['device']}": index for index, item in enumerate(DEFAULT_DEVICES)}
active_device_keys = set(DEVICE_ORDER)
guardian_core_device_keys: set[str] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def topic_kind(topic: str) -> str:
    if topic.endswith("/sensor/vital"):
        return "vital"
    if topic.endswith("/sensor/env"):
        return "env"
    if topic.endswith("/vision/event"):
        return "vision"
    if topic.endswith("/set"):
        return "device_set"
    if topic.endswith("/state"):
        return "device_state"
    if topic.endswith("/ack"):
        return "device_ack"
    return "unknown"


def elder_id_from_topic(topic: str) -> str:
    parts = topic.split("/")
    return parts[1] if len(parts) >= 2 else "unknown"


def device_key(room: str, device: str) -> str:
    return f"{room}/{device}"


def sorted_devices() -> list[dict[str, Any]]:
    return sorted(
        [item for key, item in device_states.items() if key in active_device_keys],
        key=lambda item: (DEVICE_ORDER.get(device_key(item["room"], item["device"]), 999), item["room"], item["device"]),
    )


def default_room_env_state(room: str) -> dict[str, Any]:
    defaults = DEFAULT_ROOM_ENV[room]
    return {
        "room": room,
        "temperature": defaults["temperature"],
        "humidity": defaults["humidity"],
        "co2_ppm": defaults["co2_ppm"],
        "presence": False,
        "timestamp": None,
        "updated_at": None,
        "sample_id": None,
        "elder_id": None,
    }


def init_default_room_env_states() -> None:
    for room in ROOM_KEYS:
        room_env_states.setdefault(room, default_room_env_state(room))


def room_env_snapshot() -> list[dict[str, Any]]:
    init_default_room_env_states()
    return [dict(room_env_states[room]) for room in ROOM_KEYS]


def update_room_env_state(payload: dict[str, Any]) -> dict[str, Any] | None:
    room = str(payload.get("room", ""))
    if room not in ROOM_KEYS:
        return None
    presence = payload.get("presence")
    state = {
        **room_env_states.get(room, default_room_env_state(room)),
        "room": room,
        "temperature": payload.get("temperature"),
        "humidity": payload.get("humidity"),
        "co2_ppm": payload.get("co2_ppm"),
        "presence": presence if isinstance(presence, bool) else room_env_states.get(room, {}).get("presence", False),
        "timestamp": payload.get("timestamp"),
        "updated_at": utc_now(),
        "sample_id": payload.get("sample_id"),
        "elder_id": payload.get("elder_id", ELDER_ID),
    }
    room_env_states[room] = state
    return state


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def init_bathroom_stay_monitor() -> None:
    if bathroom_stay_monitor:
        return
    bathroom_stay_monitor.update(
        {
            "current_room": None,
            "room_presence": {room: False for room in ROOM_KEYS},
            "bathroom_present": False,
            "bathroom_entered_at": None,
            "bathroom_exited_at": None,
            "bathroom_elapsed_sec": 0,
            "bathroom_reference_limit_sec": 60,
            "status": "not_in_bathroom",
            "last_stay_seconds": None,
            "last_updated_at": None,
            "elder_id": ELDER_ID,
            "sample_id": None,
            "recent_presence_events": [],
            "demo_flow_rows": [],
            "demo_flow_seen_sample_ids": [],
            "demo_flow_current_room": None,
            "demo_flow_room_entered_at": None,
            "demo_final_bathroom_stay_sec": None,
        }
    )


def bathroom_stay_monitor_snapshot() -> dict[str, Any]:
    init_bathroom_stay_monitor()
    monitor = dict(bathroom_stay_monitor)
    entered_at = _parse_datetime(monitor.get("bathroom_entered_at"))
    if monitor.get("bathroom_present") and entered_at:
        monitor["bathroom_elapsed_sec"] = max(0, int((datetime.now(timezone.utc) - entered_at).total_seconds()))
        limit = float(monitor.get("bathroom_reference_limit_sec") or 60)
        monitor["status"] = "over_limit" if monitor["bathroom_elapsed_sec"] > limit else "in_bathroom"
    monitor["recent_presence_events"] = list(monitor.get("recent_presence_events") or [])[:10]
    monitor["demo_flow_rows"] = list(monitor.get("demo_flow_rows") or [])
    return monitor


async def bathroom_reference_limit_sec(elder_id: str) -> float:
    return await current_vital_baseline_value(elder_id, "bathroom_routine", "bathroom_stay_p90_sec", 60)


def _presence_event_label(room_presence: dict[str, bool]) -> str:
    return ", ".join(f"{room}={'true' if room_presence.get(room) else 'false'}" for room in ROOM_KEYS)


def update_bathroom_stay_monitor(env_payload: dict[str, Any]) -> dict[str, Any]:
    init_bathroom_stay_monitor()
    if env_payload.get("source") == "bathroom_baseline_generator":
        return bathroom_stay_monitor_snapshot()
    rooms = env_payload.get("rooms") if isinstance(env_payload.get("rooms"), dict) else {}
    room_presence = {
        room: bool((rooms.get(room) or {}).get("presence"))
        for room in ROOM_KEYS
        if isinstance(rooms.get(room), dict)
    }
    if not room_presence:
        return bathroom_stay_monitor_snapshot()
    previous_present = bool(bathroom_stay_monitor.get("bathroom_present"))
    now_value = env_payload.get("timestamp") or env_payload.get("observed_at") or utc_now()
    now_dt = _parse_datetime(now_value) or datetime.now(timezone.utc)
    current_room = next((room for room in ROOM_KEYS if room_presence.get(room)), None)
    bathroom_present = bool(room_presence.get("bathroom"))
    entered_at = bathroom_stay_monitor.get("bathroom_entered_at")
    exited_at = bathroom_stay_monitor.get("bathroom_exited_at")
    last_stay_seconds = bathroom_stay_monitor.get("last_stay_seconds")
    if bathroom_present and not previous_present:
        entered_at = now_dt.isoformat()
        exited_at = None
        last_stay_seconds = None
    elif not bathroom_present and previous_present:
        old_entered = _parse_datetime(bathroom_stay_monitor.get("bathroom_entered_at"))
        if old_entered:
            last_stay_seconds = max(0, int((now_dt - old_entered).total_seconds()))
        exited_at = now_dt.isoformat()
        entered_at = None
    active_entered = _parse_datetime(entered_at)
    elapsed = max(0, int((now_dt - active_entered).total_seconds())) if bathroom_present and active_entered else 0
    limit = float(bathroom_stay_monitor.get("bathroom_reference_limit_sec") or 60)
    status = "over_limit" if bathroom_present and elapsed > limit else "in_bathroom" if bathroom_present else "not_in_bathroom"
    source = str(env_payload.get("source") or "")
    sample_id = str(env_payload.get("snapshot_id") or "")
    event = {
        "timestamp": now_dt.isoformat(),
        "current_room": current_room,
        "bathroom_present": bathroom_present,
        "room_presence": dict(room_presence),
        "summary": _presence_event_label(room_presence),
        "sample_id": sample_id or None,
    }
    recent = [event, *list(bathroom_stay_monitor.get("recent_presence_events") or [])][:10]
    demo_flow_rows = list(bathroom_stay_monitor.get("demo_flow_rows") or [])
    demo_seen_sample_ids = list(bathroom_stay_monitor.get("demo_flow_seen_sample_ids") or [])
    demo_current_room = bathroom_stay_monitor.get("demo_flow_current_room")
    demo_room_entered_at = bathroom_stay_monitor.get("demo_flow_room_entered_at")
    demo_final_stay = bathroom_stay_monitor.get("demo_final_bathroom_stay_sec")
    if source == "bathroom_stay_demo" and (not sample_id or sample_id not in demo_seen_sample_ids):
        if current_room != demo_current_room:
            demo_current_room = current_room
            demo_room_entered_at = now_dt.isoformat()
        room_entered_dt = _parse_datetime(demo_room_entered_at) or now_dt
        current_room_elapsed = max(0, int((now_dt - room_entered_dt).total_seconds())) if current_room else 0
        row = {
            "index": len(demo_flow_rows) + 1,
            "sample_id": sample_id or None,
            "timestamp": now_dt.isoformat(),
            "current_room": current_room,
            "current_room_elapsed_sec": current_room_elapsed,
            "bathroom_present": bathroom_present,
            "bathroom_elapsed_sec": elapsed,
        }
        demo_flow_rows = [*demo_flow_rows, row][-1500:]
        if sample_id:
            demo_seen_sample_ids = [*demo_seen_sample_ids, sample_id][-1500:]
        if not bathroom_present and previous_present and last_stay_seconds is not None:
            demo_final_stay = last_stay_seconds
    bathroom_stay_monitor.update(
        {
            "current_room": current_room,
            "room_presence": {room: bool(room_presence.get(room)) for room in ROOM_KEYS},
            "bathroom_present": bathroom_present,
            "bathroom_entered_at": entered_at,
            "bathroom_exited_at": exited_at,
            "bathroom_elapsed_sec": elapsed,
            "status": status,
            "last_stay_seconds": last_stay_seconds,
            "last_updated_at": now_dt.isoformat(),
            "elder_id": env_payload.get("elder_id", ELDER_ID),
            "sample_id": env_payload.get("snapshot_id"),
            "recent_presence_events": recent,
            "demo_flow_rows": demo_flow_rows,
            "demo_flow_seen_sample_ids": demo_seen_sample_ids,
            "demo_flow_current_room": demo_current_room,
            "demo_flow_room_entered_at": demo_room_entered_at,
            "demo_final_bathroom_stay_sec": demo_final_stay,
        }
    )
    return bathroom_stay_monitor_snapshot()


def init_default_device_states() -> None:
    for item in DEFAULT_DEVICES:
        upsert_device_state(
            {
                "elder_id": ELDER_ID,
                "room": item["room"],
                "device": item["device"],
                "state": item["state"],
                "value": item["value"],
                "online": True,
                "timestamp": utc_now(),
            },
            log=False,
        )


def append_device_log(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    entry = {"type": event_type, "timestamp": utc_now(), "data": data}
    device_log.appendleft(entry)
    return entry


def visible_device_log() -> list[dict[str, Any]]:
    return [item for item in device_log if item.get("type") in DEVICE_ACTION_LOG_TYPES]


def upsert_device_state(data: dict[str, Any], *, log: bool = True) -> dict[str, Any]:
    room = str(data.get("room", "unknown"))
    device = str(data.get("device", "unknown"))
    key = device_key(room, device)
    previous = device_states.get(key, {})
    state = {
        **previous,
        "elder_id": data.get("elder_id", previous.get("elder_id", ELDER_ID)),
        "room": room,
        "device": device,
        "state": str(data.get("state", previous.get("state", "unknown"))),
        "value": data.get("value", previous.get("value")),
        "online": bool(data.get("online", previous.get("online", True))),
        "timestamp": data.get("timestamp", utc_now()),
    }
    device_states[key] = state
    if log:
        append_device_log("device_state", state)
    return state


def derive_device_state(action: str, value: Any) -> tuple[str, Any]:
    if action == "open":
        return "open", value
    if action == "close":
        return "closed", value
    if action in {"turn_on", "alarm_on"}:
        return "on", value
    if action in {"turn_off", "alarm_off"}:
        return "off", value
    if action == "set_temperature":
        return "on", value
    return "unknown", value


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_env_for_guardian_core(sample: dict[str, Any]) -> dict[str, Any]:
    env = sample.get("environment")
    if not isinstance(env, dict):
        return sample
    if not should_downgrade_env_for_guardian_core(sample):
        return sample

    normalized = {**sample, "environment": {**env}}
    normalized_env = normalized["environment"]
    defaults = DEFAULT_ROOM_ENV.get(str(env.get("room")), DEFAULT_ROOM_ENV["living_room"])
    normalized_env["temperature"] = defaults["temperature"]
    normalized_env["humidity"] = defaults["humidity"]
    normalized_env["co2_ppm"] = defaults["co2_ppm"]
    normalized_env["gas_ppm"] = 0
    normalized_env["smoke_ppm"] = 0
    normalized["injected_event"] = "normal"
    append_device_log(
        "scenario_downgraded",
        {
            "scene": sample.get("scene"),
            "event_type": sample.get("injected_event"),
            "event_room": env.get("room"),
            "occupant_room": env.get("occupant_room"),
            "reason": "non-gas event is outside elder room; send normal env sample to guardian-core",
        },
    )
    return normalized


def should_downgrade_env_for_guardian_core(sample: dict[str, Any]) -> bool:
    event_type = str(sample.get("injected_event", "normal"))
    env = sample.get("environment")
    if not isinstance(env, dict) or event_type in {"normal", "gas_leak"}:
        return False
    if str(env.get("presence", "home")) == "away":
        return True
    event_room = str(env.get("room", ""))
    occupant_room = str(env.get("occupant_room") or event_room)
    return bool(event_room and occupant_room and event_room != occupant_room)


def decode_payload(msg: mqtt.MQTTMessage) -> dict[str, Any]:
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as exc:
        payload = {
            "error": f"invalid json: {exc}",
            "raw": msg.payload.decode("utf-8", errors="replace"),
        }
    return payload


async def broadcast(message: dict[str, Any]) -> None:
    dead: list[WebSocket] = []
    for websocket in list(connections):
        try:
            await websocket.send_json(message)
        except Exception:
            dead.append(websocket)
    for websocket in dead:
        connections.discard(websocket)


def append_record(topic: str, payload: dict[str, Any]) -> dict[str, Any]:
    global record_counter
    record_counter += 1
    record = {
        "record_no": record_counter,
        "topic": topic,
        "kind": topic_kind(topic),
        "elder_id": payload.get("elder_id") or elder_id_from_topic(topic),
        "sample_id": payload.get("sample_id"),
        "sample_timestamp": payload.get("timestamp"),
        "received_at": utc_now(),
        "payload": payload,
    }
    records.appendleft(record)
    return record


def publish_model(topic: str, payload: SensorVitalSample | SensorEnvSample) -> None:
    if mqtt_client is None or not mqtt_connected:
        raise HTTPException(status_code=503, detail="MQTT not connected")
    result = mqtt_client.publish(topic, model_to_json(payload), qos=1)
    result.wait_for_publish()
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise HTTPException(status_code=503, detail=f"MQTT publish failed rc={result.rc}")


def publish_json(topic: str, payload: dict[str, Any] | HomeDeviceState) -> None:
    if mqtt_client is None or not mqtt_connected:
        append_device_log("error", {"message": f"MQTT not connected; skip publish {topic}"})
        return
    body = model_to_json(payload) if isinstance(payload, HomeDeviceState) else json.dumps(payload, ensure_ascii=False, default=str)
    result = mqtt_client.publish(topic, body, qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        append_device_log("error", {"message": f"MQTT publish failed rc={result.rc}", "topic": topic})


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return float(ordered[index])


def generated_vital_values(index: int) -> tuple[int, int]:
    heart_wave = math.sin(index / 37.0) * 8 + math.sin(index / 113.0) * 4
    activity_bump = 10 if index % 420 in range(80, 130) else 0
    heart_rate = int(round(76 + heart_wave + activity_bump))
    heart_rate = max(62, min(102, heart_rate))
    spo2_wave = math.sin(index / 91.0) * 1.2 + math.sin(index / 211.0) * 0.6
    spo2 = int(round(96 + spo2_wave))
    spo2 = max(94, min(98, spo2))
    return heart_rate, spo2


def vital_metric_preview(sample_count: int) -> dict[str, Any]:
    heart_values: list[float] = []
    spo2_values: list[float] = []
    for index in range(sample_count):
        heart_rate, spo2 = generated_vital_values(index)
        heart_values.append(float(heart_rate))
        spo2_values.append(float(spo2))
    return {
        "heart_rate": {
            "min": min(heart_values),
            "max": max(heart_values),
            "avg": round(sum(heart_values) / len(heart_values), 1),
            "low_reference": int(round(min(heart_values) - 1)),
            "normal_reference": int(round(sum(heart_values) / len(heart_values))),
            "high_reference": int(round(max(heart_values) + 1)),
        },
        "spo2": {
            "min": min(spo2_values),
            "max": max(spo2_values),
            "avg": round(sum(spo2_values) / len(spo2_values), 1),
            "low_reference": round(min(spo2_values) - 0.5, 1),
            "normal_reference": round(sum(spo2_values) / len(spo2_values), 1),
            "high_reference": round(max(spo2_values) + 0.5, 1),
        },
    }


def bathroom_stay_durations(count: int, avg_stay_sec: int, p90_stay_sec: int) -> list[int]:
    normal_count = max(1, count - 3)
    low = max(10, int(avg_stay_sec * 0.65))
    high = max(low + 1, int(avg_stay_sec * 1.25))
    durations = [low + (index * 37) % max(1, high - low) for index in range(normal_count)]
    durations.extend([max(avg_stay_sec, p90_stay_sec - 45), p90_stay_sec, p90_stay_sec + 60])
    return durations[:count]


def home_presence_snapshot(elder_id: str, present_room: str, observed_at: datetime, *, source: str) -> dict[str, Any]:
    rooms: dict[str, dict[str, Any]] = {}
    for room in ROOM_KEYS:
        base = dict(DEFAULT_ROOM_ENV[room])
        rooms[room] = {
            **base,
            "gas_ppm": 0,
            "smoke_ppm": 0,
            "presence": room == present_room,
        }
    return {
        "schema": "home_environment_snapshot_v1",
        "snapshot_id": f"baseline_{uuid4().hex[:10]}",
        "elder_id": elder_id,
        "source": source,
        "rooms": rooms,
        "observed_at": observed_at.isoformat(),
        "timestamp": observed_at.isoformat(),
    }


async def rebuild_edge_baselines(elder_id: str, baseline_types: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"elder_id": elder_id}
    if baseline_types:
        payload["baseline_types"] = baseline_types
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        response = await client.post(f"{EDGE_API_BASE}/api/v2/baselines/rebuild", json=payload)
        response.raise_for_status()
        return response.json()


def ensure_mqtt_connected() -> None:
    if mqtt_client is None or not mqtt_connected:
        raise HTTPException(status_code=503, detail="MQTT not connected")


def publish_sample_pair(sample: dict[str, Any]) -> int:
    ensure_mqtt_connected()
    guardian_core_sample = normalize_env_for_guardian_core(sample)
    if guardian_core_sample is not sample:
        downgraded_env_overrides[f"env_{sample['sample_id']}"] = {
            **sample["environment"],
            "sample_id": f"env_{sample['sample_id']}",
            "elder_id": sample["elder_id"],
            "timestamp": sample["timestamp"],
        }
    vital_sample, env_sample = to_standard_samples(guardian_core_sample)
    publish_model(elder_sensor_vital(sample["elder_id"]), vital_sample)
    if sample.get("bathroom_stay_demo"):
        publish_json(elder_sensor_env(sample["elder_id"]), home_environment_snapshot(sample))
    else:
        publish_model(elder_sensor_env(sample["elder_id"]), env_sample)
    return 2


def home_environment_snapshot(sample: dict[str, Any]) -> dict[str, Any]:
    env = sample.get("environment", {})
    occupant_room = str(env.get("occupant_room") or env.get("room") or "living_room")
    rooms: dict[str, dict[str, Any]] = {}
    for room in ROOM_KEYS:
        base = dict(DEFAULT_ROOM_ENV[room])
        if room == env.get("room"):
            base.update(
                {
                    "temperature": env.get("temperature", base["temperature"]),
                    "humidity": env.get("humidity", base["humidity"]),
                    "co2_ppm": env.get("co2_ppm", base["co2_ppm"]),
                    "gas_ppm": env.get("gas_ppm", 0),
                    "smoke_ppm": env.get("smoke_ppm", 0),
                }
            )
        else:
            base.update({"gas_ppm": 0, "smoke_ppm": 0})
        base["presence"] = room == occupant_room
        rooms[room] = base
    return {
        "schema": "home_environment_snapshot_v1",
        "snapshot_id": f"envsnap_{sample['sample_id']}",
        "elder_id": sample["elder_id"],
        "source": "background_mqtt_timeline",
        "observed_at": sample["timestamp"],
        "timestamp": sample["timestamp"],
        "rooms": rooms,
    }


async def trigger_vision_event(event_type: str, elder_id: str, room: str) -> dict[str, Any]:
    payload = {
        "elder_id": elder_id,
        "camera_id": room,
        "room": room,
        "event_type": event_type,
        "confidence": 0.92 if event_type == "suspected_fall" else 0.78,
        "posture": "lying" if event_type == "suspected_fall" else "unknown",
        "motion_state": "static",
        "risk_level": "P1" if event_type == "suspected_fall" else "P2",
        "triggered_at": utc_now(),
    }
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        response = await client.post(f"{VISION_SERVICE_URL}/api/v2/vision/triggers", json=payload)
        response.raise_for_status()
        return response.json()


async def vision_capture(request: VisionCaptureProxyRequest) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        response = await client.post(f"{VISION_SERVICE_URL}/api/v2/vision/captures", json=request.model_dump())
        response.raise_for_status()
        return response.json()


async def recent_vision_captures(elder_id: str, camera_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get(
            f"{VISION_SERVICE_URL}/api/v2/vision/captures/recent",
            params={"elder_id": elder_id, "camera_id": camera_id},
        )
        response.raise_for_status()
        return response.json()


async def clear_vision_captures(request: VisionCaptureClearProxyRequest) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(f"{VISION_SERVICE_URL}/api/v2/vision/captures/clear", json=request.model_dump())
        response.raise_for_status()
        return response.json()


async def publish_risk_signal(event_type: str, elder_id: str, room: str) -> int:
    ensure_mqtt_connected()
    if event_type == "normal":
        publish_json(
            home_device_state("bedroom", "presence_sensor"),
            {
                "elder_id": elder_id,
                "room": "bedroom",
                "device": "presence_sensor",
                "present": True,
                "state": "present",
                "online": True,
                "timestamp": utc_now(),
            },
        )
        return 1
    if event_type in {"suspected_fall", "long_static"}:
        await trigger_vision_event(event_type, elder_id, room)
        return 1
    return 0


async def current_vital_baseline_value(elder_id: str, baseline_type: str, metric_key: str, default: float) -> float:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(f"{EDGE_API_BASE}/api/v2/personal-baselines", params={"elder_id": elder_id})
            response.raise_for_status()
            baselines = response.json().get("personal_baselines", [])
    except httpx.HTTPError:
        return default
    for baseline in baselines:
        if baseline.get("baseline_type") == baseline_type:
            try:
                return int(float(baseline.get("metrics", {}).get(metric_key, default)))
            except (TypeError, ValueError):
                return default
    return default


async def current_heart_rate_p90(elder_id: str) -> float:
    return await current_vital_baseline_value(elder_id, "heart_rate_daily", "p90", 100)


async def current_spo2_p10(elder_id: str) -> float:
    return await current_vital_baseline_value(elder_id, "spo2_daily", "p10", 95)


async def current_bathroom_stay_p90(elder_id: str) -> float:
    return await current_vital_baseline_value(elder_id, "bathroom_routine", "bathroom_stay_p90_sec", 60)


async def post_vital_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(f"{EDGE_API_BASE}/api/v2/ai-review-candidates", json=candidate)
        response.raise_for_status()
        return response.json()


async def create_heart_rate_candidate(elder_id: str, room: str, *, latest_value: int = 115) -> dict[str, Any]:
    baseline_p90 = await current_heart_rate_p90(elder_id)
    candidate = {
        "elder_id": elder_id,
        "candidate_type": "vital_baseline_anomaly",
        "priority": "medium",
        "status": "pending",
        "reason": "heart rate window above personal p90",
        "source_segment_ids": [],
        "baseline_refs": ["heart_rate_daily:default"],
        "features": {
            "metric": "heart_rate",
            "direction": "high",
            "latest_value": latest_value,
            "min": latest_value,
            "max": latest_value,
            "p10": latest_value,
            "p90": latest_value,
            "baseline_p90": baseline_p90,
            "sample_count": 24,
            "window_seconds": 300,
            "room": room,
            "demo_source": "background_mqtt_timeline",
            "dedupe_key": f"demo-heart-rate-high-{elder_id}-{room}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        },
    }
    return await post_vital_candidate(candidate)


async def create_spo2_candidate(elder_id: str, room: str, *, latest_value: int = 94) -> dict[str, Any]:
    baseline_p10 = await current_spo2_p10(elder_id)
    candidate = {
        "elder_id": elder_id,
        "candidate_type": "vital_baseline_anomaly",
        "priority": "medium",
        "status": "pending",
        "reason": "spo2 window below personal p10",
        "source_segment_ids": [],
        "baseline_refs": ["spo2_daily:default"],
        "features": {
            "metric": "spo2",
            "direction": "low",
            "latest_value": latest_value,
            "min": latest_value,
            "max": latest_value,
            "p10": latest_value,
            "p90": latest_value,
            "baseline_p10": baseline_p10,
            "sample_count": 24,
            "window_seconds": 300,
            "room": room,
            "demo_source": "background_mqtt_timeline",
            "dedupe_key": f"demo-spo2-low-{elder_id}-{room}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        },
    }
    return await post_vital_candidate(candidate)


async def create_bathroom_stay_candidate(elder_id: str, *, duration_seconds: int | None = None) -> dict[str, Any]:
    baseline_p90 = await current_bathroom_stay_p90(elder_id)
    duration = duration_seconds or baseline_p90 + 15
    key_time = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    candidate = {
        "elder_id": elder_id,
        "candidate_type": "bathroom_stay_anomaly",
        "priority": "medium",
        "status": "pending",
        "reason": "卫生间停留超过个人90分位",
        "source_segment_ids": [],
        "baseline_refs": ["bathroom_routine:default"],
        "features": {
            "duration_seconds": duration,
            "baseline_p90_seconds": baseline_p90,
            "room": "bathroom",
            "demo_source": "background_mqtt_manual",
            "dedupe_key": f"demo-bathroom-stay-{elder_id}-{key_time}",
        },
    }
    return await post_vital_candidate(candidate)


def manual_risk_samples(event_type: str, elder_id: str, room: str) -> tuple[SensorVitalSample, SensorEnvSample]:
    vital = {"heart_rate": 78, "spo2": 96, "systolic_bp": 128, "diastolic_bp": 80, "body_temperature": 36.6}
    env = {"temperature": 25.0, "humidity": 50.0, "co2_ppm": 900, "gas_ppm": 0, "smoke_ppm": 0}
    if event_type == "spo2_critical":
        vital.update(heart_rate=88, spo2=86)
    elif event_type == "spo2_low":
        vital.update(heart_rate=88, spo2=90)
    elif event_type == "heart_rate_abnormal":
        vital.update(heart_rate=138)
    elif event_type == "heart_rate_baseline_anomaly":
        vital.update(heart_rate=115, systolic_bp=136, diastolic_bp=84)
    elif event_type == "spo2_baseline_anomaly":
        vital.update(heart_rate=86, spo2=94)
    elif event_type == "bathroom_stay_anomaly_demo":
        env.update(temperature=24.0, humidity=58.0, co2_ppm=780)
    elif event_type == "co2_high":
        env.update(co2_ppm=1800)
    elif event_type == "gas_leak":
        env.update(gas_ppm=180)
    elif event_type == "temperature_high":
        env.update(temperature=31.0)
    elif event_type == "temperature_low":
        env.update(temperature=15.0)
    elif event_type == "humidity_abnormal":
        env.update(humidity=82.0)
    return SensorVitalSample(elder_id=elder_id, **vital), SensorEnvSample(elder_id=elder_id, room=room, **env)


def scenario_status_snapshot() -> dict[str, Any]:
    return dict(scenario_job)


async def sync_devices_from_guardian_core() -> None:
    global active_device_keys, guardian_core_device_keys
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            response = await client.get(f"{GUARDIAN_CORE_URL}/api/home/devices")
            response.raise_for_status()
            data = response.json()
        synced_keys = {
            device_key(str(item.get("room", "")), str(item.get("device", "")))
            for item in data.get("devices", [])
            if item.get("room") and item.get("device")
        }
        guardian_core_device_keys = synced_keys
        if synced_keys:
            active_device_keys = set(DEVICE_ORDER) | synced_keys
        for item in data.get("devices", []):
            upsert_device_state(item, log=False)
        append_device_log("guardian_core", {"status": "devices_synced", "url": GUARDIAN_CORE_URL})
    except Exception as exc:
        append_device_log("guardian_core", {"status": "sync_failed", "url": GUARDIAN_CORE_URL, "error": str(exc)})


def broadcast_from_mqtt(message: dict[str, Any]) -> None:
    if event_loop is not None:
        asyncio.run_coroutine_threadsafe(broadcast(message), event_loop)


def handle_device_set(topic: str, payload: dict[str, Any]) -> None:
    parts = topic.split("/")
    if len(parts) < 4:
        return
    room, device = parts[1], parts[2]
    action = str(payload.get("action", ""))
    state_text, value = derive_device_state(action, payload.get("value"))
    command = {
        "topic": topic,
        "cmd_id": payload.get("cmd_id"),
        "room": room,
        "device": device,
        "action": action,
        "value": payload.get("value"),
        "reason": payload.get("reason"),
        "priority": payload.get("priority"),
        "timestamp": utc_now(),
    }
    append_device_log("device_command", command)
    state = HomeDeviceState(
        elder_id=payload.get("elder_id", ELDER_ID),
        room=room,
        device=device,
        state=state_text,
        value=value,
        online=True,
    )
    updated = upsert_device_state(state.model_dump(mode="json"))
    ack = {"cmd_id": payload.get("cmd_id"), "status": "acked", "room": room, "device": device, "timestamp": utc_now()}
    publish_json(home_device_ack(room, device), ack)
    publish_json(home_device_state(room, device), state)
    broadcast_from_mqtt({"type": "device_command", "data": command, "devices": sorted_devices(), "device_log": visible_device_log()})
    broadcast_from_mqtt({"type": "device_state", "data": updated, "devices": sorted_devices(), "device_log": visible_device_log()})
    broadcast_from_mqtt({"type": "device_ack", "data": ack, "devices": sorted_devices(), "device_log": visible_device_log()})


def handle_device_state(topic: str, payload: dict[str, Any]) -> None:
    parts = topic.split("/")
    if len(parts) >= 4:
        payload.setdefault("room", parts[1])
        payload.setdefault("device", parts[2])
    updated = upsert_device_state(payload)
    broadcast_from_mqtt({"type": "device_state", "data": updated, "devices": sorted_devices(), "device_log": visible_device_log()})


def handle_device_ack(topic: str, payload: dict[str, Any]) -> None:
    parts = topic.split("/")
    if len(parts) >= 4:
        payload.setdefault("room", parts[1])
        payload.setdefault("device", parts[2])
    payload.setdefault("timestamp", utc_now())
    append_device_log("device_ack", payload)
    broadcast_from_mqtt({"type": "device_ack", "data": payload, "devices": sorted_devices(), "device_log": visible_device_log()})


def simulate_local_device_command(payload: dict[str, Any]) -> dict[str, Any]:
    room = str(payload.get("room", "unknown"))
    device = str(payload.get("device", "unknown"))
    action = str(payload.get("action", ""))
    state_text, value = derive_device_state(action, payload.get("value"))
    command = {
        "cmd_id": payload.get("cmd_id", f"local_{uuid4().hex[:12]}"),
        "room": room,
        "device": device,
        "action": action,
        "value": payload.get("value"),
        "reason": payload.get("reason"),
        "priority": payload.get("priority", "P3"),
        "timestamp": utc_now(),
        "status": "local_simulated",
    }
    append_device_log("manual_command", {"payload": payload, "result": command})
    state = HomeDeviceState(elder_id=payload.get("elder_id", ELDER_ID), room=room, device=device, state=state_text, value=value, online=True)
    updated = upsert_device_state(state.model_dump(mode="json"))
    publish_json(home_device_state(room, device), state)
    return {"ok": True, "status": "local_simulated", "command": command, "state": updated}


def validate_scenario_request(request: ScenarioPublishRequest) -> None:
    if request.scene not in SCENE_LABELS:
        raise HTTPException(status_code=422, detail=f"unknown scene: {request.scene}")
    if request.event_type not in EVENT_LABELS:
        raise HTTPException(status_code=422, detail=f"unknown event_type: {request.event_type}")
    if request.event_room not in ROOM_KEYS:
        raise HTTPException(status_code=422, detail=f"unknown event_room: {request.event_room}")
    if request.duration_sec < request.interval_sec:
        raise HTTPException(status_code=422, detail="duration_sec must be >= interval_sec")
    if request.trigger_second > request.duration_sec:
        raise HTTPException(status_code=422, detail="trigger_second must be <= duration_sec")


async def sleep_until_next_sample(interval_sec: int) -> None:
    remaining = float(interval_sec)
    while remaining > 0:
        if scenario_job["stop_requested"]:
            return
        step = min(0.2, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def sleep_between_fast_samples() -> None:
    remaining = FAST_SCENARIO_SAMPLE_DELAY_SEC
    while remaining > 0:
        if scenario_job["stop_requested"]:
            return
        step = min(0.05, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def run_scenario_job(request: ScenarioPublishRequest, samples: list[dict[str, Any]]) -> None:
    signal_published = False
    try:
        for index, sample in enumerate(samples):
            if scenario_job["stop_requested"]:
                scenario_job["status"] = "stopped"
                break
            scenario_job["published_messages"] += publish_sample_pair(sample)
            if not signal_published and int(sample.get("time_offset_sec", 0)) >= request.trigger_second:
                scenario_job["published_messages"] += await publish_risk_signal(request.event_type, request.elder_id, request.event_room)
                if request.event_type == "heart_rate_baseline_anomaly":
                    await create_heart_rate_candidate(request.elder_id, request.event_room)
                elif request.event_type == "spo2_baseline_anomaly":
                    await create_spo2_candidate(request.elder_id, request.event_room)
                signal_published = True
            scenario_job["sent_samples"] += 1
            if index < len(samples) - 1:
                if request.realtime:
                    await sleep_until_next_sample(request.realtime_interval_sec)
                else:
                    await sleep_between_fast_samples()
        if scenario_job["status"] == "running":
            scenario_job["status"] = "completed"
    except Exception as exc:
        scenario_job["status"] = "failed"
        scenario_job["error"] = str(exc)
    finally:
        scenario_job["finished_at"] = utc_now()


def create_scenario_job(request: ScenarioPublishRequest, samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_id": f"scenario_{uuid4().hex[:12]}",
        "status": "running",
        "scene": request.scene,
        "event_type": request.event_type,
        "event_room": request.event_room,
        "total_samples": len(samples),
        "interval_sec": request.interval_sec,
        "realtime_interval_sec": request.realtime_interval_sec,
        "sent_samples": 0,
        "published_messages": 0,
        "stop_requested": False,
        "started_at": utc_now(),
        "finished_at": None,
        "error": None,
    }


def on_connect(client: mqtt.Client, userdata: object, flags: dict[str, Any], rc: int, properties: object = None) -> None:
    global mqtt_connected
    mqtt_connected = rc == 0
    if mqtt_connected:
        for topic in MQTT_TOPICS:
            client.subscribe(topic, qos=1)
            print(f"[MQTT] subscribed {topic}")
        print(f"[MQTT] connected {MQTT_HOST}:{MQTT_PORT}")
    else:
        print(f"[MQTT] connect failed rc={rc}")


def on_disconnect(client: mqtt.Client, userdata: object, rc: int, properties: object = None) -> None:
    global mqtt_connected
    mqtt_connected = False
    print(f"[MQTT] disconnected rc={rc}")


def on_message(client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
    payload = decode_payload(msg)
    record = append_record(msg.topic, payload)
    print(f"[MQTT] message {msg.topic} {record['sample_id']}")
    kind = topic_kind(msg.topic)
    if kind == "env":
        override = downgraded_env_overrides.pop(str(payload.get("sample_id")), None)
        env_payload = override or payload
        if env_payload.get("schema") == "home_environment_snapshot_v1" and isinstance(env_payload.get("rooms"), dict):
            update_bathroom_stay_monitor(env_payload)
            for room, room_payload in env_payload["rooms"].items():
                if isinstance(room_payload, dict):
                    update_room_env_state(
                        {
                            **room_payload,
                            "room": room,
                            "timestamp": env_payload.get("timestamp") or env_payload.get("observed_at"),
                            "elder_id": env_payload.get("elder_id", ELDER_ID),
                            "sample_id": env_payload.get("snapshot_id"),
                        }
                    )
        else:
            update_room_env_state(env_payload)
    if msg.topic.endswith("/set"):
        handle_device_set(msg.topic, payload)
    elif msg.topic.endswith("/state"):
        handle_device_state(msg.topic, payload)
    elif msg.topic.endswith("/ack"):
        handle_device_ack(msg.topic, payload)
    broadcast_from_mqtt(
        {
            "type": "mqtt_record",
            "data": record,
            "total": len(records),
            "room_env": room_env_snapshot(),
            "bathroom_stay_monitor": bathroom_stay_monitor_snapshot(),
        }
    )


@app.on_event("startup")
async def startup() -> None:
    global event_loop, mqtt_client
    event_loop = asyncio.get_running_loop()
    init_default_room_env_states()
    init_bathroom_stay_monitor()
    init_default_device_states()
    await sync_devices_from_guardian_core()
    mqtt_client = mqtt.Client(client_id=f"background-mqtt-monitor-{os.getpid()}")
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()


@app.on_event("shutdown")
async def shutdown() -> None:
    if mqtt_client is not None:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(APP_ROOT / "frontend" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "mqtt": {
            "host": MQTT_HOST,
            "port": MQTT_PORT,
            "topics": MQTT_TOPICS,
            "connected": mqtt_connected,
        },
        "record_count": len(records),
    }


@app.get("/api/records")
async def list_records(limit: int = 3100) -> dict[str, Any]:
    clipped = list(records)[: max(1, min(limit, MAX_RECORDS))]
    return {"items": clipped, "total": len(records)}


@app.get("/api/devices")
async def list_devices() -> dict[str, Any]:
    await sync_devices_from_guardian_core()
    return {
        "devices": sorted_devices(),
        "device_log": visible_device_log(),
        "room_env": room_env_snapshot(),
        "bathroom_stay_monitor": bathroom_stay_monitor_snapshot(),
    }


@app.get("/api/personal-baselines")
async def list_personal_baselines(elder_id: str = ELDER_ID) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(f"{EDGE_API_BASE}/api/v2/personal-baselines", params={"elder_id": elder_id})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge personal baseline query failed", "error": str(exc)})


@app.post("/api/personal-baselines")
async def create_personal_baseline(request: PersonalBaselineRequest) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(f"{EDGE_API_BASE}/api/v2/personal-baselines", json=request.model_dump())
            response.raise_for_status()
            if request.baseline_type == "bathroom_routine":
                bathroom_stay_monitor["bathroom_reference_limit_sec"] = float(request.metrics.get("bathroom_stay_p90_sec") or 60)
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge personal baseline save failed", "error": str(exc)})


@app.post("/api/baselines/rebuild")
async def rebuild_baselines(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    elder_id = str((payload or {}).get("elder_id") or ELDER_ID)
    try:
        return await rebuild_edge_baselines(elder_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge baseline rebuild failed", "error": str(exc)})


@app.get("/api/daily-health-summary")
async def list_daily_health_summary(elder_id: str = ELDER_ID, limit: int = 7) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(
                f"{EDGE_API_BASE}/api/v2/daily-health-summaries",
                params={"elder_id": elder_id, "limit": limit},
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge daily health summary query failed", "error": str(exc)})


@app.post("/api/daily-health-summary/generate")
async def generate_daily_health_summary(request: DailyHealthSummaryProxyRequest) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=240, trust_env=False) as client:
            response = await client.post(f"{EDGE_API_BASE}/api/v2/daily-health-summaries/generate", json=request.model_dump())
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge daily health summary generation failed", "error": str(exc)})
    await broadcast({"type": "daily_health_summary", "data": data})
    return data


@app.post("/api/baselines/auto-vitals")
async def auto_vitals_baseline(request: AutoVitalsBaselineRequest) -> dict[str, Any]:
    ensure_mqtt_connected()
    start_at = datetime.now(timezone.utc) - timedelta(seconds=request.sample_count * request.logical_interval_sec)
    for index in range(request.sample_count):
        observed_at = start_at + timedelta(seconds=index * request.logical_interval_sec)
        heart_rate, spo2 = generated_vital_values(index)
        sample = SensorVitalSample(
            elder_id=request.elder_id,
            heart_rate=heart_rate,
            spo2=spo2,
            systolic_bp=128,
            diastolic_bp=80,
            body_temperature=36.6,
            timestamp=observed_at,
        )
        if request.publish_mqtt:
            publish_model(elder_sensor_vital(request.elder_id), sample)
    if request.rebuild_delay_sec:
        await asyncio.sleep(request.rebuild_delay_sec)
    try:
        rebuild = await rebuild_edge_baselines(request.elder_id, ["heart_rate_daily", "spo2_daily"])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge baseline rebuild failed", "error": str(exc)})
    preview = vital_metric_preview(request.sample_count)
    await broadcast({"type": "auto_baseline", "data": {"kind": "vitals", "preview": preview, "rebuild": rebuild}})
    return {
        "ok": True,
        "elder_id": request.elder_id,
        "published_samples": request.sample_count if request.publish_mqtt else 0,
        "logical_interval_sec": request.logical_interval_sec,
        "logical_duration_sec": request.sample_count * request.logical_interval_sec,
        "preview": preview,
        "rebuild": rebuild,
    }


@app.post("/api/baselines/auto-bathroom")
async def auto_bathroom_baseline(request: AutoBathroomBaselineRequest) -> dict[str, Any]:
    ensure_mqtt_connected()
    durations = bathroom_stay_durations(request.stay_count, request.avg_stay_sec, request.p90_stay_sec)
    cursor = datetime.now(timezone.utc) - timedelta(seconds=sum(durations) + request.stay_count * 900)
    published = 0
    for duration in durations:
        publish_json(elder_sensor_env(request.elder_id), home_presence_snapshot(request.elder_id, "living_room", cursor, source="bathroom_baseline_generator"))
        publish_json(elder_sensor_env(request.elder_id), home_presence_snapshot(request.elder_id, "bathroom", cursor + timedelta(seconds=5), source="bathroom_baseline_generator"))
        publish_json(
            elder_sensor_env(request.elder_id),
            home_presence_snapshot(request.elder_id, "living_room", cursor + timedelta(seconds=duration + 5), source="bathroom_baseline_generator"),
        )
        cursor += timedelta(seconds=duration + 900)
        published += 3
    if request.rebuild_delay_sec:
        await asyncio.sleep(request.rebuild_delay_sec)
    try:
        rebuild = await rebuild_edge_baselines(request.elder_id, ["bathroom_routine"])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge baseline rebuild failed", "error": str(exc)})
    bathroom_stay_monitor["bathroom_reference_limit_sec"] = await bathroom_reference_limit_sec(request.elder_id)
    preview = {
        "stay_count": len(durations),
        "avg_stay_sec": round(sum(durations) / len(durations), 1),
        "reference_limit_sec": max(durations) + 10,
        "durations": durations,
    }
    await broadcast({"type": "auto_baseline", "data": {"kind": "bathroom", "preview": preview, "rebuild": rebuild}})
    return {"ok": True, "elder_id": request.elder_id, "published_snapshots": published, "preview": preview, "rebuild": rebuild}


@app.post("/api/bathroom-stay/demo")
async def bathroom_stay_demo(request: BathroomStayDemoRequest) -> dict[str, Any]:
    ensure_mqtt_connected()
    bathroom_stay_monitor["bathroom_reference_limit_sec"] = await bathroom_reference_limit_sec(request.elder_id)
    bathroom_stay_monitor.update(
        {
            "current_room": None,
            "room_presence": {room: False for room in ROOM_KEYS},
            "bathroom_present": False,
            "bathroom_entered_at": None,
            "bathroom_exited_at": None,
            "bathroom_elapsed_sec": 0,
            "status": "not_in_bathroom",
            "last_stay_seconds": None,
            "demo_flow_rows": [],
            "demo_flow_seen_sample_ids": [],
            "demo_flow_current_room": None,
            "demo_flow_room_entered_at": None,
            "demo_final_bathroom_stay_sec": None,
        }
    )
    now = datetime.now(timezone.utc)
    start_at = now - timedelta(seconds=request.duration_seconds)
    published = 0
    flow_preview: list[dict[str, Any]] = []
    pre_entry = home_presence_snapshot(request.elder_id, "living_room", start_at - timedelta(seconds=request.logical_interval_sec), source="bathroom_stay_demo")
    update_bathroom_stay_monitor(pre_entry)
    publish_json(elder_sensor_env(request.elder_id), pre_entry)
    flow_preview.append({"room": "living_room", "observed_at": pre_entry["observed_at"], "elapsed_sec": 0})
    published += 1
    steps = max(1, int(math.ceil(request.duration_seconds / request.logical_interval_sec)))
    for index in range(steps + 1):
        elapsed_sec = min(index * request.logical_interval_sec, request.duration_seconds)
        observed_at = start_at + timedelta(seconds=elapsed_sec)
        snapshot = home_presence_snapshot(request.elder_id, "bathroom", observed_at, source="bathroom_stay_demo")
        update_bathroom_stay_monitor(snapshot)
        publish_json(elder_sensor_env(request.elder_id), snapshot)
        if index in {0, steps} or index % max(1, steps // 4) == 0:
            flow_preview.append({"room": "bathroom", "observed_at": snapshot["observed_at"], "elapsed_sec": int(elapsed_sec)})
        published += 1
    exit_at = start_at + timedelta(seconds=request.duration_seconds + request.logical_interval_sec)
    exit_entry = home_presence_snapshot(request.elder_id, "living_room", exit_at, source="bathroom_stay_demo")
    exit_entry["bathroom_stay_completed_sec"] = request.duration_seconds
    update_bathroom_stay_monitor(exit_entry)
    publish_json(elder_sensor_env(request.elder_id), exit_entry)
    bathroom_stay_monitor["last_stay_seconds"] = request.duration_seconds
    flow_preview.append({"room": "living_room", "observed_at": exit_entry["observed_at"], "elapsed_sec": request.duration_seconds})
    published += 1
    if request.rebuild_delay_sec:
        await asyncio.sleep(request.rebuild_delay_sec)
    try:
        rebuild = await rebuild_edge_baselines(request.elder_id, ["bathroom_routine"])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge baseline rebuild failed", "error": str(exc)})
    bathroom_stay_monitor["bathroom_reference_limit_sec"] = await bathroom_reference_limit_sec(request.elder_id)
    bathroom_stay_monitor["demo_final_bathroom_stay_sec"] = request.duration_seconds
    monitor = bathroom_stay_monitor_snapshot()
    await broadcast(
        {
            "type": "bathroom_stay_demo",
            "data": {
                "duration_seconds": request.duration_seconds,
                "logical_interval_sec": request.logical_interval_sec,
                "published_snapshots": published,
                "flow_preview": flow_preview,
                "rebuild": rebuild,
            },
            "bathroom_stay_monitor": monitor,
        }
    )
    return {
        "ok": True,
        "elder_id": request.elder_id,
        "duration_seconds": request.duration_seconds,
        "logical_interval_sec": request.logical_interval_sec,
        "published_snapshots": published,
        "flow_preview": flow_preview,
        "bathroom_stay_monitor": monitor,
        "rebuild": rebuild,
    }


@app.post("/api/device/command")
async def command_device(request: DeviceCommandRequest) -> dict[str, Any]:
    payload = request.model_dump()
    key = device_key(request.room, request.device)
    if key not in guardian_core_device_keys:
        result = simulate_local_device_command({**payload, "elder_id": ELDER_ID})
        await broadcast({"type": "manual_command", "data": {"payload": payload, "result": result}, "devices": sorted_devices(), "device_log": visible_device_log()})
        return result
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(f"{GUARDIAN_CORE_URL}/api/home/device/command", json=payload)
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPError as exc:
        error = {"message": "guardian-core command failed", "error": str(exc), "payload": payload}
        append_device_log("error", error)
        await broadcast({"type": "device_error", "data": error, "devices": sorted_devices(), "device_log": visible_device_log()})
        raise HTTPException(status_code=502, detail=error)
    append_device_log("manual_command", {"payload": payload, "result": result})
    await broadcast({"type": "manual_command", "data": {"payload": payload, "result": result}, "devices": sorted_devices(), "device_log": visible_device_log()})
    return result


@app.get("/api/scenario/options")
async def scenario_options() -> dict[str, Any]:
    return {
        "scenes": [{"value": key, "label": label} for key, label in SCENE_LABELS.items()],
        "events": [{"value": key, "label": label} for key, label in EVENT_LABELS.items()],
    }


@app.get("/api/scenario/status")
async def scenario_status() -> dict[str, Any]:
    return scenario_status_snapshot()


@app.post("/api/scenario/start")
async def start_scenario(request: ScenarioPublishRequest) -> dict[str, Any]:
    global scenario_job, scenario_task
    validate_scenario_request(request)
    if scenario_job["status"] == "running":
        raise HTTPException(status_code=409, detail="scenario already running")
    ensure_mqtt_connected()
    await sync_devices_from_guardian_core()
    if request.event_type == "bathroom_stay_anomaly_demo":
        bathroom_p90 = await current_bathroom_stay_p90(request.elder_id)
        required_duration = min(600, request.trigger_second + bathroom_p90 + 15)
        if required_duration > request.duration_sec:
            request = request.model_copy(update={"duration_sec": required_duration})
    samples = build_event_samples(
        request.scene,
        request.event_type,
        request.trigger_second,
        request.duration_sec,
        request.interval_sec,
        request.elder_id,
        request.event_room,
    )
    scenario_job = create_scenario_job(request, samples)
    scenario_task = asyncio.create_task(run_scenario_job(request, samples))
    return {"ok": True, "samples": len(samples), **scenario_status_snapshot()}


@app.post("/api/scenario/stop")
async def stop_scenario() -> dict[str, Any]:
    if scenario_job["status"] != "running":
        raise HTTPException(status_code=409, detail="no scenario is running")
    scenario_job["stop_requested"] = True
    return {"ok": True, **scenario_status_snapshot()}


@app.post("/api/scenario/publish")
async def publish_scenario(request: ScenarioPublishRequest) -> dict[str, Any]:
    return await start_scenario(request)


@app.post("/api/manual/vital")
async def submit_manual_vital(sample: SensorVitalSample) -> dict[str, Any]:
    publish_model(elder_sensor_vital(sample.elder_id), sample)
    return {"ok": True, "kind": "vital", "elder_id": sample.elder_id}


@app.post("/api/manual/env")
async def submit_manual_env(sample: SensorEnvSample) -> dict[str, Any]:
    publish_model(elder_sensor_env(sample.elder_id), sample)
    return {"ok": True, "kind": "env", "elder_id": sample.elder_id}


@app.post("/api/manual/risk-event")
async def submit_manual_risk_event(request: ManualRiskEventRequest) -> dict[str, Any]:
    if request.event_type not in EVENT_LABELS:
        raise HTTPException(status_code=422, detail=f"unknown event_type: {request.event_type}")
    if request.room not in ROOM_KEYS:
        raise HTTPException(status_code=422, detail=f"unknown room: {request.room}")
    vital, env = manual_risk_samples(request.event_type, request.elder_id, request.room)
    publish_model(elder_sensor_vital(request.elder_id), vital)
    publish_model(elder_sensor_env(request.elder_id), env)
    messages = 2 + await publish_risk_signal(request.event_type, request.elder_id, request.room)
    if request.event_type == "heart_rate_baseline_anomaly":
        await create_heart_rate_candidate(request.elder_id, request.room)
    elif request.event_type == "spo2_baseline_anomaly":
        await create_spo2_candidate(request.elder_id, request.room)
    elif request.event_type == "bathroom_stay_anomaly_demo":
        await create_bathroom_stay_candidate(request.elder_id)
    return {"ok": True, "event_type": request.event_type, "elder_id": request.elder_id, "messages": messages}


@app.post("/api/vision/captures")
async def proxy_vision_capture(request: VisionCaptureProxyRequest) -> dict[str, Any]:
    try:
        return await vision_capture(request)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"vision capture failed: {exc}") from exc


@app.get("/api/vision/captures/recent")
async def proxy_recent_vision_captures(elder_id: str = "elder_001", camera_id: str = "living_room") -> dict[str, Any]:
    try:
        return await recent_vision_captures(elder_id, camera_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"vision recent captures failed: {exc}") from exc


@app.post("/api/vision/captures/clear")
async def proxy_clear_vision_captures(request: VisionCaptureClearProxyRequest) -> dict[str, Any]:
    try:
        return await clear_vision_captures(request)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"vision capture clear failed: {exc}") from exc


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    connections.add(websocket)
    await websocket.send_json(
        {
            "type": "snapshot",
            "data": list(records)[:MAX_RECORDS],
            "total": len(records),
            "devices": sorted_devices(),
            "device_log": visible_device_log(),
            "room_env": room_env_snapshot(),
            "bathroom_stay_monitor": bathroom_stay_monitor_snapshot(),
        }
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.discard(websocket)
