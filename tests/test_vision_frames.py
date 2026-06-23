from __future__ import annotations

import importlib.util
import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "apps" / "vision-service" / "app" / "frame_selection.py"
SPEC = importlib.util.spec_from_file_location("vision_frame_selection", MODULE_PATH)
assert SPEC and SPEC.loader
frame_selection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frame_selection)
closest_frame = frame_selection.closest_frame
select_keyframes = frame_selection.select_keyframes

CONTACT_SHEET_PATH = ROOT / "apps" / "vision-service" / "app" / "contact_sheet.py"
CONTACT_SHEET_SPEC = importlib.util.spec_from_file_location("vision_contact_sheet", CONTACT_SHEET_PATH)
assert CONTACT_SHEET_SPEC and CONTACT_SHEET_SPEC.loader
contact_sheet = importlib.util.module_from_spec(CONTACT_SHEET_SPEC)
CONTACT_SHEET_SPEC.loader.exec_module(contact_sheet)
make_contact_sheet = contact_sheet.make_contact_sheet
VISION_SERVICE_ROOT = str(ROOT / "apps" / "vision-service")
sys.path.insert(0, VISION_SERVICE_ROOT)
import fastapi.dependencies.utils as fastapi_dependency_utils  # noqa: E402

fastapi_dependency_utils.ensure_multipart_is_installed = lambda: None
from app import main as vision_main  # noqa: E402

sys.path.remove(VISION_SERVICE_ROOT)
sys.modules.pop("app", None)


@dataclass
class Frame:
    frame_id: str
    captured_at: datetime


class VisionFrameTests(unittest.TestCase):
    @staticmethod
    def _jpeg(color: tuple[int, int, int]) -> bytes:
        from io import BytesIO

        output = BytesIO()
        Image.new("RGB", (320, 240), color).save(output, format="JPEG")
        return output.getvalue()

    def test_closest_frame_respects_exclusion(self) -> None:
        captured_at = datetime.now(timezone.utc)
        frame = Frame("one", captured_at)
        self.assertIs(closest_frame([frame], captured_at), frame)
        self.assertIsNone(closest_frame([frame], captured_at, excluded_frame_ids={"one"}))

    def test_keyframe_selection_is_ordered_unique_and_marks_missing(self) -> None:
        triggered_at = datetime.now(timezone.utc)
        frames = [
            Frame("before", triggered_at - timedelta(seconds=2)),
            Frame("trigger", triggered_at),
        ]
        selected = select_keyframes(frames, triggered_at, (-2000, -1000, 0, 1000, 2000))
        selected_ids = [frame.frame_id for frame in selected if frame is not None]

        self.assertEqual(selected_ids, ["before", "trigger"])
        self.assertEqual(len(selected_ids), len(set(selected_ids)))
        self.assertEqual(sum(frame is None for frame in selected), 3)

    def test_one_second_frames_select_third_image_as_trigger(self) -> None:
        triggered_at = datetime.now(timezone.utc)
        frames = [
            Frame(f"image-{index + 1}", triggered_at + timedelta(seconds=offset))
            for index, offset in enumerate((-2, -1, 0, 1, 2))
        ]

        selected = select_keyframes(frames, triggered_at, (-2000, -1000, 0, 1000, 2000))

        self.assertEqual([frame.frame_id for frame in selected], ["image-1", "image-2", "image-3", "image-4", "image-5"])
        self.assertEqual(selected[2].captured_at, triggered_at)

    def test_local_contact_sheet_uses_three_frames_and_trigger_border(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            colors = {-1000: (10, 80, 140), 0: (20, 160, 80), 1000: (140, 80, 10)}
            for offset, color in colors.items():
                filename = f"frame_{offset}.jpg"
                Image.new("RGB", (320, 240), color).save(root / filename, format="JPEG")
                records.append({"offset_ms": offset, "filename": filename, "missing": False})
            output = root / "local_contact_sheet.jpg"

            make_contact_sheet(
                records,
                output,
                offsets_ms=(-1000, 0, 1000),
                cell_width=224,
                cell_height=168,
            )

            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (672, 196))
                trigger_border = rendered.convert("RGB").getpixel((225, 30))
            self.assertGreater(trigger_border[0], trigger_border[1] * 1.4)

    def test_full_contact_sheet_keeps_five_frame_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            offsets = (-2000, -1000, 0, 1000, 2000)
            for index, offset in enumerate(offsets):
                filename = f"frame_{index}.jpg"
                Image.new("RGB", (320, 240), (20 * index, 80, 140)).save(root / filename, format="JPEG")
                records.append({"offset_ms": offset, "filename": filename, "missing": False})
            output = root / "contact_sheet.jpg"

            make_contact_sheet(
                records,
                output,
                offsets_ms=offsets,
                cell_width=256,
                cell_height=192,
            )

            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (1280, 220))

    def test_local_contact_sheet_marks_missing_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trigger = root / "trigger.jpg"
            Image.new("RGB", (320, 240), (20, 160, 80)).save(trigger, format="JPEG")
            output = root / "local_contact_sheet.jpg"

            make_contact_sheet(
                [{"offset_ms": 0, "filename": trigger.name, "missing": False}],
                output,
                offsets_ms=(-1000, 0, 1000),
                cell_width=224,
                cell_height=168,
            )

            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (672, 196))
                missing_cell = rendered.convert("RGB").getpixel((112, 112))
            self.assertLess(max(missing_cell) - min(missing_cell), 25)

    def test_camera_capture_pool_keeps_recent_five_and_can_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_root = vision_main.SNAPSHOT_ROOT
            vision_main.SNAPSHOT_ROOT = root

            async def run() -> None:
                colors = [(index * 20, 80, 140) for index in range(6)]

                async def fake_snapshot(*_: object) -> bytes:
                    return self._jpeg(colors.pop(0))

                request = vision_main.VisionCaptureRequest(
                    elder_id="elder_test",
                    camera_id="living_room",
                    room="living_room",
                    trigger_source="test",
                    reason="unit",
                )
                with patch.object(vision_main, "fetch_camera_snapshot", fake_snapshot):
                    for _ in range(6):
                        await vision_main.create_camera_capture(request)
                recent = vision_main.recent_pending_captures("elder_test", "living_room")
                self.assertEqual(len(recent), 5)

                result = await vision_main.clear_recent_captures(
                    vision_main.VisionCaptureClearRequest(elder_id="elder_test", camera_id="living_room")
                )
                self.assertTrue(result["ok"])
                self.assertEqual(vision_main.recent_pending_captures("elder_test", "living_room"), [])

            try:
                asyncio.run(run())
            finally:
                vision_main.SNAPSHOT_ROOT = original_root

    def test_trigger_uses_recent_five_and_local_sheet_uses_middle_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_root = vision_main.SNAPSHOT_ROOT
            vision_main.SNAPSHOT_ROOT = root

            async def run() -> None:
                colors = [(30 + index * 20, 80, 140) for index in range(5)]

                async def fake_snapshot(*_: object) -> bytes:
                    return self._jpeg(colors.pop(0))

                request = vision_main.VisionCaptureRequest(
                    elder_id="elder_test",
                    camera_id="living_room",
                    room="living_room",
                    trigger_source="test",
                    reason="unit",
                )
                with patch.object(vision_main, "fetch_camera_snapshot", fake_snapshot):
                    for _ in range(5):
                        await vision_main.create_camera_capture(request)

                trigger = vision_main.VisionTrigger(
                    elder_id="elder_test",
                    camera_id="living_room",
                    room="living_room",
                    event_type="suspected_fall",
                    triggered_at=datetime.now(timezone.utc),
                )
                await vision_main.finalize_frame_set(trigger, "frames_unit")
                matches = list(root.glob("*/*/*/*/frames_unit/manifest.json"))
                self.assertEqual(len(matches), 1)
                manifest = json.loads(matches[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "complete")
                self.assertEqual(manifest["capture_mode"], "recent_five_captures")
                self.assertEqual(manifest["local_frame_policy"], "middle_three")
                self.assertEqual([frame["filename"] for frame in manifest["frames"]], [f"frame_000{index}.jpg" for index in range(1, 6)])
                self.assertEqual(vision_main.recent_pending_captures("elder_test", "living_room"), [])
                with Image.open(root / manifest["local_contact_sheet_path"]) as local_sheet:
                    self.assertEqual(local_sheet.size, (672, 196))
                self.assertTrue((root / manifest["contact_sheet_path"]).is_file())

            try:
                asyncio.run(run())
            finally:
                vision_main.SNAPSHOT_ROOT = original_root


if __name__ == "__main__":
    unittest.main()
