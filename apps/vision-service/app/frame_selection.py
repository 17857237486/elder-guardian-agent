from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence


def closest_frame(
    candidates: Iterable[Any],
    target: datetime,
    *,
    tolerance_sec: float = 1.4,
    excluded_frame_ids: set[str] | None = None,
) -> Any | None:
    excluded = excluded_frame_ids or set()
    available = [item for item in candidates if item.frame_id not in excluded]
    if not available:
        return None
    frame = min(available, key=lambda item: abs((item.captured_at - target).total_seconds()))
    return frame if abs((frame.captured_at - target).total_seconds()) <= tolerance_sec else None


def select_keyframes(
    candidates: Iterable[Any],
    triggered_at: datetime,
    offsets_ms: Sequence[int],
    *,
    tolerance_sec: float = 1.4,
) -> list[Any | None]:
    frames = list(candidates)
    selected_ids: set[str] = set()
    selected: list[Any | None] = []
    for offset_ms in offsets_ms:
        target = triggered_at + timedelta(milliseconds=offset_ms)
        frame = closest_frame(
            frames,
            target,
            tolerance_sec=tolerance_sec,
            excluded_frame_ids=selected_ids,
        )
        selected.append(frame)
        if frame is not None:
            selected_ids.add(frame.frame_id)
    return selected
