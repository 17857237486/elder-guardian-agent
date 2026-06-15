from __future__ import annotations

import importlib.util
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


@dataclass
class Frame:
    frame_id: str
    captured_at: datetime


class VisionFrameTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
