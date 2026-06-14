from __future__ import annotations

import importlib.util
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "apps" / "vision-service" / "app" / "frame_selection.py"
SPEC = importlib.util.spec_from_file_location("vision_frame_selection", MODULE_PATH)
assert SPEC and SPEC.loader
frame_selection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frame_selection)
closest_frame = frame_selection.closest_frame
select_keyframes = frame_selection.select_keyframes


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
            Frame("before", triggered_at - timedelta(seconds=4)),
            Frame("trigger", triggered_at),
        ]
        selected = select_keyframes(frames, triggered_at, (-4000, -2000, 0, 2000, 4000))
        selected_ids = [frame.frame_id for frame in selected if frame is not None]

        self.assertEqual(selected_ids, ["before", "trigger"])
        self.assertEqual(len(selected_ids), len(set(selected_ids)))
        self.assertEqual(sum(frame is None for frame in selected), 3)


if __name__ == "__main__":
    unittest.main()
