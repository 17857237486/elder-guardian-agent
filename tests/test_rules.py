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
    def test_abnormal_heart_rate_is_p1(self) -> None:
        event = rules.classify_observation(
            {
                "observation_id": "obs-heart-rate",
                "elder_id": "elder_001",
                "kind": "vital",
                "payload": {"room": "living_room", "heart_rate": 138, "spo2": 96},
                "observed_at": datetime(2026, 6, 14, 15, 30, tzinfo=timezone.utc).isoformat(),
            }
        )
        self.assertIsNotNone(event)
        self.assertEqual(str(event.event_type), "heart_rate_abnormal")
        self.assertEqual(str(event.risk_level), "P1")

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

    def test_p3_environment_rules_require_presence_when_present_field_exists(self) -> None:
        absent_event = rules.classify_observation(
            {
                "observation_id": "obs-absent-hot",
                "elder_id": "elder_001",
                "kind": "environment",
                "payload": {"room": "kitchen", "temperature": 31, "humidity": 50, "presence": False},
            }
        )
        self.assertIsNone(absent_event)

        present_event = rules.classify_observation(
            {
                "observation_id": "obs-present-hot",
                "elder_id": "elder_001",
                "kind": "environment",
                "payload": {"room": "living_room", "temperature": 31, "humidity": 50, "presence": True},
            }
        )
        self.assertIsNotNone(present_event)
        self.assertEqual(str(present_event.event_type), "temperature_high")
        self.assertEqual(str(present_event.risk_level), "P3")

    def test_gas_leak_ignores_presence_filter(self) -> None:
        event = rules.classify_observation(
            {
                "observation_id": "obs-gas-absent",
                "elder_id": "elder_001",
                "kind": "environment",
                "payload": {"room": "kitchen", "gas_ppm": 180, "presence": False},
            }
        )
        self.assertIsNotNone(event)
        self.assertEqual(str(event.event_type), "gas_leak")
        self.assertEqual(str(event.risk_level), "P0")

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

    def test_smoke_alone_does_not_trigger_gas_event(self) -> None:
        event = rules.classify_observation(
            {
                "observation_id": "obs-smoke",
                "elder_id": "elder_001",
                "kind": "environment",
                "payload": {"room": "kitchen", "gas_ppm": 0, "smoke_ppm": 120},
            }
        )
        self.assertIsNone(event)


if __name__ == "__main__":
    unittest.main()
