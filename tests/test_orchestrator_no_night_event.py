from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app.main import app


class OrchestratorNoNightEventTests(unittest.TestCase):
    def test_bedroom_absence_observation_does_not_trigger_risk_event(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/api/v2/orchestrator/observations",
            json={
                "observation_id": "obs-bedroom-absent",
                "elder_id": "elder_001",
                "kind": "device_state",
                "source": "presence_sensor",
                "payload": {
                    "room": "bedroom",
                    "device": "presence_sensor",
                    "present": False,
                    "state": "absent",
                },
                "observed_at": "2026-06-18T22:00:00+08:00",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "triggered": False})


if __name__ == "__main__":
    unittest.main()
