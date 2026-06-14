from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app import rules


class FixedDateTime(datetime):
    fixed_now = datetime(2026, 6, 14, 23, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        value = cls.fixed_now
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        rules.HISTORY.clear()
        rules.LAST_COMPOSITE_EVENT.clear()

    def test_abnormal_humidity_is_screened_at_level_one(self) -> None:
        event = rules.classify_observation(
            {
                "observation_id": "obs-humidity",
                "elder_id": "elder_001",
                "kind": "environment",
                "payload": {"room": "living_room", "humidity": 82},
                "observed_at": FixedDateTime.fixed_now.isoformat(),
            }
        )
        self.assertIsNotNone(event)
        self.assertEqual(str(event.event_type), "humidity_abnormal")
        self.assertEqual(str(event.risk_level), "P3")

    @patch("app.rules.datetime", FixedDateTime)
    def test_night_bathroom_composite_requires_sustained_state(self) -> None:
        now = FixedDateTime.fixed_now
        observations = [
            ("device_state", {"room": "bedroom", "present": False}, now - timedelta(minutes=19)),
            ("device_state", {"room": "bathroom", "present": True}, now - timedelta(minutes=17)),
            ("device_state", {"room": "bathroom", "device": "light", "state": "on"}, now - timedelta(minutes=11)),
            ("vital", {"heart_rate": 110}, now),
        ]
        event = None
        for index, (kind, payload, observed_at) in enumerate(observations):
            event = rules.classify_observation(
                {
                    "observation_id": f"obs-{index}",
                    "elder_id": "elder_001",
                    "kind": kind,
                    "payload": payload,
                    "observed_at": observed_at.isoformat(),
                }
            )

        self.assertIsNotNone(event)
        self.assertEqual(str(event.event_type), "night_bathroom_not_returned")
        self.assertEqual(str(event.risk_level), "P2")


if __name__ == "__main__":
    unittest.main()
