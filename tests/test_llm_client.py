from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app.llm_client import LLMOutputError, _extract_json_object, _normalize_output


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


if __name__ == "__main__":
    unittest.main()
