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

from Background_MQTT.generate_scenario_data import EVENT_LABELS, build_event_samples, classify_hint


EXPECTED_EVENTS = {
    "normal",
    "spo2_critical",
    "spo2_low",
    "heart_rate_abnormal",
    "heart_rate_baseline_anomaly",
    "spo2_baseline_anomaly",
    "bathroom_stay_anomaly_demo",
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

    def test_frontend_risk_events_are_ordered_by_severity(self) -> None:
        html = (ROOT / "Background_MQTT" / "frontend" / "index.html").read_text(encoding="utf-8")
        order = [
            'value="gas_leak"',
            'value="spo2_critical"',
            'value="spo2_low"',
            'value="heart_rate_abnormal"',
            'value="suspected_fall"',
            'value="long_static"',
            'value="heart_rate_baseline_anomaly"',
            'value="spo2_baseline_anomaly"',
            'value="bathroom_stay_anomaly_demo"',
            'value="co2_high"',
            'value="temperature_high"',
            'value="temperature_low"',
            'value="humidity_abnormal"',
            'value="normal"',
        ]
        positions = [html.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))

    def test_vision_capture_panel_is_chinese_and_not_question_marks(self) -> None:
        html = (ROOT / "Background_MQTT" / "frontend" / "index.html").read_text(encoding="utf-8")
        start = html.index("真实摄像头拍照验证")
        end = html.index("个人基线设置", start)
        section = html[start:end]
        self.assertIn("拍一张照片", section)
        self.assertIn("一键清除已拍摄图片", section)
        self.assertIn("用最近五张触发疑似跌倒", section)
        self.assertNotIn("????", section)

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

    def test_heart_rate_candidate_scenario_stays_below_hard_rule(self) -> None:
        sample = build_event_samples("dinner", "heart_rate_baseline_anomaly", 0, 10, 5, "elder_001")[-1]
        self.assertEqual(sample["vital"]["heart_rate"], 115)
        self.assertGreaterEqual(sample["vital"]["heart_rate"], 45)
        self.assertLessEqual(sample["vital"]["heart_rate"], 130)
        self.assertEqual(sample["risk_hint"]["level"], "P2")

    def test_spo2_candidate_scenario_stays_above_hard_rule(self) -> None:
        sample = build_event_samples("dinner", "spo2_baseline_anomaly", 0, 10, 5, "elder_001")[-1]
        self.assertEqual(sample["vital"]["spo2"], 94)
        self.assertGreaterEqual(sample["vital"]["spo2"], 92)
        self.assertEqual(sample["risk_hint"]["level"], "P2")

    def test_bathroom_stay_candidate_scenario_keeps_bathroom_presence(self) -> None:
        samples = build_event_samples("dinner", "bathroom_stay_anomaly_demo", 10, 40, 5, "elder_001")
        before = [item for item in samples if item["time_offset_sec"] < 10][-1]
        after = [item for item in samples if item["time_offset_sec"] >= 10][-1]
        self.assertTrue(after["bathroom_stay_demo"])
        self.assertEqual(after["environment"]["occupant_room"], "bathroom")
        self.assertEqual(after["environment"]["room"], "bathroom")
        self.assertNotEqual(before["environment"]["occupant_room"], "bathroom")
        self.assertEqual(after["risk_hint"]["level"], "P2")
        self.assertGreaterEqual(after["vital"]["spo2"], 92)
        self.assertLessEqual(after["vital"]["heart_rate"], 130)

    def test_mild_heart_rate_variation_is_record_only_in_scenario_hint(self) -> None:
        env = {"gas_ppm": 0, "co2_ppm": 800, "temperature": 24}
        for heart_rate in (50, 115):
            with self.subTest(heart_rate=heart_rate):
                hint = classify_hint("dinner", heart_rate, 96, env, "walking")
                self.assertEqual(hint["level"], "P4")

    def test_device_log_is_filtered_to_actions(self) -> None:
        backend = (ROOT / "Background_MQTT" / "backend.py").read_text(encoding="utf-8")
        html = (ROOT / "Background_MQTT" / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('DEVICE_ACTION_LOG_TYPES = {"device_command", "manual_command"}', backend)
        self.assertIn("visible_device_log()", backend)
        self.assertIn('new Set(["device_command", "manual_command"])', html)

    def test_records_limit_supports_auto_baseline_display(self) -> None:
        backend = (ROOT / "Background_MQTT" / "backend.py").read_text(encoding="utf-8")
        html = (ROOT / "Background_MQTT" / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('BACKGROUND_MAX_RECORDS", "3100"', backend)
        self.assertIn("async def list_records(limit: int = 3100)", backend)
        self.assertIn("const recordLimit = 3100", html)
        self.assertIn(".slice(0, recordLimit)", html)

    def test_bathroom_presence_monitor_is_exposed_on_8090(self) -> None:
        backend = (ROOT / "Background_MQTT" / "backend.py").read_text(encoding="utf-8")
        html = (ROOT / "Background_MQTT" / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("bathroom_stay_monitor_snapshot()", backend)
        self.assertIn("update_bathroom_stay_monitor(env_payload)", backend)
        self.assertIn('"bathroom_stay_monitor": bathroom_stay_monitor_snapshot()', backend)
        self.assertIn("卫生间停留时长推导", html)
        self.assertIn("demo_flow_rows", backend)
        self.assertIn("pir-flow-rows", html)
        self.assertIn("pir-final-stay", html)
        self.assertIn("setBathroomStayMonitor(message.bathroom_stay_monitor)", html)

    def test_bathroom_demo_sends_continuous_home_environment_snapshots(self) -> None:
        backend = (ROOT / "Background_MQTT" / "backend.py").read_text(encoding="utf-8")
        html = (ROOT / "Background_MQTT" / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("logical_interval_sec: int = Field(default=5", backend)
        self.assertIn("for index in range(steps + 1):", backend)
        self.assertIn("home_presence_snapshot(request.elder_id, \"bathroom\", observed_at, source=\"bathroom_stay_demo\")", backend)
        self.assertIn("\"published_snapshots\": published", backend)
        self.assertIn("每 5 秒输入一组整屋环境数据", html)
        self.assertIn("本次卫生间停留时长", html)
        self.assertIn("logical_interval_sec: 5", html)

    def test_bathroom_baseline_generator_does_not_take_over_demo_monitor(self) -> None:
        backend = (ROOT / "Background_MQTT" / "backend.py").read_text(encoding="utf-8")
        self.assertIn('if env_payload.get("source") == "bathroom_baseline_generator":', backend)
        self.assertIn("return bathroom_stay_monitor_snapshot()", backend)


if __name__ == "__main__":
    unittest.main()
