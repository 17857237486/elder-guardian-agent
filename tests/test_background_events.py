from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))

paho = types.ModuleType("paho")
paho_mqtt = types.ModuleType("paho.mqtt")
paho_client = types.ModuleType("paho.mqtt.client")
paho_mqtt.client = paho_client
paho.mqtt = paho_mqtt
sys.modules.setdefault("paho", paho)
sys.modules.setdefault("paho.mqtt", paho_mqtt)
sys.modules.setdefault("paho.mqtt.client", paho_client)

from Background_MQTT.generate_scenario_data import EVENT_LABELS, build_event_samples


EXPECTED_EVENTS = {
    "normal",
    "spo2_critical",
    "spo2_low",
    "heart_rate_abnormal",
    "suspected_fall",
    "long_static",
    "co2_high",
    "gas_leak",
    "temperature_high",
    "temperature_low",
    "humidity_abnormal",
}


class BackgroundEventTests(unittest.TestCase):
    def test_all_risk_events_are_available(self) -> None:
        self.assertEqual(set(EVENT_LABELS), EXPECTED_EVENTS)
        self.assertNotIn("night_abnormal_activity", EVENT_LABELS)

    def test_frontend_does_not_offer_night_abnormal_event(self) -> None:
        html = (ROOT / "Background_MQTT" / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("night_abnormal_activity", html)

    def test_spo2_levels_are_distinct(self) -> None:
        critical = build_event_samples("dinner", "spo2_critical", 0, 10, 5, "elder_001")[-1]
        warning = build_event_samples("dinner", "spo2_low", 0, 10, 5, "elder_001")[-1]
        self.assertLess(critical["vital"]["spo2"], 88)
        self.assertGreaterEqual(warning["vital"]["spo2"], 88)
        self.assertLess(warning["vital"]["spo2"], 92)

    def test_humidity_event_crosses_rule_threshold(self) -> None:
        sample = build_event_samples("dinner", "humidity_abnormal", 0, 10, 5, "elder_001")[-1]
        self.assertGreater(sample["environment"]["humidity"], 75)

    def test_heart_rate_scenario_crosses_p1_threshold_with_normal_spo2(self) -> None:
        sample = build_event_samples("dinner", "heart_rate_abnormal", 0, 10, 5, "elder_001")[-1]
        self.assertEqual(sample["vital"]["heart_rate"], 138)
        self.assertGreaterEqual(sample["vital"]["spo2"], 92)
        self.assertEqual(sample["risk_hint"]["level"], "P1")


if __name__ == "__main__":
    unittest.main()
