from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeviceReadingTests(unittest.TestCase):
    def test_device_reading_http_api(self) -> None:
        script = textwrap.dedent(
            """
            from fastapi.testclient import TestClient

            from app.database import Base, engine
            from app.main import app

            Base.metadata.create_all(bind=engine)
            client = TestClient(app)
            payload = {
                "elder_id": "elder_001",
                "device_id": "sht30_bathroom_01",
                "device_type": "temperature_humidity_sensor",
                "room": "bathroom",
                "source": "real_device",
                "metrics": {"temperature": 25.2, "humidity": 61.5},
                "units": {"temperature": "°C", "humidity": "%"},
            }
            created = client.post("/api/v2/device-readings", json=payload)
            assert created.status_code == 200, created.text
            assert created.json()["device_reading"]["device_id"] == "sht30_bathroom_01"

            latest = client.get("/api/v2/device-readings/latest?elder_id=elder_001")
            assert latest.status_code == 200, latest.text
            readings = latest.json()["device_readings_latest"]
            assert readings[0]["device_id"] == "sht30_bathroom_01"
            assert readings[0]["metrics"]["humidity"] == 61.5

            state = client.get("/api/v2/dashboard/state?elder_id=elder_001").json()
            assert state["device_readings_latest"][0]["device_id"] == "sht30_bathroom_01"
            assert state["events"] == []
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{Path(directory, 'device_readings_api.db').as_posix()}"
            env["ORCHESTRATOR_URL"] = ""
            paths = [ROOT / "apps" / "edge-mcp-server", ROOT / "packages" / "guardian-shared"]
            env["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_device_readings_are_display_only_and_available_to_dashboard(self) -> None:
        script = textwrap.dedent(
            """
            import asyncio
            import json

            from app.database import Base, engine, SessionLocal
            from app import repository
            from app.mqtt_bridge import MqttBridge
            from guardian_shared.v2 import DeviceReadingV2

            Base.metadata.create_all(bind=engine)

            with SessionLocal() as db:
                record = repository.create_device_reading(
                    db,
                    DeviceReadingV2(
                        elder_id="elder_001",
                        device_id="dht22_living_room_01",
                        device_type="temperature_humidity_sensor",
                        room="living_room",
                        metrics={"temperature": 24.6, "humidity": 51.2},
                        units={"temperature": "°C", "humidity": "%"},
                    ),
                )
                assert record["metrics"]["temperature"] == 24.6
                assert record["online"] is True

                state = repository.dashboard_state(db, "elder_001")
                assert len(state["device_readings_latest"]) == 1
                assert state["device_readings_latest"][0]["device_id"] == "dht22_living_room_01"
                assert state["events"] == []
                assert state["workflow_steps"] == []

            bridge = MqttBridge()
            payload = {
                "device_type": "temperature_humidity_sensor",
                "room": "bedroom",
                "temperature": 35.0,
                "humidity": 90.0,
            }
            asyncio.run(
                bridge._handle_message(
                    "elder/elder_001/device/dht22_bedroom_01/telemetry",
                    json.dumps(payload).encode("utf-8"),
                )
            )

            with SessionLocal() as db:
                latest = repository.latest_device_readings(db, "elder_001")
                device_ids = {item["device_id"] for item in latest}
                assert "dht22_bedroom_01" in device_ids
                observations = repository.list_observations(db, "elder_001")
                assert observations == []
                state = repository.dashboard_state(db, "elder_001")
                assert state["events"] == []
                assert state["workflows"] == []
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{Path(directory, 'device_readings.db').as_posix()}"
            env["ORCHESTRATOR_URL"] = ""
            paths = [ROOT / "apps" / "edge-mcp-server", ROOT / "packages" / "guardian-shared"]
            env["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
