from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app import rules


class RuleTests(unittest.TestCase):
    def test_abnormal_humidity_is_screened_at_level_one(self) -> None:
        event = rules.classify_observation(
            {
                "observation_id": "obs-humidity",
                "elder_id": "elder_001",
                "kind": "environment",
                "payload": {"room": "living_room", "humidity": 82},
                "observed_at": datetime(2026, 6, 14, 15, 30, tzinfo=timezone.utc).isoformat(),
            }
        )
        self.assertIsNotNone(event)
        self.assertEqual(str(event.event_type), "humidity_abnormal")
        self.assertEqual(str(event.risk_level), "P3")

    def test_old_night_composites_and_direct_visual_event_are_removed(self) -> None:
        observations = [
            {"kind": "device_state", "payload": {"room": "bathroom", "present": True}},
            {"kind": "device_state", "payload": {"room": "bathroom", "device": "light", "state": "on"}},
            {"kind": "device_state", "payload": {"room": "hall", "device": "door", "state": "open"}},
            {"kind": "vital", "payload": {"heart_rate": 110, "spo2": 96}},
            {"kind": "vision", "payload": {"room": "bedroom", "event_type": "night_abnormal_activity"}},
        ]
        for index, observation in enumerate(observations):
            observation.update({"observation_id": f"obs-{index}", "elder_id": "elder_001"})
            self.assertIsNone(rules.classify_observation(observation))


if __name__ == "__main__":
    unittest.main()
