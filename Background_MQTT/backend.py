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
from guardian_shared.schemas import SensorEnvSample, SensorVitalSample
from guardian_shared.topics import elder_sensor_env, elder_sensor_vital
from guardian_shared.utils import model_to_json

MQTT_HOST = os.getenv("BACKGROUND_MQTT_HOST", os.getenv("MQTT_HOST", "localhost"))
MQTT_PORT = int(os.getenv("BACKGROUND_MQTT_PORT", os.getenv("MQTT_PORT", "1883")))
MQTT_TOPICS = ("elder/+/sensor/vital", "elder/+/sensor/env")
MAX_RECORDS = int(os.getenv("BACKGROUND_MAX_RECORDS", "1000"))
EDGE_API_BASE = os.getenv("EDGE_API_BASE", "http://localhost:8010").rstrip("/")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8020").rstrip("/")

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
scenario_task: asyncio.Task[None] | None = None
scenario_job: dict[str, Any] = {
    "run_id": None,
    "status": "idle",
    "scene": None,
    "event_type": None,
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
    trigger_second: int = Field(default=60, ge=0, le=120)
    elder_id: str = Field(default="elder_001")
    duration_sec: int = Field(default=120, ge=5, le=600)
    interval_sec: int = Field(default=5, ge=1, le=60)
    realtime: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def topic_kind(topic: str) -> str:
    if topic.endswith("/sensor/vital"):
        return "vital"
    if topic.endswith("/sensor/env"):
        return "env"
    return "unknown"


def elder_id_from_topic(topic: str) -> str:
    parts = topic.split("/")
    return parts[1] if len(parts) >= 2 else "unknown"


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


def observation_kind_for_topic(topic: str) -> str:
    if topic.endswith("/sensor/vital"):
        return "vital"
    if topic.endswith("/sensor/env"):
        return "environment"
    return "vital"


def post_observation_to_edge(topic: str, payload: SensorVitalSample | SensorEnvSample) -> None:
    body = {
        "elder_id": payload.elder_id,
        "kind": observation_kind_for_topic(topic),
        "source": "background_mqtt_http_fallback",
        "topic": topic,
        "payload": payload.model_dump(mode="json"),
    }
    try:
        response = httpx.post(f"{EDGE_API_BASE}/api/v2/observations", json=body, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MQTT not connected and edge fallback failed: {exc}") from exc


def publish_model(topic: str, payload: SensorVitalSample | SensorEnvSample) -> None:
    if mqtt_client is None or not mqtt_connected:
        post_observation_to_edge(topic, payload)
        return
    result = mqtt_client.publish(topic, model_to_json(payload), qos=1)
    result.wait_for_publish()
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise HTTPException(status_code=503, detail=f"MQTT publish failed rc={result.rc}")


def ensure_mqtt_connected() -> None:
    if mqtt_client is None or not mqtt_connected:
        raise HTTPException(status_code=503, detail="MQTT not connected")


def publish_sample_pair(sample: dict[str, Any]) -> int:
    vital_sample, env_sample = to_standard_samples(sample)
    publish_model(elder_sensor_vital(sample["elder_id"]), vital_sample)
    publish_model(elder_sensor_env(sample["elder_id"]), env_sample)
    return 2


def scenario_status_snapshot() -> dict[str, Any]:
    return dict(scenario_job)


def validate_scenario_request(request: ScenarioPublishRequest) -> None:
    if request.scene not in SCENE_LABELS:
        raise HTTPException(status_code=422, detail=f"unknown scene: {request.scene}")
    if request.event_type not in EVENT_LABELS:
        raise HTTPException(status_code=422, detail=f"unknown event_type: {request.event_type}")
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


async def run_scenario_job(request: ScenarioPublishRequest, samples: list[dict[str, Any]]) -> None:
    try:
        for index, sample in enumerate(samples):
            if scenario_job["stop_requested"]:
                scenario_job["status"] = "stopped"
                break
            scenario_job["published_messages"] += publish_sample_pair(sample)
            scenario_job["sent_samples"] += 1
            if request.realtime and index < len(samples) - 1:
                await sleep_until_next_sample(request.interval_sec)
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
    if event_loop is not None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "mqtt_record", "data": record, "total": len(records)}),
            event_loop,
        )


@app.on_event("startup")
async def startup() -> None:
    global event_loop, mqtt_client
    event_loop = asyncio.get_running_loop()
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


async def fetch_json(url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


@app.get("/api/v2/guardian/health")
async def guardian_v2_health() -> dict[str, Any]:
    edge = await fetch_json(f"{EDGE_API_BASE}/health")
    orchestrator = await fetch_json(f"{ORCHESTRATOR_URL}/health")
    return {
        "ok": bool(edge.get("ok")) and bool(orchestrator.get("ok")) and mqtt_connected,
        "mqtt_connected": mqtt_connected,
        "edge": edge,
        "orchestrator": orchestrator,
    }


@app.get("/api/v2/guardian/state")
async def guardian_v2_state(elder_id: str = "elder_001") -> dict[str, Any]:
    return await fetch_json(f"{EDGE_API_BASE}/api/v2/dashboard/state?elder_id={elder_id}")


@app.get("/api/records")
async def list_records(limit: int = 300) -> dict[str, Any]:
    clipped = list(records)[: max(1, min(limit, MAX_RECORDS))]
    return {"items": clipped, "total": len(records)}


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
    samples = build_event_samples(
        request.scene,
        request.event_type,
        request.trigger_second,
        request.duration_sec,
        request.interval_sec,
        request.elder_id,
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    connections.add(websocket)
    await websocket.send_json({"type": "snapshot", "data": list(records)[:300], "total": len(records)})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.discard(websocket)
