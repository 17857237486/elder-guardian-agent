from __future__ import annotations

import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app.llm_client import (
    CloudLLMClient,
    LLMOutputError,
    LOCAL_OUTPUT_CONTRACT,
    LOCAL_RISK_POLICY_PROMPT,
    LOCAL_VISUAL_INSTRUCTION,
    LocalMultimodalClient,
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

    def test_more_than_two_evidence_items_are_rejected(self) -> None:
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

        with self.assertRaises(LLMOutputError):
            _normalize_multimodal_output(
                {"event": {"event_type": "co2_high", "risk_level": "P3"}}, output
            )

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

        with self.assertRaisesRegex(LLMOutputError, "more than 3 items"):
            _normalize_multimodal_output(
                {"event": {"event_type": "suspected_fall", "risk_level": "P1"}},
                output,
                array_limits={"temporal_changes": 3},
            )

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
        self.assertEqual(body["max_tokens"], 128)
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
        final_calls = [
            call
            for call in runner._record_step.await_args_list
            if len(call.args) >= 3 and call.args[2] == "final_advisory"
        ]
        self.assertEqual(final_calls[-1].args[4]["family_summary"], "cloud family summary")

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


if __name__ == "__main__":
    unittest.main()
