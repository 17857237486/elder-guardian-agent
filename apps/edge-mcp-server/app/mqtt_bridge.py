from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import paho.mqtt.client as mqtt

from guardian_shared.schemas import new_id
from guardian_shared.topics import ELDER_TOPIC_PATTERNS, HOME_TOPIC_PATTERNS, home_device_set
from guardian_shared.utils import model_to_json, parse_json_payload
from guardian_shared.v2 import DeviceReadingV2, ObservationKind, RawObservationV2

from app.config import settings
from app.database import SessionLocal
from app import repository

logger = logging.getLogger(__name__)


def _kind_from_topic(parts: list[str]) -> ObservationKind | None:
    if len(parts) >= 4 and parts[0] == "elder" and parts[2] == "sensor" and parts[3] == "vital":
        return ObservationKind.VITAL
    if len(parts) >= 4 and parts[0] == "elder" and parts[2] == "sensor" and parts[3] == "env":
        return ObservationKind.ENVIRONMENT
    if len(parts) >= 3 and parts[0] == "elder" and parts[2] == "vision":
        return ObservationKind.VISION
    if len(parts) >= 4 and parts[0] == "elder" and parts[2] == "hmi" and parts[3] == "response":
        return ObservationKind.HMI_RESPONSE
    if len(parts) >= 4 and parts[0] == "home" and parts[3] == "state":
        return ObservationKind.DEVICE_STATE
    if len(parts) >= 4 and parts[0] == "home" and parts[3] == "ack":
        return ObservationKind.DEVICE_ACK
    return None


def _is_device_telemetry_topic(parts: list[str]) -> bool:
    return len(parts) >= 5 and parts[0] == "elder" and parts[2] == "device" and parts[4] == "telemetry"


def _telemetry_metrics(data: dict[str, Any]) -> dict[str, Any]:
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    return {
        key: data[key]
        for key in ("temperature", "humidity", "battery", "rssi", "illuminance")
        if key in data
    }


def _is_home_environment_snapshot(data: dict[str, Any]) -> bool:
    return data.get("schema") == "home_environment_snapshot_v1" and isinstance(data.get("rooms"), dict)


def _presence_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "present"}:
            return True
        if normalized in {"0", "false", "no", "off", "absent"}:
            return False
    return bool(value)


def _snapshot_observations(elder_id: str, topic: str, data: dict[str, Any]) -> list[RawObservationV2]:
    snapshot_id = str(data.get("snapshot_id") or new_id("envsnap"))
    source = str(data.get("source") or "mqtt")
    observed_at = data.get("observed_at") or data.get("timestamp")
    observations: list[RawObservationV2] = []
    rooms = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
    for room, raw_room_payload in rooms.items():
        if not isinstance(raw_room_payload, dict):
            continue
        room_name = str(room)
        room_payload = {
            **raw_room_payload,
            "room": room_name,
            "snapshot_id": snapshot_id,
            "source_snapshot_schema": "home_environment_snapshot_v1",
        }
        common = {
            "elder_id": elder_id,
            "source": source,
            "topic": topic,
            "observed_at": observed_at,
        }
        observations.append(
            RawObservationV2(
                kind=ObservationKind.ENVIRONMENT,
                payload=room_payload,
                **({key: value for key, value in common.items() if value is not None}),
            )
        )
        if "presence" in raw_room_payload:
            present = _presence_bool(raw_room_payload.get("presence"))
            observations.append(
                RawObservationV2(
                    kind=ObservationKind.DEVICE_STATE,
                    payload={
                        "room": room_name,
                        "device": "pir_presence",
                        "present": present,
                        "state": "present" if present else "absent",
                        "snapshot_id": snapshot_id,
                        "source_snapshot_schema": "home_environment_snapshot_v1",
                    },
                    **({key: value for key, value in common.items() if value is not None}),
                )
            )
    return observations


class MqttBridge:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.client = mqtt.Client(client_id=f"edge-mcp-{settings.elder_id}")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        try:
            self.client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
            self.client.loop_start()
            logger.info("MQTT bridge connecting to %s:%s", settings.mqtt_host, settings.mqtt_port)
        except Exception:
            logger.exception("MQTT broker unavailable; edge MCP HTTP still runs")

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def publish_command(self, room: str, device: str, payload: Any) -> tuple[bool, str]:
        topic = home_device_set(room, device)
        if not self.connected:
            if settings.simulate_device_when_mqtt_unavailable:
                self._simulate_device_feedback(room, device, payload)
                return True, f"simulated://{topic}"
            return False, "MQTT not connected"
        result = self.client.publish(topic, model_to_json(payload), qos=1)
        return result.rc == mqtt.MQTT_ERR_SUCCESS, topic

    def _simulate_device_feedback(self, room: str, device: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            payload = {}
        action = str(payload.get("action") or "")
        value = payload.get("value")
        state = "unknown"
        if action == "open":
            state = "open"
        elif action == "close":
            state = "closed"
        elif action in {"turn_on", "alarm_on"}:
            state = "on"
        elif action in {"turn_off", "alarm_off"}:
            state = "off"
        elif action == "set_temperature":
            state = "on"
        elder_id = payload.get("elder_id") or settings.elder_id
        ack_payload = {"cmd_id": payload.get("cmd_id"), "status": "acked", "room": room, "device": device}
        state_payload = {"elder_id": elder_id, "room": room, "device": device, "state": state, "value": value, "online": True}
        with SessionLocal() as db:
            repository.create_observation(
                db,
                RawObservationV2(
                    elder_id=elder_id,
                    kind=ObservationKind.DEVICE_ACK,
                    source="simulated_device_no_mqtt",
                    topic=f"home/{room}/{device}/ack",
                    payload=ack_payload,
                ),
            )
            repository.create_observation(
                db,
                RawObservationV2(
                    elder_id=elder_id,
                    kind=ObservationKind.DEVICE_STATE,
                    source="simulated_device_no_mqtt",
                    topic=f"home/{room}/{device}/state",
                    payload=state_payload,
                ),
            )

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: dict[str, Any], rc: int, properties: Any = None) -> None:
        self.connected = rc == 0
        if rc == 0:
            for topic in [*ELDER_TOPIC_PATTERNS, *HOME_TOPIC_PATTERNS]:
                client.subscribe(topic, qos=1)
        else:
            logger.error("MQTT bridge connect failed rc=%s", rc)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int, properties: Any = None) -> None:
        self.connected = False

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._handle_message(msg.topic, msg.payload), self.loop)

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        data = parse_json_payload(payload)
        parts = topic.split("/")
        if _is_device_telemetry_topic(parts):
            elder_id = data.get("elder_id") or parts[1]
            device_id = data.get("device_id") or parts[3]
            payload = {
                "elder_id": elder_id,
                "device_id": device_id,
                "device_type": str(data.get("device_type") or "unknown"),
                "room": str(data.get("room") or "living_room"),
                "source": str(data.get("source") or "real_device"),
                "metrics": _telemetry_metrics(data),
                "units": data.get("units") if isinstance(data.get("units"), dict) else {},
                "topic": topic,
            }
            if data.get("observed_at") or data.get("timestamp"):
                payload["observed_at"] = data.get("observed_at") or data.get("timestamp")
            reading = DeviceReadingV2(
                **payload,
            )
            with SessionLocal() as db:
                repository.create_device_reading(db, reading)
            return
        kind = _kind_from_topic(parts)
        if kind is None:
            logger.warning("Unhandled MQTT topic %s", topic)
            return
        elder_id = data.get("elder_id") or (parts[1] if parts and parts[0] == "elder" else settings.elder_id)
        if kind == ObservationKind.ENVIRONMENT and _is_home_environment_snapshot(data):
            observations = _snapshot_observations(str(elder_id), topic, data)
            with SessionLocal() as db:
                records = [repository.create_observation(db, observation) for observation in observations]
            if settings.orchestrator_url:
                for record in records:
                    asyncio.create_task(self._forward_to_orchestrator(record))
            return
        observation_payload = {
            "elder_id": elder_id,
            "kind": kind,
            "source": "mqtt",
            "topic": topic,
            "payload": data,
        }
        if data.get("observed_at") or data.get("timestamp"):
            observation_payload["observed_at"] = data.get("observed_at") or data.get("timestamp")
        observation = RawObservationV2(**observation_payload)
        with SessionLocal() as db:
            record = repository.create_observation(db, observation)
        if settings.orchestrator_url:
            asyncio.create_task(self._forward_to_orchestrator(record))

    async def _forward_to_orchestrator(self, record: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=240) as client:
                response = await client.post(f"{settings.orchestrator_url.rstrip('/')}/api/v2/orchestrator/observations", json=record)
                response.raise_for_status()
        except Exception:
            logger.exception("Failed to forward observation to orchestrator")
