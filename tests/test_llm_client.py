from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app.llm_client import (
    LLMOutputError,
    _extract_json_object,
    _normalize_multimodal_output,
    _normalize_multimodal_response,
    _normalize_output,
    build_cloud_multimodal_content,
    build_local_multimodal_content,
)
from app.workflow import WorkflowRunner
from guardian_shared.enums import EventType, RiskLevel
from guardian_shared.v2 import NormalizedEventV2


VALID_OUTPUT = {
    "summary": "已完成复盘。",
    "relevant_facts": ["fact"],
    "risk_notes": ["risk"],
    "uncertainty": "low",
    "next_step_hint": "continue",
}


class LLMClientParserTests(unittest.TestCase):
    def test_extracts_json_from_wrapped_text(self) -> None:
        self.assertEqual(_extract_json_object("prefix {\"summary\":\"ok\"} suffix"), {"summary": "ok"})

    def test_extracts_complete_template_from_truncated_outer_object(self) -> None:
        content = '{"output_template":{"risk_level":"P1","family_summary":"ok"},"event":{"summary":"truncated'
        self.assertEqual(
            _extract_json_object(content),
            {"output_template": {"risk_level": "P1", "family_summary": "ok"}},
        )

    def test_extracts_complete_multimodal_prefix_before_truncated_echo(self) -> None:
        conclusion = {
            "event_semantics": "elderly fall suspected",
            "risk_level": "P1",
            "confidence": 0.8,
            "temporal_changes": ["standing to floor"],
            "supporting_evidence": ["five ordered frames"],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": ["check immediately"],
            "family_summary": "possible fall",
        }
        content = json.dumps(conclusion)[:-1] + ',"event":{"summary":"truncated'

        self.assertEqual(_extract_json_object(content), conclusion)

    def test_empty_content_is_rejected(self) -> None:
        with self.assertRaises(LLMOutputError):
            _extract_json_object("")

    def test_missing_required_fields_is_rejected(self) -> None:
        with self.assertRaises(LLMOutputError):
            _normalize_output("context_fetch_conversation", {"event": {"risk_level": "P3"}}, {"summary": "only"})

    def test_risk_downgrade_is_rejected(self) -> None:
        output = {
            **VALID_OUTPUT,
            "reviewed_risk_level": "P3",
            "recommended_followup": ["ask"],
        }
        with self.assertRaises(LLMOutputError):
            _normalize_output("risk_decision_conversation", {"event": {"risk_level": "P1"}}, output)

    def test_device_command_fields_are_rejected(self) -> None:
        output = {
            **VALID_OUTPUT,
            "reviewed_risk_level": "P1",
            "recommended_followup": ["ask"],
            "commands": [{"device": "fan", "action": "turn_on"}],
        }
        with self.assertRaises(LLMOutputError):
            _normalize_output("risk_decision_conversation", {"event": {"risk_level": "P1"}}, output)

    def test_multimodal_risk_downgrade_is_rejected(self) -> None:
        output = {
            "event_semantics": "standing after a fall-like transition",
            "risk_level": "P3",
            "confidence": 0.8,
            "temporal_changes": [],
            "supporting_evidence": [],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": [],
            "family_summary": "review required",
        }
        with self.assertRaises(LLMOutputError):
            _normalize_multimodal_output({"event": {"risk_level": "P1"}}, output)

    def test_multimodal_template_echo_is_unwrapped(self) -> None:
        nested = {
            "event_semantics": "fall detected",
            "risk_level": "P1",
            "confidence": 0.8,
            "temporal_changes": ["standing to lying"],
            "supporting_evidence": ["horizontal posture"],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": ["check elder"],
            "family_summary": "possible fall",
        }
        normalized = _normalize_multimodal_output(
            {"event": {"risk_level": "P1"}},
            {"event": {"event_type": "suspected_fall"}, "output_template": nested},
        )
        self.assertEqual(normalized["event_semantics"], "fall detected")
        self.assertEqual(normalized["confidence"], 0.8)

    def test_local_uses_one_contact_sheet_and_cloud_uses_individual_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contact_sheet = root / "contact_sheet.jpg"
            contact_sheet.write_bytes(b"sheet")
            frames = []
            for index in range(5):
                frame = root / f"frame_{index}.jpg"
                frame.write_bytes(f"frame-{index}".encode())
                frames.append(frame)

            local_content = build_local_multimodal_content({"risk_level": "P2"}, {}, contact_sheet)
            cloud_content = build_cloud_multimodal_content(
                {"event_type": "long_static", "risk_level": "P2"},
                {},
                {},
                list(zip([-2000, -1000, 0, 1000, 2000], frames, strict=True)),
            )

            self.assertEqual(sum(item["type"] == "image_url" for item in local_content), 1)
            self.assertEqual(sum(item["type"] == "image_url" for item in cloud_content), 5)
            self.assertIn('"confidence":0.8', local_content[0]["text"])
            self.assertIn("T-2、T-1、T、T+1、T+2", local_content[0]["text"])
            self.assertIn("suspected_fall只能输出P1或P0", local_content[0]["text"])
            labels = [item["text"] for item in cloud_content if item["type"] == "text"][1:]
            self.assertEqual(
                labels,
                [
                    "关键帧时间标签：T-2s（offset_ms=-2000）",
                    "关键帧时间标签：T-1s（offset_ms=-1000）",
                    "关键帧时间标签：T（offset_ms=0）",
                    "关键帧时间标签：T+1s（offset_ms=1000）",
                    "关键帧时间标签：T+2s（offset_ms=2000）",
                ],
            )

    def test_nonvisual_prompt_does_not_claim_visual_evidence(self) -> None:
        content = build_local_multimodal_content(
            {"event_type": "heart_rate_abnormal", "risk_level": "P1"}, {}, None
        )

        self.assertEqual(len(content), 1)
        self.assertIn("这是非视觉事件，没有图片", content[0]["text"])
        self.assertNotIn("比较联系表", content[0]["text"])

    def test_cloud_frame_labels_preserve_missing_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.jpg"
            trigger = root / "trigger.jpg"
            after = root / "after.jpg"
            for path in (before, trigger, after):
                path.write_bytes(b"frame")

            content = build_cloud_multimodal_content(
                {"event_type": "suspected_fall", "risk_level": "P1"},
                {},
                {},
                [(-2000, before), (0, trigger), (2000, after)],
            )
            labels = [item["text"] for item in content if item["type"] == "text"][1:]

            self.assertEqual(
                labels,
                [
                    "关键帧时间标签：T-2s（offset_ms=-2000）",
                    "关键帧时间标签：T（offset_ms=0）",
                    "关键帧时间标签：T+2s（offset_ms=2000）",
                ],
            )

    def test_event_minimum_risk_is_enforced(self) -> None:
        valid = {
            "event_semantics": "疑似跌倒",
            "risk_level": "P2",
            "confidence": 0.8,
            "temporal_changes": [],
            "supporting_evidence": [],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": [],
            "family_summary": "疑似跌倒",
        }
        cases = [
            ("suspected_fall", "P4", "P2"),
            ("long_static", "P4", "P3"),
            ("night_abnormal_activity", "P4", "P3"),
            ("co2_high", "P4", "P4"),
            ("gas_leak", "P4", "P1"),
        ]
        for event_type, rule_risk, model_risk in cases:
            with self.subTest(event_type=event_type):
                output = {**valid, "risk_level": model_risk}
                with self.assertRaises(LLMOutputError):
                    _normalize_multimodal_output(
                        {"event": {"event_type": event_type, "risk_level": rule_risk}}, output
                    )

        for accepted in ("P1", "P0"):
            output = {**valid, "risk_level": accepted}
            normalized = _normalize_multimodal_output(
                {"event": {"event_type": "suspected_fall", "risk_level": "P4"}}, output
            )
            self.assertEqual(normalized["risk_level"], accepted)

    def test_llm_output_error_carries_rejected_model_diagnostics(self) -> None:
        parsed = {"event_semantics": "possible fall", "risk_level": "P3"}
        error = LLMOutputError(
            "model attempted to downgrade risk from P1 to P3",
            raw_model_content=json.dumps(parsed),
            parsed_model_output=parsed,
        )

        self.assertEqual(error.raw_model_content, json.dumps(parsed))
        self.assertEqual(error.parsed_model_output, parsed)

    def test_workflow_fallback_records_rejected_model_diagnostics(self) -> None:
        parsed = {
            "event_semantics": "老人跌倒后坐地",
            "risk_level": "P3",
            "confidence": 0.81,
        }
        error = LLMOutputError(
            "model attempted to downgrade risk from P1 to P3",
            raw_model_content=json.dumps(parsed, ensure_ascii=False),
            parsed_model_output=parsed,
        )
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.SUSPECTED_FALL,
            risk_level=RiskLevel.P1,
            summary="发现疑似跌倒。",
            confidence=0.92,
        )

        fallback = WorkflowRunner._fallback_result(event, error)

        self.assertTrue(fallback["fallback"])
        self.assertEqual(fallback["risk_level"], "P1")
        self.assertEqual(fallback["rejected_model_output"], parsed)
        self.assertEqual(fallback["rejected_model_content"], json.dumps(parsed, ensure_ascii=False))

    def test_multimodal_response_records_downgrade_output(self) -> None:
        output = {
            "event_semantics": "老人跌倒后坐地",
            "risk_level": "P3",
            "confidence": 0.81,
            "temporal_changes": ["站立变为坐地"],
            "supporting_evidence": ["连续姿态下降"],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": [],
            "family_summary": "疑似跌倒",
        }
        raw = json.dumps(output, ensure_ascii=False)

        with self.assertRaises(LLMOutputError) as captured:
            _normalize_multimodal_response({"event": {"risk_level": "P1"}}, raw)

        self.assertEqual(captured.exception.raw_model_content, raw)
        self.assertEqual(captured.exception.parsed_model_output, output)

    def test_multimodal_response_records_invalid_json_content(self) -> None:
        raw = "not valid JSON"

        with self.assertRaises(LLMOutputError) as captured:
            _normalize_multimodal_response({"event": {"risk_level": "P1"}}, raw)

        self.assertEqual(captured.exception.raw_model_content, raw)
        self.assertIsNone(captured.exception.parsed_model_output)

    def test_multimodal_response_records_forbidden_action_output(self) -> None:
        output = {
            "event_semantics": "疑似跌倒",
            "risk_level": "P1",
            "confidence": 0.9,
            "temporal_changes": [],
            "supporting_evidence": [],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": [],
            "family_summary": "疑似跌倒",
            "commands": [{"device": "alarm", "action": "on"}],
        }
        raw = json.dumps(output, ensure_ascii=False)

        with self.assertRaises(LLMOutputError) as captured:
            _normalize_multimodal_response({"event": {"risk_level": "P1"}}, raw)

        self.assertEqual(captured.exception.raw_model_content, raw)
        self.assertEqual(captured.exception.parsed_model_output, output)


if __name__ == "__main__":
    unittest.main()
