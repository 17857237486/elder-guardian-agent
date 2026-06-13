from __future__ import annotations

import argparse
import os
from pathlib import Path

import paho.mqtt.client as mqtt
from fastapi import FastAPI

from guardian_shared.enums import EventType
from guardian_shared.schemas import VisionEvent
from guardian_shared.topics import elder_vision_event
from guardian_shared.utils import model_to_json

app = FastAPI(title="Mock Vision Service")
ROOT = Path(__file__).resolve().parents[3]


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "vision-service",
        "mqtt_host": os.getenv("MQTT_HOST", "localhost"),
        "mqtt_port": int(os.getenv("MQTT_PORT", "1883")),
    }


def publish_event(event_name: str, elder_id: str, room: str = "living_room") -> VisionEvent:
    specs = {
        "suspected_fall": (0.86, "lying_low", "sudden_down"),
        "long_static": (0.72, "sitting_or_lying", "static_45min"),
        "night_abnormal_activity": (0.77, "standing", "night_wandering"),
        "normal": (0.98, "standing", "normal"),
    }
    confidence, posture, motion_state = specs[event_name]
    event = VisionEvent(
        elder_id=elder_id,
        room=room,
        event_type=EventType(event_name),
        confidence=confidence,
        posture=posture,
        motion_state=motion_state,
        snapshot_path=str(ROOT / "data" / "snapshots" / f"{event_name}.jpg"),
    )
    client = mqtt.Client(client_id="vision-service-mock")
    client.connect(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883")), keepalive=30)
    client.publish(elder_vision_event(elder_id), model_to_json(event), qos=1)
    client.disconnect()
    return event


@app.post("/simulate/{event_name}")
async def simulate(event_name: str, elder_id: str = "elder_001", room: str = "living_room") -> dict:
    event = publish_event(event_name, elder_id, room)
    return {"ok": True, "event": event.model_dump(mode="json")}


def cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=["suspected_fall", "long_static", "night_abnormal_activity", "normal"], required=True)
    parser.add_argument("--elder-id", default=os.getenv("ELDER_ID", "elder_001"))
    parser.add_argument("--room", default="living_room")
    args = parser.parse_args()
    event = publish_event(args.event, args.elder_id, args.room)
    print(model_to_json(event))


if __name__ == "__main__":
    cli()

