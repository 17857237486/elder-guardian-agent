from __future__ import annotations

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
    _normalize_output,
    build_cloud_multimodal_content,
    build_local_multimodal_content,
)


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
            cloud_content = build_cloud_multimodal_content({"risk_level": "P2"}, {}, {}, frames)

            self.assertEqual(sum(item["type"] == "image_url" for item in local_content), 1)
            self.assertEqual(sum(item["type"] == "image_url" for item in cloud_content), 5)
            self.assertIn('"confidence":0.8', local_content[0]["text"])
            self.assertIn("confidence must be a number", local_content[0]["text"])


if __name__ == "__main__":
    unittest.main()
