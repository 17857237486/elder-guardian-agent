from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from datetime import datetime, timezone
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
from guardian_shared.topics import elder_sensor_env, elder_sensor_vital, elder_vision_event, home_device_ack, home_device_state
from guardian_shared.utils import model_to_json

MQTT_HOST = os.getenv("BACKGROUND_MQTT_HOST", os.getenv("MQTT_HOST", "localhost"))
MQTT_PORT = int(os.getenv("BACKGROUND_MQTT_PORT", os.getenv("MQTT_PORT", "1883")))
MQTT_TOPICS = ("elder/+/sensor/vital", "elder/+/sensor/env", "elder/+/vision/event", "home/+/+/set", "home/+/+/state", "home/+/+/ack")
GUARDIAN_CORE_URL = os.getenv("GUARDIAN_CORE_URL", "http://localhost:8000").rstrip("/")
EDGE_API_BASE = os.getenv("EDGE_API_BASE", "http://edge-mcp-server:8010").rstrip("/")
ELDER_ID = os.getenv("ELDER_ID", "elder_001")
MAX_RECORDS = int(os.getenv("BACKGROUND_MAX_RECORDS", "1000"))
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
    state = {
        **room_env_states.get(room, default_room_env_state(room)),
        "room": room,
        "temperature": payload.get("temperature"),
        "humidity": payload.get("humidity"),
        "co2_ppm": payload.get("co2_ppm"),
        "timestamp": payload.get("timestamp"),
        "updated_at": utc_now(),
        "sample_id": payload.get("sample_id"),
        "elder_id": payload.get("elder_id", ELDER_ID),
    }
    room_env_states[room] = state
    return state


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
    publish_model(elder_sensor_env(sample["elder_id"]), env_sample)
    return 2


def publish_risk_signal(event_type: str, elder_id: str, room: str) -> int:
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
        publish_json(
            elder_vision_event(elder_id),
            {
                "elder_id": elder_id,
                "room": room,
                "event_type": event_type,
                "confidence": 0.92 if event_type == "suspected_fall" else 0.78,
                "posture": "lying" if event_type == "suspected_fall" else "unknown",
                "motion_state": "static",
                "timestamp": utc_now(),
            },
        )
        return 1
    return 0


def manual_risk_samples(event_type: str, elder_id: str, room: str) -> tuple[SensorVitalSample, SensorEnvSample]:
    vital = {"heart_rate": 78, "spo2": 96, "systolic_bp": 128, "diastolic_bp": 80, "body_temperature": 36.6}
    env = {"temperature": 25.0, "humidity": 50.0, "co2_ppm": 900, "gas_ppm": 0, "smoke_ppm": 0}
    if event_type == "spo2_critical":
        vital.update(heart_rate=88, spo2=86)
    elif event_type == "spo2_low":
        vital.update(heart_rate=88, spo2=90)
    elif event_type == "heart_rate_abnormal":
        vital.update(heart_rate=138)
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
                scenario_job["published_messages"] += publish_risk_signal(request.event_type, request.elder_id, request.event_room)
                signal_published = True
            scenario_job["sent_samples"] += 1
            if index < len(samples) - 1:
                if request.realtime:
                    await sleep_until_next_sample(request.interval_sec)
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
        update_room_env_state(override or payload)
    if msg.topic.endswith("/set"):
        handle_device_set(msg.topic, payload)
    elif msg.topic.endswith("/state"):
        handle_device_state(msg.topic, payload)
    elif msg.topic.endswith("/ack"):
        handle_device_ack(msg.topic, payload)
    broadcast_from_mqtt({"type": "mqtt_record", "data": record, "total": len(records), "room_env": room_env_snapshot()})


@app.on_event("startup")
async def startup() -> None:
    global event_loop, mqtt_client
    event_loop = asyncio.get_running_loop()
    init_default_room_env_states()
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
async def list_records(limit: int = 300) -> dict[str, Any]:
    clipped = list(records)[: max(1, min(limit, MAX_RECORDS))]
    return {"items": clipped, "total": len(records)}


@app.get("/api/devices")
async def list_devices() -> dict[str, Any]:
    await sync_devices_from_guardian_core()
    return {"devices": sorted_devices(), "device_log": visible_device_log(), "room_env": room_env_snapshot()}


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
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"message": "edge personal baseline save failed", "error": str(exc)})


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
    messages = 2 + publish_risk_signal(request.event_type, request.elder_id, request.room)
    return {"ok": True, "event_type": request.event_type, "elder_id": request.elder_id, "messages": messages}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    connections.add(websocket)
    await websocket.send_json(
        {
            "type": "snapshot",
            "data": list(records)[:300],
            "total": len(records),
            "devices": sorted_devices(),
            "device_log": visible_device_log(),
            "room_env": room_env_snapshot(),
        }
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.discard(websocket)
