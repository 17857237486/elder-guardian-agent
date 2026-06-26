from __future__ import annotations

import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app.llm_client import (
    CloudLLMClient,
    EVENT_MINIMUM_RISK,
    LLMOutputError,
    LOCAL_OUTPUT_CONTRACT,
    LOCAL_RISK_POLICY_PROMPT,
    LOCAL_VISUAL_INSTRUCTION,
    LocalMultimodalClient,
    RISK_POLICY_PROMPT,
    _cloud_sensor_context_summary,
    _compact_local_case,
    _extract_json_object,
    _normalize_multimodal_output,
    _normalize_multimodal_response,
    _normalize_local_multimodal_output,
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

    def test_repairs_top_level_object_missing_final_brace(self) -> None:
        content = (
            '{"event_semantics":"老人静卧可能休息",'
            '"risk_level":"P2",'
            '"confidence":0.78,'
            '"supporting_evidence":["未见跌倒","生命体征正常"],'
            '"family_summary":"建议询问老人是否需要帮助"'
        )

        self.assertEqual(
            _extract_json_object(content),
            {
                "event_semantics": "老人静卧可能休息",
                "risk_level": "P2",
                "confidence": 0.78,
                "supporting_evidence": ["未见跌倒", "生命体征正常"],
                "family_summary": "建议询问老人是否需要帮助",
            },
        )

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
            self.assertIn("minimum_risk_level=P2", local_content[0]["text"])
            self.assertIn("risk_level:P0|P1|P2|P3|P4", local_content[0]["text"])
            self.assertNotIn('"output_template"', local_content[0]["text"])
            self.assertNotIn('"required_fields"', local_content[0]["text"])
            self.assertIn("T-1、T、T+1", local_content[0]["text"])
            self.assertNotIn("T-2、T-1", local_content[0]["text"])
            self.assertIn("temporal_changes最多5项", cloud_content[0]["text"])
            self.assertIn("异常状态", cloud_content[0]["text"])
            self.assertIn("疼痛或不适", cloud_content[0]["text"])
            self.assertIn("生命体征正常只能说明", cloud_content[0]["text"])
            self.assertNotIn('"context"', cloud_content[0]["text"])
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

    def test_user_invalid_composite_risk_is_rejected(self) -> None:
        raw = json.dumps(
            {
                "output_template": {
                    "event_semantics": "疑似跌倒",
                    "risk_level": "P1, P2",
                    "confidence": 0.85,
                    "temporal_changes": "从站立到躺卧，持续静止",
                    "supporting_evidence": "连续多帧显示姿态变化",
                    "contradictions": "",
                    "missing_information": "",
                    "recommended_followup": "建议联系家人确认",
                    "family_summary": "疑似跌倒事件",
                }
            },
            ensure_ascii=False,
        )

        with self.assertRaises(LLMOutputError) as captured:
            _normalize_multimodal_response(
                {"event": {"event_type": "suspected_fall", "risk_level": "P1"}}, raw
            )

        self.assertIn("invalid risk level: P1, P2", str(captured.exception))

    def test_string_arrays_are_repaired_and_empty_strings_are_removed(self) -> None:
        output = {
            "event_semantics": "疑似跌倒",
            "risk_level": "P1",
            "confidence": 0.85,
            "temporal_changes": "从站立到躺卧",
            "supporting_evidence": "连续姿态下降",
            "contradictions": "",
            "missing_information": "",
            "recommended_followup": "立即确认状态",
            "family_summary": "老人疑似跌倒",
        }

        normalized = _normalize_multimodal_output(
            {"event": {"event_type": "suspected_fall", "risk_level": "P1"}}, output
        )

        self.assertEqual(normalized["temporal_changes"], ["从站立到躺卧"])
        self.assertEqual(normalized["contradictions"], [])
        self.assertIn("temporal_changes", normalized["schema_repaired_fields"])
        self.assertIn("contradictions", normalized["schema_repaired_fields"])

    def test_more_than_two_evidence_items_are_trimmed(self) -> None:
        output = {
            "event_semantics": "环境异常",
            "risk_level": "P3",
            "confidence": 0.8,
            "temporal_changes": [],
            "supporting_evidence": ["证据一", "证据二", "证据三"],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": [],
            "family_summary": "环境异常",
        }

        normalized = _normalize_multimodal_output(
            {"event": {"event_type": "co2_high", "risk_level": "P3"}}, output
        )

        self.assertEqual(len(normalized["supporting_evidence"]), 2)
        self.assertIn("supporting_evidence", normalized["schema_repaired_fields"])

    def test_cloud_temporal_changes_follow_available_frame_limit(self) -> None:
        output = {
            "event_semantics": "老人行走中失衡跌倒",
            "risk_level": "P1",
            "confidence": 0.98,
            "temporal_changes": [f"frame {index}" for index in range(5)],
            "supporting_evidence": ["posture changed", "remained on floor"],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": ["check elder"],
            "family_summary": "possible fall",
        }

        normalized = _normalize_multimodal_output(
            {"event": {"event_type": "suspected_fall", "risk_level": "P1"}},
            output,
            array_limits={"temporal_changes": 5},
        )
        self.assertEqual(len(normalized["temporal_changes"]), 5)

        trimmed = _normalize_multimodal_output(
            {"event": {"event_type": "suspected_fall", "risk_level": "P1"}},
            output,
            array_limits={"temporal_changes": 3},
        )
        self.assertEqual(trimmed["temporal_changes"], ["frame 0", "frame 1", "frame 2"])
        self.assertIn("temporal_changes", trimmed["schema_repaired_fields"])

    def test_local_prompt_stays_within_rk3588_budget(self) -> None:
        fixed_prompt = (
            "分析老人安全事件，先分析证据再生成结论。"
            + LOCAL_RISK_POLICY_PROMPT
            + "minimum_risk_level=P1。"
            + LOCAL_VISUAL_INSTRUCTION
            + LOCAL_OUTPUT_CONTRACT
            + "\n输入："
        )
        self.assertLessEqual(len(fixed_prompt.encode("utf-8")), 1200)

        with tempfile.TemporaryDirectory() as directory:
            contact_sheet = Path(directory) / "contact_sheet.jpg"
            contact_sheet.write_bytes(b"sheet")
            event = {
                "event_type": "suspected_fall",
                "risk_level": "P1",
                "rule_risk_level": "P1",
                "room": "living_room",
                "summary": "检测到疑似跌倒，需要立即确认老人状态",
                "confidence": 0.92,
                "rule_trace": {"payload": {"posture": "lying", "motion_state": "static"}},
            }
            context = {
                "sensors": {
                    "observations": [
                        {"kind": "vital", "payload": {"heart_rate": 96, "spo2": 95, "room": "living_room"}}
                    ]
                },
                "devices": {"devices": [{"room": "living_room", "device": "light", "state": "on"}]},
            }
            prompt_text = build_local_multimodal_content(event, context, contact_sheet)[0]["text"]

        self.assertLessEqual(len(prompt_text.encode("utf-8")), 2200)

    def test_local_request_uses_one_user_message(self) -> None:
        captured: dict[str, object] = {}
        valid_content = json.dumps(
            {
                "event_semantics": "疑似跌倒",
                "risk_level": "P1",
                "confidence": 0.91,
                "supporting_evidence": ["姿态高度快速下降"],
                "family_summary": "老人疑似跌倒",
            },
            ensure_ascii=False,
        )

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": valid_content}}]}

        class FakeClient:
            def __init__(self, **_: object) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, _: str, **kwargs: object) -> FakeResponse:
                captured.update(kwargs)
                return FakeResponse()

        fake_settings = SimpleNamespace(
            llm_mock=False,
            llm_model="internvl3.5-4b-rk3588",
            llm_base_url="http://model.invalid/v1",
            llm_api_key="local",
            llm_timeout_sec=240,
            llm_max_tokens=160,
        )
        with (
            patch("app.llm_client.settings", fake_settings),
            patch("app.llm_client.httpx.AsyncClient", FakeClient),
        ):
            result = asyncio.run(
                LocalMultimodalClient().analyze(
                    event={"event_type": "suspected_fall", "risk_level": "P1"},
                    context={},
                    contact_sheet=None,
                )
            )

        body = captured["json"]
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertEqual(body["max_tokens"], 160)
        self.assertEqual(result["risk_level"], "P1")
        self.assertEqual(result["temporal_changes"], [])
        self.assertEqual(result["contradictions"], [])
        self.assertEqual(result["missing_information"], [])
        self.assertEqual(result["recommended_followup"], [])

    def test_local_five_field_output_is_expanded_and_guarded(self) -> None:
        output = {
            "event_semantics": "疑似跌倒",
            "risk_level": "P1",
            "confidence": 0.9,
            "supporting_evidence": ["触发帧前后姿态下降"],
            "family_summary": "老人疑似跌倒",
        }
        normalized = _normalize_local_multimodal_output(
            {"event": {"event_type": "suspected_fall", "risk_level": "P1"}}, output
        )

        self.assertEqual(normalized["risk_level"], "P1")
        self.assertEqual(normalized["temporal_changes"], [])
        self.assertEqual(normalized["recommended_followup"], [])

        with self.assertRaises(LLMOutputError):
            _normalize_local_multimodal_output(
                {"event": {"event_type": "suspected_fall", "risk_level": "P1"}},
                {**output, "risk_level": "P2"},
            )
        with self.assertRaises(LLMOutputError):
            _normalize_local_multimodal_output(
                {"event": {"event_type": "suspected_fall", "risk_level": "P1"}},
                {**output, "commands": [{"device": "alarm", "action": "on"}]},
            )

    def test_heart_rate_local_output_accepts_p1_or_p0_and_rejects_downgrades(self) -> None:
        output = {
            "event_semantics": "心率明显偏高",
            "risk_level": "P1",
            "confidence": 0.9,
            "supporting_evidence": ["心率138次每分钟"],
            "family_summary": "老人心率异常需确认",
        }
        payload = {"event": {"event_type": "heart_rate_abnormal", "risk_level": "P1"}}

        for accepted in ("P1", "P0"):
            normalized = _normalize_local_multimodal_output(payload, {**output, "risk_level": accepted})
            self.assertEqual(normalized["risk_level"], accepted)

        for rejected in ("P2", "P3", "P4"):
            with self.subTest(risk_level=rejected), self.assertRaises(LLMOutputError):
                _normalize_local_multimodal_output(payload, {**output, "risk_level": rejected})

    def test_cloud_request_disables_thinking_and_preserves_invalid_response(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "not valid JSON"}}]}

        class FakeClient:
            def __init__(self, **_: object) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, _: str, **kwargs: object) -> FakeResponse:
                captured.update(kwargs)
                return FakeResponse()

        fake_settings = SimpleNamespace(
            cloud_llm_enabled=True,
            cloud_llm_base_url="https://example.invalid/v1",
            cloud_llm_api_key="secret",
            cloud_llm_model="qwen3-vl-plus",
            cloud_llm_timeout_sec=120,
            llm_max_tokens=512,
        )
        with (
            patch("app.llm_client.settings", fake_settings),
            patch("app.llm_client.httpx.AsyncClient", FakeClient),
        ):
            result = asyncio.run(
                CloudLLMClient().review(
                    event={"event_type": "suspected_fall", "risk_level": "P1"},
                    local_result={"risk_level": "P1"},
                    context={},
                    image_frames=[],
                )
            )

        body = captured["json"]
        self.assertIsInstance(body, dict)
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["rejected_model_content"], "not valid JSON")

    def test_cloud_daily_health_summary_completes_without_device_control(self) -> None:
        captured: dict[str, object] = {}
        cloud_output = {
            "overall_status": "今日整体平稳",
            "risk_level": "P2",
            "key_findings": ["心率血氧稳定", "卫生间停留未超出参考"],
            "family_message": "今天整体平稳，建议保持观察。",
            "recommended_followup": ["晚间确认饮水和休息"],
            "data_quality_note": "生命体征样本充足",
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": json.dumps(cloud_output, ensure_ascii=False)}}]}

        class FakeClient:
            def __init__(self, **_: object) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, _: str, **kwargs: object) -> FakeResponse:
                captured.update(kwargs)
                return FakeResponse()

        fake_settings = SimpleNamespace(
            cloud_llm_enabled=True,
            cloud_llm_base_url="https://example.invalid/v1",
            cloud_llm_api_key="secret",
            cloud_llm_model="qwen3-vl-plus",
            cloud_llm_timeout_sec=120,
            llm_max_tokens=512,
        )
        with (
            patch("app.llm_client.settings", fake_settings),
            patch("app.llm_client.httpx.AsyncClient", FakeClient),
        ):
            result = asyncio.run(
                CloudLLMClient().daily_health_summary(
                    {
                        "elder_id": "elder_001",
                        "summary_date": "2026-06-23",
                        "risk_level": "P2",
                        "local_stats": {"events": {"highest_risk": "P2"}, "vitals": {"heart_rate": {"count": 24}}},
                    }
                )
            )

        body = captured["json"]
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["risk_level"], "P2")
        self.assertEqual(result["family_message"], "今天整体平稳，建议保持观察。")
        self.assertEqual(result["model"], "qwen3-vl-plus")

    def test_cloud_five_frame_review_completes(self) -> None:
        captured: dict[str, object] = {}
        cloud_output = {
            "event_semantics": "老人行走中失衡跌倒",
            "risk_level": "P1",
            "confidence": 0.98,
            "temporal_changes": [f"T{index} posture" for index in range(5)],
            "supporting_evidence": ["height dropped", "remained down"],
            "contradictions": [],
            "missing_information": ["no audio"],
            "recommended_followup": ["check immediately"],
            "family_summary": "possible fall",
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(cloud_output)},
                        }
                    ]
                }

        class FakeClient:
            def __init__(self, **_: object) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, _: str, **kwargs: object) -> FakeResponse:
                captured.update(kwargs)
                return FakeResponse()

        fake_settings = SimpleNamespace(
            cloud_llm_enabled=True,
            cloud_llm_base_url="https://example.invalid/v1",
            cloud_llm_api_key="secret",
            cloud_llm_model="qwen3-vl-plus",
            cloud_llm_timeout_sec=120,
            llm_max_tokens=512,
        )
        with tempfile.TemporaryDirectory() as directory:
            frames: list[tuple[int, Path]] = []
            for index, offset in enumerate((-2000, -1000, 0, 1000, 2000)):
                frame = Path(directory) / f"frame_{index}.jpg"
                frame.write_bytes(b"frame")
                frames.append((offset, frame))
            with (
                patch("app.llm_client.settings", fake_settings),
                patch("app.llm_client.httpx.AsyncClient", FakeClient),
            ):
                result = asyncio.run(
                    CloudLLMClient().review(
                        event={"event_type": "suspected_fall", "risk_level": "P1"},
                        local_result={"risk_level": "P1"},
                        context={},
                        image_frames=frames,
                    )
                )

        body = captured["json"]
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["temporal_changes"], cloud_output["temporal_changes"])

    def test_nonvisual_prompt_does_not_claim_visual_evidence(self) -> None:
        content = build_local_multimodal_content(
            {"event_type": "heart_rate_abnormal", "risk_level": "P1"}, {}, None
        )

        self.assertEqual(len(content), 1)
        self.assertIn("非视觉：只分析事件", content[0]["text"])
        self.assertNotIn("比较T-2、T-1、T、T+1、T+2", content[0]["text"])

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

    def test_long_static_local_model_low_risk_is_deferred_to_cloud(self) -> None:
        output = {
            "event_semantics": "looks like resting, no fall evidence",
            "risk_level": "P4",
            "confidence": 0.82,
            "supporting_evidence": ["stable posture", "eyes closed"],
            "family_summary": "elder appears to be resting",
        }

        normalized = _normalize_local_multimodal_output(
            {"event": {"event_type": "long_static", "risk_level": "P2"}}, output
        )

        self.assertEqual(normalized["risk_level"], "P2")
        self.assertEqual(normalized["risk_guardrail_adjustment"], "long_static_local_low_risk_deferred_to_cloud")

    def test_cloud_long_static_can_downgrade_sleep_with_normal_context(self) -> None:
        output = {
            "event_semantics": "sleeping rest",
            "risk_level": "P4",
            "confidence": 0.86,
            "temporal_changes": ["stable posture"],
            "supporting_evidence": ["stable posture", "vital signs normal"],
            "contradictions": [],
            "missing_information": [],
            "recommended_followup": [],
            "family_summary": "elder appears resting",
        }
        payload = {
            "event": {"event_type": "long_static", "risk_level": "P2"},
            "context": {
                "recent_vital_samples": {
                    "samples": [
                        {"heart_rate": 76, "spo2": 96, "systolic_bp": 128, "diastolic_bp": 79}
                    ]
                },
                "baseline_context": {
                    "baselines": [
                        {"baseline_type": "heart_rate_daily", "metrics": {"p10": 64, "p90": 97}},
                        {"baseline_type": "spo2_daily", "metrics": {"p10": 93.5}},
                    ]
                },
            },
        }

        normalized = _normalize_multimodal_output(payload, output)

        self.assertEqual(normalized["risk_level"], "P4")

    def test_suspected_fall_local_model_still_cannot_downgrade(self) -> None:
        output = {
            "event_semantics": "possible fall",
            "risk_level": "P4",
            "confidence": 0.82,
            "supporting_evidence": ["image is unclear"],
            "family_summary": "needs confirmation",
        }

        with self.assertRaises(LLMOutputError):
            _normalize_local_multimodal_output(
                {"event": {"event_type": "suspected_fall", "risk_level": "P1"}}, output
            )

    def test_night_abnormal_activity_is_not_a_model_minimum_risk_policy(self) -> None:
        self.assertNotIn("night_abnormal_activity", EVENT_MINIMUM_RISK)
        self.assertNotIn("卧室持续无人5分钟", RISK_POLICY_PROMPT)
        self.assertNotIn("夜间异常活动", RISK_POLICY_PROMPT)

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
        self.assertEqual(fallback["fallback_type"], "safety_rejected")
        self.assertEqual(fallback["risk_level"], "P1")
        self.assertEqual(fallback["rejected_model_output"], parsed)
        self.assertEqual(fallback["rejected_model_content"], json.dumps(parsed, ensure_ascii=False))

    def test_workflow_fallback_classifies_503_and_timeout(self) -> None:
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.HEART_RATE_ABNORMAL,
            risk_level=RiskLevel.P1,
            summary="心率异常",
            confidence=0.95,
        )
        request = httpx.Request("POST", "http://172.30.0.1:8001/v1/chat/completions")
        response = httpx.Response(503, request=request)
        unavailable = WorkflowRunner._fallback_result(
            event,
            httpx.HTTPStatusError("service unavailable", request=request, response=response),
        )
        timed_out = WorkflowRunner._fallback_result(event, httpx.ReadTimeout("timed out", request=request))

        self.assertEqual(unavailable["fallback_type"], "service_unavailable")
        self.assertEqual(timed_out["fallback_type"], "timeout")
        self.assertEqual(unavailable["risk_level"], "P1")
        self.assertEqual(timed_out["risk_level"], "P1")

    def test_deterministic_p3_events_skip_local_and_cloud_models(self) -> None:
        for event_type in (
            EventType.CO2_HIGH,
            EventType.TEMPERATURE_HIGH,
            EventType.TEMPERATURE_LOW,
            "humidity_abnormal",
        ):
            with self.subTest(event_type=str(event_type)):
                runner = WorkflowRunner()
                runner.local_llm.analyze = AsyncMock(side_effect=AssertionError("local model must not run"))
                runner.cloud_llm.review = AsyncMock(side_effect=AssertionError("cloud model must not run"))
                runner._record_step = AsyncMock()
                runner.edge.update_event_analysis = AsyncMock(return_value={})
                event = NormalizedEventV2(
                    elder_id="elder_001",
                    event_type=event_type,
                    risk_level=RiskLevel.P3,
                    summary="确定性环境异常",
                    confidence=1.0,
                    source_kind="environment",
                )

                asyncio.run(
                    runner._run_analysis(
                        SimpleNamespace(workflow_id="wf_p3"),
                        event,
                        {"event_type": str(event_type), "risk_level": "P3"},
                        {"status": "executed", "actions": ["policy-gated"]},
                    )
                )

                runner.local_llm.analyze.assert_not_awaited()
                runner.cloud_llm.review.assert_not_awaited()
                update_payload = runner.edge.update_event_analysis.await_args.args[1]
                self.assertEqual(update_payload["final_risk_level"], "P3")
                self.assertEqual(update_payload["decision_source"], "rule")
                steps = {call.args[2]: call.args[4] for call in runner._record_step.await_args_list}
                self.assertEqual(steps["local_multiframe_analysis"]["status"], "skipped")
                self.assertEqual(steps["cloud_review"]["status"], "not_required")
                self.assertEqual(steps["final_advisory"]["final_risk_level"], "P3")

    def test_deterministic_vital_events_skip_local_and_cloud_models(self) -> None:
        cases = (
            (EventType.HEART_RATE_ABNORMAL, RiskLevel.P1),
            (EventType.SPO2_LOW, RiskLevel.P1),
            (EventType.SPO2_LOW, RiskLevel.P0),
        )
        for event_type, risk_level in cases:
            with self.subTest(event_type=str(event_type), risk_level=str(risk_level)):
                runner = WorkflowRunner()
                runner.local_llm.analyze = AsyncMock(side_effect=AssertionError("local model must not run"))
                runner.cloud_llm.review = AsyncMock(side_effect=AssertionError("cloud model must not run"))
                runner._record_step = AsyncMock()
                runner.edge.update_event_analysis = AsyncMock(return_value={})
                event = NormalizedEventV2(
                    elder_id="elder_001",
                    event_type=event_type,
                    risk_level=risk_level,
                    summary="确定性生命体征异常",
                    confidence=1.0,
                    source_kind="vital",
                )

                asyncio.run(
                    runner._run_analysis(
                        SimpleNamespace(workflow_id="wf_vital"),
                        event,
                        {"event_type": str(event_type), "risk_level": str(risk_level)},
                        {"status": "executed", "actions": ["rule"]},
                    )
                )

                runner.local_llm.analyze.assert_not_awaited()
                runner.cloud_llm.review.assert_not_awaited()
                update_payload = runner.edge.update_event_analysis.await_args.args[1]
                self.assertEqual(update_payload["final_risk_level"], str(risk_level))
                self.assertEqual(update_payload["decision_source"], "rule")
                steps = {call.args[2]: call.args[4] for call in runner._record_step.await_args_list}
                self.assertEqual(steps["local_multiframe_analysis"]["status"], "skipped")
                self.assertEqual(steps["local_multiframe_analysis"]["reason"], "deterministic_vital_rule")
                self.assertEqual(steps["local_multiframe_analysis"]["latency_ms"], 0)
                self.assertEqual(steps["cloud_review"]["status"], "not_required")
                self.assertEqual(steps["cloud_review"]["reason"], "deterministic_vital_rule")
                self.assertEqual(steps["final_advisory"]["final_risk_level"], str(risk_level))

    def test_p3_environment_events_create_chinese_hmi_prompt(self) -> None:
        for event_type in (
            EventType.CO2_HIGH,
            EventType.TEMPERATURE_HIGH,
            EventType.TEMPERATURE_LOW,
            "humidity_abnormal",
        ):
            with self.subTest(event_type=str(event_type)):
                runner = WorkflowRunner()
                runner.edge.request_home_action = AsyncMock(return_value={"ok": True, "executions": []})
                runner.edge.create_hmi_prompt = AsyncMock(return_value={"prompt_id": "prompt_p3", "status": "waiting"})
                runner.edge.raise_family_alert = AsyncMock(side_effect=AssertionError("P3 must not notify family by default"))
                event = NormalizedEventV2(
                    elder_id="elder_001",
                    event_type=event_type,
                    risk_level=RiskLevel.P3,
                    room="kitchen",
                    summary="P3 environment issue",
                    confidence=1.0,
                    source_kind="environment",
                )

                result = asyncio.run(runner._execute_policy(SimpleNamespace(workflow_id="wf_p3"), event, {}))

                self.assertEqual(result["status"], "p3_hmi_prompted")
                runner.edge.create_hmi_prompt.assert_awaited_once()
                prompt = runner.edge.create_hmi_prompt.await_args.args[0]
                self.assertEqual(prompt.options, ["我没事", "需要帮助", "联系家属"])
                self.assertIn("您", prompt.message)
                if str(event_type) == "humidity_abnormal":
                    runner.edge.request_home_action.assert_not_awaited()
                else:
                    runner.edge.request_home_action.assert_awaited_once()

    def test_p0_events_execute_emergency_policy_and_create_hmi_prompt(self) -> None:
        runner = WorkflowRunner()
        runner.edge.request_home_action = AsyncMock(return_value={"ok": True, "executions": ["action"]})
        runner.edge.raise_family_alert = AsyncMock(return_value={"alert_id": "alert_p0", "status": "sent"})
        runner.edge.create_hmi_prompt = AsyncMock(return_value={"prompt_id": "prompt_p0", "status": "waiting"})
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.GAS_LEAK,
            risk_level=RiskLevel.P0,
            room="kitchen",
            summary="gas leak",
            confidence=1.0,
            source_kind="environment",
            evidence=[{"payload": {"room": "kitchen", "gas_ppm": 180}}],
        )

        result = asyncio.run(runner._execute_policy(SimpleNamespace(workflow_id="wf_p0"), event, {}))

        self.assertIn("action", result)
        self.assertIn("alert", result)
        self.assertIn("prompt", result)
        runner.edge.request_home_action.assert_awaited_once()
        runner.edge.raise_family_alert.assert_awaited_once()
        runner.edge.create_hmi_prompt.assert_awaited_once()
        prompt = runner.edge.create_hmi_prompt.await_args.args[0]
        alert = runner.edge.raise_family_alert.await_args.args[0]
        self.assertEqual(prompt.options, ["我没事", "需要帮助", "联系家属"])
        self.assertIn("燃气异常", prompt.message)
        self.assertIn("180 ppm", alert.message)
        self.assertIn("关闭燃气阀", alert.message)

    def test_p1_family_alert_messages_include_vital_value(self) -> None:
        runner = WorkflowRunner()
        heart_event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.HEART_RATE_ABNORMAL,
            risk_level=RiskLevel.P1,
            summary="heart rate abnormal",
            evidence=[{"payload": {"heart_rate": 138, "spo2": 96}}],
        )
        spo2_event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.SPO2_LOW,
            risk_level=RiskLevel.P1,
            summary="spo2 low",
            evidence=[{"payload": {"heart_rate": 82, "spo2": 90}}],
        )

        self.assertIn("138 bpm", runner._family_alert_message(heart_event, RiskLevel.P1))
        self.assertIn("90%", runner._family_alert_message(spo2_event, RiskLevel.P1))

    def test_hmi_risk_messages_use_chinese_templates(self) -> None:
        cases = [
            (
                NormalizedEventV2(
                    elder_id="elder_001",
                    event_type=EventType.HEART_RATE_ABNORMAL,
                    risk_level=RiskLevel.P1,
                    summary="heart rate is too high",
                ),
                "心率明显异常",
            ),
            (
                NormalizedEventV2(
                    elder_id="elder_001",
                    event_type=EventType.SPO2_LOW,
                    risk_level=RiskLevel.P1,
                    summary="spo2 is low",
                ),
                "血氧偏低",
            ),
            (
                NormalizedEventV2(
                    elder_id="elder_001",
                    event_type=EventType.SUSPECTED_FALL,
                    risk_level=RiskLevel.P1,
                    summary="suspected fall",
                ),
                "疑似跌倒",
            ),
            (
                NormalizedEventV2(
                    elder_id="elder_001",
                    event_type=EventType.LONG_STATIC,
                    risk_level=RiskLevel.P2,
                    summary="long static",
                ),
                "较长时间没有活动",
            ),
            (
                NormalizedEventV2(
                    elder_id="elder_001",
                    event_type="vital_baseline_anomaly",
                    risk_level=RiskLevel.P2,
                    summary="spo2 window below personal p10",
                    evidence=[
                        {
                            "candidate": {
                                "features": {
                                    "metric": "spo2",
                                    "direction": "low",
                                }
                            }
                        }
                    ],
                ),
                "血氧比平时偏低",
            ),
            (
                NormalizedEventV2(
                    elder_id="elder_001",
                    event_type="bathroom_stay_anomaly",
                    risk_level=RiskLevel.P2,
                    summary="bathroom stay exceeds personal p90",
                ),
                "卫生间停留时间较长",
            ),
        ]

        for event, expected_text in cases:
            with self.subTest(event_type=str(event.event_type)):
                message = WorkflowRunner._risk_hmi_message(event)
                self.assertIn(expected_text, message)
                self.assertNotIn("heart rate", message.lower())
                self.assertNotIn("spo2 window", message.lower())
                self.assertNotIn("suspected fall", message.lower())
                self.assertNotIn("long static", message.lower())

    def test_local_context_uses_presence_timeline_twenty_environment_samples(self) -> None:
        base = "2026-06-18T22:{minute:02d}:00+08:00"
        observations: list[dict[str, object]] = []

        def env(index: int, room: str, minute: int, present: bool = True) -> dict[str, object]:
            return {
                "observation_id": f"env_{room}_{index}",
                "kind": "environment",
                "observed_at": base.format(minute=minute),
                "payload": {
                    "room": room,
                    "temperature": 24 + index / 10,
                    "humidity": 50,
                    "co2_ppm": 800 + index,
                    "presence": present,
                    "snapshot_id": f"snap_{minute}",
                },
            }

        for index in range(15):
            observations.append(env(index, "kitchen", index))
            observations.append(
                {
                    "observation_id": f"presence_kitchen_{index}",
                    "kind": "device_state",
                    "observed_at": base.format(minute=index),
                    "payload": {"room": "kitchen", "device": "pir_presence", "present": True, "state": "present"},
                }
            )
        for index in range(15):
            minute = 15 + index
            observations.append(env(index, "living_room", minute))
            observations.append(
                {
                    "observation_id": f"presence_living_{index}",
                    "kind": "device_state",
                    "observed_at": base.format(minute=minute),
                    "payload": {
                        "room": "living_room",
                        "device": "pir_presence",
                        "present": True,
                        "state": "present",
                    },
                }
            )
        observations.append(env(0, "bedroom", 29, present=False))
        observations.append(
            {
                "observation_id": "heart_trigger",
                "kind": "vital",
                "observed_at": "2026-06-18T22:30:00+08:00",
                "payload": {"heart_rate": 138, "spo2": 96, "room": "living_room"},
            }
        )

        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.HEART_RATE_ABNORMAL,
            risk_level=RiskLevel.P1,
            summary="heart rate abnormal",
            trigger_observation_ids=["heart_trigger"],
        )
        context = WorkflowRunner._build_local_context(
            event,
            {"trigger_observation_ids": ["heart_trigger"]},
            {"elder_id": "elder_001", "observations": observations},
            {"devices": []},
        )

        env_context = context["environment_context"]
        samples = env_context["samples"]
        self.assertEqual(env_context["target_samples"], 30)
        self.assertEqual(env_context["actual_samples"], 30)
        self.assertEqual(env_context["room_sequence"], ["kitchen", "living_room"])
        self.assertEqual([sample["room"] for sample in samples[:15]], ["kitchen"] * 15)
        self.assertEqual([sample["room"] for sample in samples[15:]], ["living_room"] * 15)
        self.assertEqual([sample["observed_at"] for sample in samples], sorted(sample["observed_at"] for sample in samples))
        self.assertEqual(context["elder_location"]["current_room"], "living_room")

        local_obs = context["sensors"]["observations"]
        local_ids = {item["observation_id"] for item in local_obs}
        self.assertIn("heart_trigger", local_ids)
        self.assertNotIn("env_bedroom_0", local_ids)
        self.assertTrue(
            all(
                item["kind"] != "environment" or item["payload"]["room"] in {"kitchen", "living_room"}
                for item in local_obs
            )
        )

    def test_compact_local_case_preserves_elder_environment_context(self) -> None:
        samples = [
            {
                "observation_id": f"env_{index}",
                "observed_at": f"2026-06-18T22:{index:02d}:00+08:00",
                "room": "living_room",
                "temperature": 24.0,
                "humidity": 50,
                "co2_ppm": 820,
                "presence": True,
            }
            for index in range(20)
        ]
        context = {
            "elder_location": {
                "current_room": "living_room",
                "source": "pir_presence",
                "observed_at": "2026-06-18T22:19:00+08:00",
            },
            "environment_context": {
                "target_samples": 20,
                "actual_samples": 20,
                "selection_policy": "presence_timeline_current_room_then_previous_rooms",
                "room_sequence": ["living_room"],
                "samples": samples,
            },
            "sensors": {
                "observations": [
                    {
                        "kind": "vital",
                        "payload": {"heart_rate": 138, "spo2": 96, "room": "living_room"},
                    }
                ]
            },
            "devices": {"devices": []},
        }

        compact = _compact_local_case(
            {"event_type": "heart_rate_abnormal", "risk_level": "P1", "room": "living_room"},
            context,
        )

        self.assertEqual(compact["elder_location"]["current_room"], "living_room")
        self.assertEqual(compact["environment_context"]["actual_samples"], 20)
        self.assertEqual(len(compact["environment_context"]["samples"]), 20)
        self.assertEqual(compact["sensor_evidence"][0]["payload"]["heart_rate"], 138)

    def test_cloud_sensor_context_summary_keeps_thirty_vital_and_environment_samples(self) -> None:
        context = {
            "elder_location": {"current_room": "living_room", "source": "pir_presence"},
            "environment_context": {
                "actual_samples": 30,
                "room_sequence": ["living_room"],
                "samples": [
                    {
                        "observation_id": f"env_{index}",
                        "observed_at": f"2026-06-18T22:{index:02d}:00+08:00",
                        "room": "living_room",
                        "temperature": 24 + index / 10,
                    }
                    for index in range(30)
                ],
            },
            "recent_vital_samples": {
                "actual_samples": 30,
                "samples": [
                    {
                        "observation_id": f"vital_{index}",
                        "observed_at": f"2026-06-18T22:{index:02d}:30+08:00",
                        "heart_rate": 70 + index,
                        "spo2": 96,
                    }
                    for index in range(30)
                ],
            },
            "baseline_context": {
                "heart_rate_daily": {
                    "quality": "stable",
                    "sample_count": 3000,
                    "metrics": {"p10": 64, "p50": 77, "p90": 95},
                },
                "spo2_daily": {
                    "quality": "stable",
                    "sample_count": 3000,
                    "metrics": {"p10": 93.5, "p50": 96, "p90": 98.5},
                },
            },
        }

        summary = _cloud_sensor_context_summary(context)

        self.assertEqual(len(summary["environment"]["samples"]), 30)
        self.assertEqual(len(summary["vital"]["samples"]), 30)
        self.assertEqual(summary["environment"]["samples"][-1]["observation_id"], "env_29")
        self.assertEqual(summary["vital"]["samples"][-1]["observation_id"], "vital_29")
        self.assertEqual(summary["vital"]["summary"]["heart_rate"]["status"], "above_personal_high_ref")
        self.assertEqual(summary["baseline"]["heart_rate_daily"]["metrics"]["p90"], 95)

    def test_vision_local_context_includes_recent_vitals_and_environment(self) -> None:
        observations = [
            {
                "observation_id": "env_1",
                "kind": "environment",
                "payload": {
                    "room": "living_room",
                    "temperature": 24.5,
                    "humidity": 52,
                    "co2_ppm": 820,
                    "gas_ppm": 0,
                    "presence": True,
                },
                "observed_at": "2026-06-24T10:00:00+08:00",
            },
            {
                "observation_id": "vital_1",
                "kind": "vital",
                "payload": {"heart_rate": 76, "spo2": 96, "room": "living_room"},
                "observed_at": "2026-06-24T10:00:05+08:00",
            },
        ]
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.LONG_STATIC,
            risk_level=RiskLevel.P2,
            summary="长时间静止",
            source_kind="VISION",
            room="living_room",
            frame_set_id="frames_test",
        )

        context = WorkflowRunner._build_local_context(
            event,
            {"trigger_observation_ids": ["vision_trigger"]},
            {"elder_id": "elder_001", "observations": observations},
            {"devices": []},
        )

        self.assertEqual(context["vision_context"]["local_frame_policy"], "middle_three")
        self.assertEqual(context["environment_context"]["actual_samples"], 1)
        self.assertEqual(context["recent_vital_samples"]["actual_samples"], 1)
        self.assertEqual(context["recent_vital_samples"]["samples"][0]["heart_rate"], 76)

    def test_candidate_context_and_prompt_are_lightweight(self) -> None:
        observations = [
            {
                "observation_id": f"env_{index}",
                "kind": "environment",
                "observed_at": f"2026-06-18T22:{index:02d}:00+08:00",
                "payload": {
                    "room": "bathroom",
                    "temperature": 25 + index,
                    "humidity": 58,
                    "presence": True,
                },
            }
            for index in range(12)
        ]
        observations.extend(
            {
                "observation_id": f"vital_{index}",
                "kind": "vital",
                "observed_at": f"2026-06-18T22:{index:02d}:30+08:00",
                "payload": {"heart_rate": 78 + index, "spo2": 96},
            }
            for index in range(12)
        )
        candidate = {
            "candidate_id": "cand_light",
            "candidate_type": "bathroom_stay_anomaly",
            "priority": "low",
            "reason": "卫生间停留超过个人90分位",
            "source_segment_ids": ["seg_bath"],
            "features": {
                "duration_seconds": 720,
                "baseline_p90_seconds": 480,
                "room": "bathroom",
                "segment": {"segment_id": "seg_bath", "segment_type": "bathroom_stay", "duration_seconds": 720, "room": "bathroom"},
                "local_result": {"error": "old timeout"},
            },
        }
        event = NormalizedEventV2(
            event_id="cand_light",
            elder_id="elder_001",
            event_type="bathroom_stay_anomaly",
            risk_level=RiskLevel.P4,
            source_kind="ai_review_candidate",
            summary="bathroom stay exceeds personal p90",
        )
        context = WorkflowRunner._build_candidate_context(
            event,
            candidate,
            {"elder_id": "elder_001", "observations": observations},
            segments=[
                {
                    "segment_id": "seg_bath",
                    "segment_type": "bathroom_stay",
                    "duration_seconds": 720,
                    "room": "bathroom",
                    "features": {"evidence_observation_ids": ["obs_bath_on"]},
                }
            ],
            baselines=[
                {
                    "baseline_type": "bathroom_routine",
                    "quality": "stable",
                    "sample_count": 14,
                    "metrics": {"bathroom_stay_p90_sec": 480},
                }
            ],
        )

        self.assertEqual(set(context), {"candidate_local_input"})
        self.assertEqual(context["candidate_local_input"]["dur"], 720)
        self.assertEqual(context["candidate_local_input"]["p90s"], 480)
        self.assertEqual(context["candidate_local_input"]["room"], "bathroom")
        self.assertIn("env", context["candidate_local_input"])
        self.assertIn("vital", context["candidate_local_input"])
        self.assertNotIn("night_night_wake_duration_p90_sec", context["candidate_local_input"])
        compact = _compact_local_case({"event_type": "bathroom_stay_anomaly", "risk_level": "P4"}, context)
        self.assertEqual(compact["candidate_review"]["dur"], 720)
        self.assertNotIn("local_result", str(compact))
        self.assertNotIn("dedupe_key", str(compact))
        self.assertNotIn("observations", str(compact))
        self.assertNotIn("devices", str(compact))
        self.assertNotIn("segment_id", str(compact))
        prompt_text = build_local_multimodal_content(
            {"event_type": "bathroom_stay_anomaly", "risk_level": "P4", "summary": "bathroom stay exceeds personal p90"},
            context,
            None,
        )[0]["text"]
        self.assertIn("candidate", prompt_text)
        self.assertIn("event_semantics", prompt_text)
        self.assertNotIn("supporting_evidence", prompt_text)
        self.assertNotIn("environment_context", prompt_text)

    def test_vital_candidate_context_is_summary_only(self) -> None:
        observations = [
            {
                "observation_id": "vital_latest",
                "kind": "vital",
                "observed_at": "2026-06-18T22:12:00+08:00",
                "payload": {"heart_rate": 104, "spo2": 95, "room": "living_room"},
            }
        ]
        candidate = {
            "candidate_id": "cand_vital",
            "candidate_type": "vital_baseline_anomaly",
            "reason": "heart rate window above personal p90",
            "source_segment_ids": ["seg_hr"],
            "features": {
                "dedupe_key": "very-long-key",
                "metric": "heart_rate",
                "direction": "high",
                "latest_value": 104,
                "max": 106,
                "p90": 102,
                "sample_count": 6,
                "window_seconds": 300,
                "baseline_p90": 96,
            },
        }
        event = NormalizedEventV2(
            event_id="cand_vital",
            elder_id="elder_001",
            event_type="vital_baseline_anomaly",
            risk_level=RiskLevel.P4,
            source_kind="ai_review_candidate",
            summary="heart rate window above personal p90",
        )
        context = WorkflowRunner._build_candidate_context(
            event,
            candidate,
            {"elder_id": "elder_001", "observations": observations},
            segments=[],
            baselines=[
                {
                    "baseline_type": "heart_rate_daily",
                    "metrics": {"p90": 96, "p50": 76, "daily_avg": 78},
                }
            ],
        )

        review = context["candidate_local_input"]
        self.assertEqual(review["p90"], 102)
        self.assertEqual(review["bp90"], 96)
        self.assertEqual(review["dir"], "high")
        self.assertEqual(review["n"], 6)
        self.assertEqual(review["win_s"], 300)
        self.assertNotIn("hr", review)
        self.assertNotIn("heart_rate_p90", review)
        compact_text = json.dumps(_compact_local_case({"event_type": "vital_baseline_anomaly", "risk_level": "P4"}, context))
        self.assertNotIn("dedupe_key", compact_text)
        self.assertNotIn("segment_id", compact_text)

    def test_candidate_local_output_allows_four_field_json(self) -> None:
        output = {
            "event_semantics": "bathroom stay mild",
            "risk_level": "P3",
            "confidence": 0.61,
            "family_summary": "record and observe",
        }

        normalized = _normalize_local_multimodal_output(
            {
                "event": {
                    "event_type": "bathroom_stay_anomaly",
                    "risk_level": "P4",
                    "summary": "bathroom stay exceeds p90",
                    "source_kind": "ai_review_candidate",
                }
            },
            output,
        )

        self.assertEqual(normalized["risk_level"], "P3")
        self.assertEqual(normalized["supporting_evidence"], ["bathroom stay exceeds p90"])
        self.assertIn("supporting_evidence", normalized["schema_repaired_fields"])

    def test_spo2_candidate_at_or_above_low_reference_cannot_upgrade_to_p1(self) -> None:
        output = {
            "event_semantics": "spo2 baseline lower than usual",
            "risk_level": "P1",
            "confidence": 0.8,
            "family_summary": "watch spo2",
        }

        normalized = _normalize_local_multimodal_output(
            {
                "event": {
                    "event_type": "vital_baseline_anomaly",
                    "risk_level": "P4",
                    "summary": "spo2 window below personal p10",
                    "source_kind": "ai_review_candidate",
                },
                "context": {
                    "candidate_local_input": {
                        "t": "vital_baseline_anomaly",
                        "metric": "spo2",
                        "dir": "low",
                        "latest": 94,
                        "min": 94,
                        "p10": 94,
                        "bp10": 93.5,
                        "n": 24,
                    }
                },
            },
            output,
        )

        self.assertEqual(normalized["risk_level"], "P2")
        self.assertEqual(normalized["risk_guardrail_adjustment"], "spo2_candidate_not_below_low_reference")

    def test_spo2_candidate_small_drop_below_reference_is_capped_at_p2(self) -> None:
        output = {
            "event_semantics": "spo2 mildly below personal baseline",
            "risk_level": "P1",
            "confidence": 0.8,
            "family_summary": "watch spo2",
        }

        normalized = _normalize_local_multimodal_output(
            {
                "event": {
                    "event_type": "vital_baseline_anomaly",
                    "risk_level": "P4",
                    "summary": "spo2 window below personal p10",
                    "source_kind": "ai_review_candidate",
                },
                "context": {
                    "candidate_local_input": {
                        "t": "vital_baseline_anomaly",
                        "metric": "spo2",
                        "dir": "low",
                        "latest": 94,
                        "min": 94,
                        "p10": 94,
                        "bp10": 95,
                        "n": 24,
                    }
                },
            },
            output,
        )

        self.assertEqual(normalized["risk_level"], "P2")
        self.assertEqual(normalized["risk_guardrail_adjustment"], "spo2_candidate_drop_not_severe_enough_for_p1")

    def test_bathroom_candidate_over_reference_is_promoted_to_p2(self) -> None:
        output = {
            "event_semantics": "bathroom stay seems okay",
            "risk_level": "P4",
            "confidence": 0.7,
            "family_summary": "record only",
        }

        normalized = _normalize_local_multimodal_output(
            {
                "event": {
                    "event_type": "bathroom_stay_anomaly",
                    "risk_level": "P4",
                    "summary": "bathroom stay exceeds p90",
                    "source_kind": "ai_review_candidate",
                },
                "context": {
                    "candidate_local_input": {
                        "t": "bathroom_stay_anomaly",
                        "dur": 600,
                        "p90s": 490,
                        "room": "bathroom",
                    }
                },
            },
            output,
        )

        self.assertEqual(normalized["risk_level"], "P2")
        self.assertEqual(normalized["risk_guardrail_adjustment"], "bathroom_stay_over_reference_promoted_to_p2")

    def test_candidate_local_output_repairs_confidence_words(self) -> None:
        output = {
            "event_semantics": "bathroom stay mild",
            "risk_level": "P3",
            "confidence": "high",
            "family_summary": "record and observe",
        }

        normalized = _normalize_local_multimodal_output(
            {
                "event": {
                    "event_type": "bathroom_stay_anomaly",
                    "risk_level": "P4",
                    "summary": "bathroom stay exceeds p90",
                    "source_kind": "ai_review_candidate",
                }
            },
            output,
        )

        self.assertEqual(normalized["confidence"], 0.8)
        self.assertIn("confidence", normalized["schema_repaired_fields"])

        with self.assertRaises(LLMOutputError):
            _normalize_local_multimodal_output(
                {"event": {"event_type": "long_static", "risk_level": "P2"}},
                {
                    **output,
                    "supporting_evidence": [],
                },
            )

    def test_final_advisory_prefers_completed_cloud_summary(self) -> None:
        class FakeLocalClient:
            async def analyze(self, **_: object) -> dict[str, object]:
                return {
                    "event_semantics": "local fall",
                    "risk_level": "P1",
                    "confidence": 0.9,
                    "family_summary": "local summary",
                }

        class FakeCloudClient:
            async def review(self, **_: object) -> dict[str, object]:
                return {
                    "status": "completed",
                    "event_semantics": "cloud confirmed fall",
                    "risk_level": "P1",
                    "confidence": 0.98,
                    "family_summary": "cloud family summary",
                }

        runner = WorkflowRunner()
        runner.local_llm = FakeLocalClient()
        runner.cloud_llm = FakeCloudClient()
        runner._collect_frames = AsyncMock(
            return_value=(
                {"image_refs": ["frame_0000.jpg"]},
                Path("contact_sheet.jpg"),
                [(offset, Path(f"frame_{offset}.jpg")) for offset in (-2000, -1000, 0, 1000, 2000)],
            )
        )
        runner._record_step = AsyncMock()
        runner.edge.get_recent_sensor_context = AsyncMock(return_value={"observations": []})
        runner.edge.get_home_device_snapshot = AsyncMock(return_value={"devices": []})
        runner.edge.update_event_analysis = AsyncMock(return_value={})
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.SUSPECTED_FALL,
            risk_level=RiskLevel.P1,
            summary="rule summary",
            confidence=0.8,
            source_kind="VISION",
            frame_set_id="frames_test",
        )

        asyncio.run(
            runner._run_analysis_inner(
                SimpleNamespace(workflow_id="wf_test"),
                event,
                {"event_type": "suspected_fall", "risk_level": "P1"},
                {"status": "baseline"},
            )
        )

        update_payload = runner.edge.update_event_analysis.await_args.args[1]
        self.assertEqual(update_payload["summary"], "cloud family summary")
        self.assertEqual(runner.edge.get_recent_sensor_context.await_count, 2)
        runner.edge.get_recent_sensor_context.assert_any_await("elder_001", limit=240)
        final_calls = [
            call
            for call in runner._record_step.await_args_list
            if len(call.args) >= 3 and call.args[2] == "final_advisory"
        ]
        self.assertEqual(final_calls[-1].args[4]["family_summary"], "cloud family summary")

    def test_long_static_hmi_waits_until_cloud_review_finishes(self) -> None:
        class FakeLocalClient:
            async def analyze(self, **_: object) -> dict[str, object]:
                return {
                    "event_semantics": "long static still concerning",
                    "risk_level": "P2",
                    "confidence": 0.78,
                    "family_summary": "local summary",
                }

        class FakeCloudClient:
            async def review(self, **_: object) -> dict[str, object]:
                return {
                    "status": "completed",
                    "event_semantics": "cloud confirms needs attention",
                    "risk_level": "P2",
                    "confidence": 0.84,
                    "family_summary": "cloud summary",
                }

        runner = WorkflowRunner()
        runner.local_llm = FakeLocalClient()
        runner.cloud_llm = FakeCloudClient()
        runner._collect_frames = AsyncMock(return_value=({}, None, []))
        runner._record_step = AsyncMock()
        runner._execute_policy = AsyncMock(return_value={"status": "waiting_hmi"})
        runner.edge.get_recent_sensor_context = AsyncMock(return_value={"observations": []})
        runner.edge.get_home_device_snapshot = AsyncMock(return_value={"devices": []})
        runner.edge.update_event_analysis = AsyncMock(return_value={})
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.LONG_STATIC,
            risk_level=RiskLevel.P2,
            summary="long static",
            confidence=0.78,
            source_kind="vision",
        )

        asyncio.run(
            runner._run_analysis_inner(
                SimpleNamespace(workflow_id="wf_test"),
                event,
                {"event_type": "long_static", "risk_level": "P2"},
                {"status": "deferred_until_local_review", "reason": "long_static_local_review_can_downgrade"},
            )
        )

        recorded = [(call.args[2], call.args[4]) for call in runner._record_step.await_args_list if len(call.args) >= 5]
        local_policy_index = next(index for index, item in enumerate(recorded) if item[0] == "local_policy_execution")
        cloud_index = next(index for index, item in enumerate(recorded) if item[0] == "cloud_review")
        post_policy_index = next(index for index, item in enumerate(recorded) if item[0] == "post_cloud_policy_execution")
        self.assertLess(local_policy_index, cloud_index)
        self.assertGreater(post_policy_index, cloud_index)
        self.assertEqual(recorded[local_policy_index][1]["status"], "deferred_until_cloud_review")
        runner._execute_policy.assert_awaited_once()
        reviewed_event = runner._execute_policy.await_args.args[1]
        self.assertEqual(str(reviewed_event.event_type), EventType.LONG_STATIC.value)
        self.assertEqual(str(reviewed_event.risk_level), RiskLevel.P2.value)

    def test_frame_collection_prefers_local_sheet_and_supports_old_manifest(self) -> None:
        event = NormalizedEventV2(
            elder_id="elder_001",
            event_type=EventType.SUSPECTED_FALL,
            risk_level=RiskLevel.P1,
            source_kind="VISION",
            frame_set_id="frames_test",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame_dir = root / "2026" / "06" / "15" / "elder_001" / "frames_test"
            frame_dir.mkdir(parents=True)
            full_sheet = frame_dir / "contact_sheet.jpg"
            local_sheet = frame_dir / "local_contact_sheet.jpg"
            trigger_frame = frame_dir / "frame_0000.jpg"
            for path in (full_sheet, local_sheet, trigger_frame):
                path.write_bytes(b"image")
            relative_dir = frame_dir.relative_to(root).as_posix()
            manifest_path = frame_dir / "manifest.json"
            manifest = {
                "frame_set_id": "frames_test",
                "contact_sheet_path": f"{relative_dir}/contact_sheet.jpg",
                "local_contact_sheet_path": f"{relative_dir}/local_contact_sheet.jpg",
                "frames": [
                    {
                        "offset_ms": 0,
                        "relative_path": f"{relative_dir}/frame_0000.jpg",
                        "missing": False,
                    }
                ],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            fake_settings = SimpleNamespace(snapshot_root=str(root), vision_frame_wait_sec=0.2)
            runner = WorkflowRunner()

            with patch("app.workflow.settings", fake_settings):
                _, selected_sheet, frames = asyncio.run(runner._collect_frames(event))
            self.assertEqual(selected_sheet, local_sheet)
            self.assertEqual(frames, [(0, trigger_frame)])

            manifest.pop("local_contact_sheet_path")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch("app.workflow.settings", fake_settings):
                _, selected_sheet, _ = asyncio.run(runner._collect_frames(event))
            self.assertEqual(selected_sheet, full_sheet)

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

    def test_candidate_low_risk_is_dismissed_without_event_promotion(self) -> None:
        runner = WorkflowRunner()
        runner._record_step = AsyncMock()
        runner.edge.create_workflow = AsyncMock(return_value={})
        runner.edge.update_ai_review_candidate = AsyncMock(return_value={})
        runner.edge.get_recent_sensor_context = AsyncMock(return_value={"elder_id": "elder_001", "observations": []})
        runner.edge.get_home_device_snapshot = AsyncMock(return_value={"devices": []})
        runner.edge.get_behavior_segments = AsyncMock(return_value=[])
        runner.edge.get_personal_baselines = AsyncMock(return_value=[])
        runner.edge.create_event = AsyncMock(side_effect=AssertionError("candidate P3/P4 must not create event"))
        runner.local_llm.analyze = AsyncMock(
            return_value={
                "event_semantics": "bathroom stay can be recorded",
                "risk_level": "P3",
                "confidence": 0.6,
                "family_summary": "continue observing",
            }
        )

        result = asyncio.run(
            runner.run_candidate(
                {
                    "candidate_id": "cand_low",
                    "elder_id": "elder_001",
                    "candidate_type": "bathroom_stay_anomaly",
                    "reason": "bathroom stay exceeds personal p90",
                }
            )
        )

        self.assertEqual(result["status"], "dismissed")
        runner.edge.create_event.assert_not_awaited()
        final_update = runner.edge.update_ai_review_candidate.await_args_list[-1].args[1]
        self.assertEqual(final_update["status"], "dismissed")

    def test_candidate_p2_or_higher_is_promoted_to_formal_event(self) -> None:
        runner = WorkflowRunner()
        runner._record_step = AsyncMock()
        runner._execute_policy = AsyncMock(return_value={"status": "waiting_hmi"})
        runner.edge.create_workflow = AsyncMock(return_value={})
        runner.edge.update_ai_review_candidate = AsyncMock(return_value={})
        runner.edge.get_recent_sensor_context = AsyncMock(return_value={"elder_id": "elder_001", "observations": []})
        runner.edge.get_home_device_snapshot = AsyncMock(return_value={"devices": []})
        runner.edge.get_behavior_segments = AsyncMock(return_value=[])
        runner.edge.get_personal_baselines = AsyncMock(return_value=[])
        runner.edge.create_event = AsyncMock(return_value={"event_id": "event_promoted"})
        runner.local_llm.analyze = AsyncMock(
            return_value={
                "event_semantics": "bathroom stay needs attention",
                "risk_level": "P2",
                "confidence": 0.82,
                "family_summary": "bathroom stay exceeded personal routine",
            }
        )

        result = asyncio.run(
            runner.run_candidate(
                {
                    "candidate_id": "cand_promote",
                    "elder_id": "elder_001",
                    "candidate_type": "bathroom_stay_anomaly",
                    "reason": "bathroom stay exceeds personal p90",
                    "features": {"duration_seconds": 720},
                }
            )
        )

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(result["promoted_event_id"], "event_promoted")
        promoted_event = runner.edge.create_event.await_args.args[0]
        self.assertEqual(str(promoted_event.event_type), "bathroom_stay_anomaly")
        self.assertEqual(str(promoted_event.risk_level), "P2")
        runner._execute_policy.assert_awaited_once()
        final_update = runner.edge.update_ai_review_candidate.await_args_list[-1].args[1]
        self.assertEqual(final_update["status"], "promoted")
        self.assertEqual(final_update["promoted_event_id"], "event_promoted")

    def test_candidate_local_llm_calls_are_serialized(self) -> None:
        class SerialProbeLocalClient:
            def __init__(self) -> None:
                self.active = 0
                self.max_active = 0
                self.calls = 0

            async def analyze(self, **_: object) -> dict[str, object]:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.02)
                self.active -= 1
                return {
                    "event_semantics": "candidate low risk",
                    "risk_level": "P3",
                    "confidence": 0.6,
                    "family_summary": "record only",
                }

        async def run_three() -> tuple[WorkflowRunner, SerialProbeLocalClient, list[dict[str, object]]]:
            runner = WorkflowRunner()
            probe = SerialProbeLocalClient()
            runner.local_llm = probe
            runner._record_step = AsyncMock()
            runner.edge.create_workflow = AsyncMock(return_value={})
            runner.edge.update_ai_review_candidate = AsyncMock(return_value={})
            runner.edge.get_recent_sensor_context = AsyncMock(return_value={"elder_id": "elder_001", "observations": []})
            runner.edge.get_behavior_segments = AsyncMock(return_value=[])
            runner.edge.get_personal_baselines = AsyncMock(return_value=[])
            runner.edge.create_event = AsyncMock(side_effect=AssertionError("low-risk candidates are not promoted"))
            results = await asyncio.gather(
                *[
                    runner.run_candidate(
                        {
                            "candidate_id": f"cand_serial_{index}",
                            "elder_id": "elder_001",
                            "candidate_type": "bathroom_stay_anomaly",
                            "reason": "bathroom stay exceeds p90",
                            "features": {"duration_seconds": 700 + index},
                        }
                    )
                    for index in range(3)
                ]
            )
            return runner, probe, results

        runner, probe, results = asyncio.run(run_three())

        self.assertEqual(probe.calls, 3)
        self.assertEqual(probe.max_active, 1)
        self.assertEqual([item["status"] for item in results], ["dismissed", "dismissed", "dismissed"])
        local_outputs = [
            call.args[4]
            for call in runner._record_step.await_args_list
            if len(call.args) >= 5 and call.args[2] == "local_multiframe_analysis"
        ]
        self.assertEqual(len(local_outputs), 3)
        self.assertTrue(all("queue_wait_ms" in item for item in local_outputs))
        self.assertTrue(any(float(item["queue_wait_ms"]) > 0 for item in local_outputs[1:]))

    def test_candidate_busy_503_is_retried_before_fallback(self) -> None:
        class BusyThenOkLocalClient:
            def __init__(self) -> None:
                self.calls = 0

            async def analyze(self, **_: object) -> dict[str, object]:
                self.calls += 1
                if self.calls < 3:
                    request = httpx.Request("POST", "http://local/v1/chat/completions")
                    response = httpx.Response(503, request=request, json={"detail": "VLM worker is busy"})
                    raise httpx.HTTPStatusError("busy", request=request, response=response)
                return {
                    "event_semantics": "candidate normal",
                    "risk_level": "P4",
                    "confidence": 0.7,
                    "family_summary": "record only",
                }

        async def run_once() -> tuple[WorkflowRunner, BusyThenOkLocalClient, dict[str, object]]:
            runner = WorkflowRunner()
            probe = BusyThenOkLocalClient()
            runner.local_llm = probe
            runner._record_step = AsyncMock()
            runner.edge.create_workflow = AsyncMock(return_value={})
            runner.edge.update_ai_review_candidate = AsyncMock(return_value={})
            runner.edge.get_recent_sensor_context = AsyncMock(return_value={"elder_id": "elder_001", "observations": []})
            runner.edge.get_behavior_segments = AsyncMock(return_value=[])
            runner.edge.get_personal_baselines = AsyncMock(return_value=[])
            runner.edge.create_event = AsyncMock(side_effect=AssertionError("P4 candidate must not be promoted"))
            with patch("app.workflow.asyncio.sleep", new=AsyncMock()):
                result = await runner.run_candidate(
                    {
                        "candidate_id": "cand_busy_retry",
                        "elder_id": "elder_001",
                        "candidate_type": "vital_baseline_anomaly",
                        "reason": "heart rate window above personal p90",
                        "features": {"metric": "heart_rate", "direction": "high", "latest_value": 115, "baseline_p90": 100},
                    }
                )
            return runner, probe, result

        runner, probe, result = asyncio.run(run_once())

        self.assertEqual(probe.calls, 3)
        self.assertEqual(result["status"], "dismissed")
        final_update = runner.edge.update_ai_review_candidate.await_args_list[-1].args[1]
        self.assertEqual(final_update["status"], "dismissed")
        self.assertFalse(final_update["features"]["local_result"].get("fallback", False))


if __name__ == "__main__":
    unittest.main()
