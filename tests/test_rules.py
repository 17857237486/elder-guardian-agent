from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app import rules
from app.event_cooldown import GasLeakCooldown, P0VitalCooldown, P3EnvironmentCooldown, VitalEventCooldown
from guardian_shared.enums import EventType, RiskLevel
from guardian_shared.v2 import NormalizedEventV2


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

    def test_mild_heart_rate_variation_does_not_create_fixed_p2_event(self) -> None:
        for heart_rate in (50, 115):
            event = rules.classify_observation(
                {
                    "observation_id": f"obs-heart-rate-{heart_rate}",
                    "elder_id": "elder_001",
                    "kind": "vital",
                    "payload": {"room": "living_room", "heart_rate": heart_rate, "spo2": 96},
                    "observed_at": datetime(2026, 6, 14, 15, 30, tzinfo=timezone.utc).isoformat(),
                }
            )
            self.assertIsNone(event)

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
        self.assertIn("湿度", event.summary)
        self.assertNotIn("outside the safe comfort range", event.summary)

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
            {"kind": "device_state", "payload": {"room": "bedroom", "device": "presence_sensor", "present": False, "state": "absent"}},
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

    def test_p3_environment_cooldown_suppresses_same_room_duplicate(self) -> None:
        now = 1000.0
        cooldown = P3EnvironmentCooldown(120, clock=lambda: now)
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type="humidity_abnormal",
            risk_level=RiskLevel.P3,
            source_kind="environment",
            room="living_room",
        )

        first = cooldown.check(event)
        second = cooldown.check(event)

        self.assertFalse(first.suppressed)
        self.assertTrue(second.suppressed)
        self.assertEqual(second.dedupe_key, "elder_001:humidity_abnormal:living_room")

    def test_p3_environment_cooldown_is_per_room_and_expires(self) -> None:
        clock = {"now": 1000.0}
        cooldown = P3EnvironmentCooldown(120, clock=lambda: clock["now"])
        living_room = NormalizedEventV2(
            elder_id="elder_001",
            event_type="humidity_abnormal",
            risk_level=RiskLevel.P3,
            source_kind="environment",
            room="living_room",
        )
        bedroom = living_room.model_copy(update={"room": "bedroom"})

        self.assertFalse(cooldown.check(living_room).suppressed)
        self.assertFalse(cooldown.check(bedroom).suppressed)
        self.assertTrue(cooldown.check(living_room).suppressed)

        clock["now"] = 1121.0
        self.assertFalse(cooldown.check(living_room).suppressed)

    def test_p3_environment_cooldown_does_not_suppress_p0_gas(self) -> None:
        cooldown = P3EnvironmentCooldown(120, clock=lambda: 1000.0)
        gas = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.GAS_LEAK,
            risk_level=RiskLevel.P0,
            source_kind="environment",
            room="kitchen",
        )

        self.assertFalse(cooldown.check(gas).suppressed)
        self.assertFalse(cooldown.check(gas).suppressed)

    def test_p1_vital_cooldown_suppresses_duplicate_heart_rate(self) -> None:
        now = 1000.0
        cooldown = VitalEventCooldown(120, clock=lambda: now)
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.HEART_RATE_ABNORMAL,
            risk_level=RiskLevel.P1,
            source_kind="vital",
        )

        first = cooldown.check(event)
        second = cooldown.check(event)

        self.assertFalse(first.suppressed)
        self.assertTrue(second.suppressed)
        self.assertEqual(second.dedupe_key, "elder_001:heart_rate_abnormal:P1")

    def test_p1_vital_cooldown_suppresses_duplicate_spo2_p1_but_not_p0(self) -> None:
        clock = {"now": 1000.0}
        cooldown = VitalEventCooldown(120, clock=lambda: clock["now"])
        p1 = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.SPO2_LOW,
            risk_level=RiskLevel.P1,
            source_kind="vital",
        )
        p0 = p1.model_copy(update={"risk_level": RiskLevel.P0})

        self.assertFalse(cooldown.check(p1).suppressed)
        self.assertTrue(cooldown.check(p1).suppressed)
        self.assertFalse(cooldown.check(p0).suppressed)
        self.assertFalse(cooldown.check(p0).suppressed)

        clock["now"] = 1121.0
        self.assertFalse(cooldown.check(p1).suppressed)

    def test_p0_gas_cooldown_suppresses_duplicate_same_room(self) -> None:
        clock = {"now": 1000.0}
        cooldown = GasLeakCooldown(120, clock=lambda: clock["now"])
        kitchen = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.GAS_LEAK,
            risk_level=RiskLevel.P0,
            source_kind="environment",
            room="kitchen",
        )
        living_room = kitchen.model_copy(update={"room": "living_room"})

        self.assertFalse(cooldown.check(kitchen).suppressed)
        self.assertTrue(cooldown.check(kitchen).suppressed)
        self.assertFalse(cooldown.check(living_room).suppressed)

        clock["now"] = 1121.0
        self.assertFalse(cooldown.check(kitchen).suppressed)

    def test_p0_vital_cooldown_suppresses_duplicate_critical_spo2(self) -> None:
        clock = {"now": 1000.0}
        cooldown = P0VitalCooldown(120, clock=lambda: clock["now"])
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.SPO2_LOW,
            risk_level=RiskLevel.P0,
            source_kind="vital",
        )

        self.assertFalse(cooldown.check(event).suppressed)
        self.assertTrue(cooldown.check(event).suppressed)

        clock["now"] = 1121.0
        self.assertFalse(cooldown.check(event).suppressed)

    def test_p0_cooldowns_do_not_require_source_kind_for_direct_events(self) -> None:
        clock = {"now": 1000.0}
        gas_cooldown = GasLeakCooldown(120, clock=lambda: clock["now"])
        spo2_cooldown = P0VitalCooldown(120, clock=lambda: clock["now"])
        gas = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.GAS_LEAK,
            risk_level=RiskLevel.P0,
            room="kitchen",
        )
        spo2 = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.SPO2_LOW,
            risk_level=RiskLevel.P0,
        )

        self.assertFalse(gas_cooldown.check(gas).suppressed)
        self.assertTrue(gas_cooldown.check(gas).suppressed)
        self.assertFalse(spo2_cooldown.check(spo2).suppressed)
        self.assertTrue(spo2_cooldown.check(spo2).suppressed)


if __name__ == "__main__":
    unittest.main()
