from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from guardian_shared.v2 import AiReviewCandidateV2, BehaviorSegmentV2, PersonalBaselineV2

from app import repository
from app.config import settings
from app.database import SessionLocal


logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
NIGHT_START = time(22, 0)
NIGHT_END = time(6, 0)
WINDOW_SECONDS = 300
LOOKBACK_DAYS = 14
MIN_VITAL_CANDIDATE_SAMPLES = 24
CANDIDATE_RECENT_WINDOW_SECONDS = 15 * 60
DEFAULT_NIGHT_WAKE_P90 = 600
DEFAULT_WAKE_COUNT_P90 = 2
DEFAULT_BATHROOM_STAY_P90 = 480
DEFAULT_HEART_RATE_P90 = 100
DEFAULT_HEART_RATE_P10 = 60
DEFAULT_SPO2_P10 = 94
HEART_RATE_P1_LOW = 45
HEART_RATE_P1_HIGH = 130
SPO2_P1_LOW = 92


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_night(value: datetime) -> bool:
    local = value.astimezone(SHANGHAI)
    return local.time() >= NIGHT_START or local.time() < NIGHT_END


def _night_key(value: datetime) -> str:
    local = value.astimezone(SHANGHAI)
    date = local.date() if local.time() >= NIGHT_START else local.date() - timedelta(days=1)
    return date.isoformat()


def _segment_reference_time(segment: dict[str, Any]) -> datetime:
    return _parse_time(segment.get("end_at") or segment.get("start_at"))


def _is_recent_segment(segment: dict[str, Any], now: datetime, *, max_age_seconds: int = CANDIDATE_RECENT_WINDOW_SECONDS) -> bool:
    reference = _segment_reference_time(segment)
    if reference > now + timedelta(seconds=60):
        return False
    return now - reference <= timedelta(seconds=max_age_seconds)


def _is_current_open_night_wake(segment: dict[str, Any], now: datetime) -> bool:
    if segment.get("segment_type") != "night_wake" or segment.get("status") != "open":
        return False
    if not _is_night(now):
        return False
    return _night_key(_parse_time(segment.get("start_at"))) == _night_key(now)


def _is_present(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "present"}:
            return True
        if normalized in {"0", "false", "no", "off", "absent"}:
            return False
    return None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def _segment_id(prefix: str, elder_id: str, key: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in key)[:80]
    return f"seg_{prefix}_{elder_id}_{safe}"


class BehaviorAnalyticsWorker:
    def __init__(self, *, presence_interval_sec: int = 10, vital_interval_sec: int = 30, baseline_interval_sec: int = 600) -> None:
        self.presence_interval_sec = presence_interval_sec
        self.vital_interval_sec = vital_interval_sec
        self.baseline_interval_sec = baseline_interval_sec
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = asyncio.Event()

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop("presence", self.presence_interval_sec, self.run_presence_once)),
            asyncio.create_task(self._loop("vital", self.vital_interval_sec, self.run_vital_once)),
            asyncio.create_task(self._loop("baseline", self.baseline_interval_sec, self.run_baseline_once)),
        ]

    async def close(self) -> None:
        self._closed.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, name: str, interval: int, fn: Any) -> None:
        while not self._closed.is_set():
            try:
                await asyncio.to_thread(fn)
            except Exception:
                logger.exception("behavior analytics worker failed name=%s", name)
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=interval)
            except TimeoutError:
                continue

    def run_presence_once(self) -> None:
        with SessionLocal() as db:
            observations = repository.list_observations(db, settings.elder_id, limit=5000)
            segments = build_presence_segments(observations, now=datetime.now(timezone.utc))
            for segment in segments:
                repository.upsert_behavior_segment(db, segment)

    def run_vital_once(self) -> None:
        with SessionLocal() as db:
            observations = repository.list_observations(db, settings.elder_id, limit=5000)
            segments = build_vital_segments(observations)
            for segment in segments:
                repository.upsert_behavior_segment(db, segment)

    def run_baseline_once(self) -> None:
        with SessionLocal() as db:
            segments = repository.list_behavior_segments(db, settings.elder_id, limit=5000)
            if settings.auto_personal_baseline_enabled:
                for baseline in build_baselines(settings.elder_id, segments, now=datetime.now(timezone.utc)):
                    repository.create_personal_baseline(db, baseline)
            baselines = repository.list_personal_baselines(db, settings.elder_id)
            if not settings.auto_candidate_enabled:
                return
            existing = repository.list_ai_review_candidates(db, settings.elder_id, limit=200)
            for candidate in build_candidates(settings.elder_id, segments, baselines, existing, now=datetime.now(timezone.utc)):
                record = repository.create_ai_review_candidate(db, candidate)
                asyncio.run(self._forward_candidate(record))

    async def _forward_candidate(self, candidate: dict[str, Any]) -> None:
        if not settings.orchestrator_url:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{settings.orchestrator_url.rstrip('/')}/api/v2/orchestrator/candidates",
                    json=candidate,
                )
                response.raise_for_status()
        except Exception:
            logger.exception("failed to forward ai review candidate candidate_id=%s", candidate.get("candidate_id"))


def _presence_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in observations:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if str(item.get("kind") or "") != "device_state":
            continue
        room = payload.get("room")
        if not room:
            continue
        present = _is_present(payload.get("present", payload.get("presence", payload.get("state"))))
        if present is None and str(payload.get("state") or "").lower() in {"present", "absent"}:
            present = str(payload.get("state")).lower() == "present"
        if present is None:
            continue
        result.append({**item, "room": str(room), "present": present, "at": _parse_time(item.get("observed_at"))})
    return sorted(result, key=lambda item: item["at"])


def build_presence_segments(observations: list[dict[str, Any]], *, now: datetime) -> list[BehaviorSegmentV2]:
    events = _presence_observations(observations)
    by_elder: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in events:
        by_elder[str(item.get("elder_id") or settings.elder_id)].append(item)
    segments: list[BehaviorSegmentV2] = []
    for elder_id, items in by_elder.items():
        segments.extend(_build_room_stays(elder_id, items, now))
        segments.extend(_build_night_wakes(elder_id, items, now))
    return segments


def _build_room_stays(elder_id: str, items: list[dict[str, Any]], now: datetime) -> list[BehaviorSegmentV2]:
    segments: list[BehaviorSegmentV2] = []
    open_by_room: dict[str, dict[str, Any]] = {}
    for item in items:
        room = item["room"]
        if item["present"]:
            existing = open_by_room.get(room)
            if existing is None:
                open_by_room[room] = item
            continue
        start = open_by_room.pop(room, None)
        if start:
            segments.append(_make_room_segment(elder_id, "room_stay", room, start, item, "closed"))
            if room == "bedroom" and _is_night(start["at"]):
                segments.append(_make_room_segment(elder_id, "night_sleep", room, start, item, "closed"))
    for room, start in open_by_room.items():
        end = {**start, "at": now, "observation_id": None}
        status = "open"
        segments.append(_make_room_segment(elder_id, "room_stay", room, start, end, status))
        if room == "bedroom" and _is_night(start["at"]):
            segments.append(_make_room_segment(elder_id, "night_sleep", room, start, end, status))
    return segments


def _make_room_segment(elder_id: str, segment_type: str, room: str, start: dict[str, Any], end: dict[str, Any], status: str) -> BehaviorSegmentV2:
    duration = max(0, int((end["at"] - start["at"]).total_seconds()))
    return BehaviorSegmentV2(
        segment_id=_segment_id(segment_type, elder_id, f"{room}_{start.get('observation_id') or start['at'].isoformat()}"),
        elder_id=elder_id,
        segment_type=segment_type,
        start_at=start["at"],
        end_at=None if status == "open" else end["at"],
        duration_seconds=duration,
        room=room,
        source_kinds=["device_state"],
        start_observation_id=start.get("observation_id"),
        end_observation_id=end.get("observation_id"),
        observation_count=2 if end.get("observation_id") else 1,
        features={"timezone": "Asia/Shanghai", "evidence_observation_ids": [item for item in [start.get("observation_id"), end.get("observation_id")] if item]},
        status=status,
    )


def _build_night_wakes(elder_id: str, items: list[dict[str, Any]], now: datetime) -> list[BehaviorSegmentV2]:
    segments: list[BehaviorSegmentV2] = []
    wake_start: dict[str, Any] | None = None
    rooms: list[str] = []
    bathroom_start: dict[str, Any] | None = None
    bathroom_seconds = 0
    for item in items:
        room = item["room"]
        if room == "bedroom" and not item["present"] and _is_night(item["at"]) and wake_start is None:
            wake_start = item
            rooms = ["bedroom"]
            bathroom_seconds = 0
            continue
        if wake_start is None:
            continue
        if item["present"] and (not rooms or rooms[-1] != room):
            rooms.append(room)
        if room == "bathroom" and item["present"] and bathroom_start is None:
            bathroom_start = item
        elif room == "bathroom" and not item["present"] and bathroom_start is not None:
            bathroom_seconds += max(0, int((item["at"] - bathroom_start["at"]).total_seconds()))
            segments.append(_make_room_segment(elder_id, "bathroom_stay", "bathroom", bathroom_start, item, "closed"))
            bathroom_start = None
        if room == "bedroom" and item["present"]:
            segments.append(_make_night_wake_segment(elder_id, wake_start, item, "closed", rooms, bathroom_seconds, True))
            wake_start = None
            bathroom_start = None
    if wake_start is not None:
        if bathroom_start is not None:
            bathroom_seconds += max(0, int((now - bathroom_start["at"]).total_seconds()))
            segments.append(_make_room_segment(elder_id, "bathroom_stay", "bathroom", bathroom_start, {**bathroom_start, "at": now, "observation_id": None}, "open"))
        segments.append(_make_night_wake_segment(elder_id, wake_start, {**wake_start, "at": now, "observation_id": None}, "open", rooms, bathroom_seconds, False))
    return segments


def _make_night_wake_segment(
    elder_id: str,
    start: dict[str, Any],
    end: dict[str, Any],
    status: str,
    rooms: list[str],
    bathroom_seconds: int,
    returned: bool,
) -> BehaviorSegmentV2:
    duration = max(0, int((end["at"] - start["at"]).total_seconds()))
    return BehaviorSegmentV2(
        segment_id=_segment_id("night_wake", elder_id, str(start.get("observation_id") or start["at"].isoformat())),
        elder_id=elder_id,
        segment_type="night_wake",
        start_at=start["at"],
        end_at=None if status == "open" else end["at"],
        duration_seconds=duration,
        room="bedroom",
        source_kinds=["device_state"],
        start_observation_id=start.get("observation_id"),
        end_observation_id=end.get("observation_id"),
        observation_count=2 if end.get("observation_id") else 1,
        features={
            "timezone": "Asia/Shanghai",
            "rooms": rooms,
            "returned_to_bedroom": returned,
            "bathroom_stay_seconds": bathroom_seconds,
            "evidence_observation_ids": [item for item in [start.get("observation_id"), end.get("observation_id")] if item],
            "night_key": _night_key(start["at"]),
        },
        status=status,
    )


def build_vital_segments(observations: list[dict[str, Any]]) -> list[BehaviorSegmentV2]:
    grouped: dict[tuple[str, str, datetime], list[tuple[datetime, float, str | None]]] = defaultdict(list)
    for item in observations:
        if str(item.get("kind") or "") != "vital":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        elder_id = str(item.get("elder_id") or payload.get("elder_id") or settings.elder_id)
        observed_at = _parse_time(item.get("observed_at"))
        window_start = datetime.fromtimestamp(int(observed_at.timestamp()) // WINDOW_SECONDS * WINDOW_SECONDS, tz=timezone.utc)
        for metric in ("heart_rate", "spo2"):
            if payload.get(metric) is None:
                continue
            grouped[(elder_id, metric, window_start)].append((observed_at, float(payload[metric]), item.get("observation_id")))
    segments: list[BehaviorSegmentV2] = []
    for (elder_id, metric, start), rows in grouped.items():
        values = [value for _, value, _ in rows]
        ids = [obs_id for _, _, obs_id in rows if obs_id]
        latest_time, latest_value, latest_id = max(rows, key=lambda row: row[0])
        segment_type = "heart_rate_window" if metric == "heart_rate" else "spo2_window"
        abnormal = sum(value > 130 or value < 45 for value in values) if metric == "heart_rate" else sum(value < 92 for value in values)
        segments.append(
            BehaviorSegmentV2(
                segment_id=_segment_id(segment_type, elder_id, start.isoformat()),
                elder_id=elder_id,
                segment_type=segment_type,
                start_at=start,
                end_at=start + timedelta(seconds=WINDOW_SECONDS),
                duration_seconds=WINDOW_SECONDS,
                source_kinds=["vital"],
                start_observation_id=ids[0] if ids else None,
                end_observation_id=latest_id,
                observation_count=len(rows),
                features={
                    "metric": metric,
                    "avg": round(mean(values), 2),
                    "min": min(values),
                    "max": max(values),
                    "p10": _percentile(values, 0.1),
                    "p50": _percentile(values, 0.5),
                    "p90": _percentile(values, 0.9),
                    "latest_value": latest_value,
                    "latest_observed_at": latest_time.isoformat(),
                    "sample_count": len(rows),
                    "abnormal_count": abnormal,
                },
                status="closed",
            )
        )
    return segments


def build_baselines(elder_id: str, segments: list[dict[str, Any]], *, now: datetime) -> list[PersonalBaselineV2]:
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    recent = [item for item in segments if _parse_time(item.get("start_at")) >= cutoff]
    baselines = [
        _night_baseline(elder_id, recent, now),
        _bathroom_baseline(elder_id, recent, now),
        _vital_baseline(elder_id, recent, now, "heart_rate_daily", "heart_rate_window"),
        _vital_baseline(elder_id, recent, now, "spo2_daily", "spo2_window"),
    ]
    return baselines


def _night_baseline(elder_id: str, segments: list[dict[str, Any]], now: datetime) -> PersonalBaselineV2:
    wakes = [item for item in segments if item.get("segment_type") == "night_wake" and item.get("status") == "closed"]
    by_night: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in wakes:
        by_night[_night_key(_parse_time(item.get("start_at")))].append(item)
    durations = [float(item.get("duration_seconds") or 0) for item in wakes]
    counts = [len(items) for items in by_night.values()]
    returns = [bool((item.get("features") or {}).get("returned_to_bedroom")) for item in wakes]
    quality = "stable" if len(by_night) >= 3 else "insufficient_data"
    metrics = {
        "night_wake_count_avg": round(mean(counts), 2) if counts else 0,
        "night_wake_count_p90": _percentile([float(item) for item in counts], 0.9) if counts else DEFAULT_WAKE_COUNT_P90,
        "night_wake_duration_avg_sec": round(mean(durations), 1) if durations else 0,
        "night_wake_duration_p90_sec": _percentile(durations, 0.9) if durations else DEFAULT_NIGHT_WAKE_P90,
        "returned_to_bedroom_rate": round(sum(returns) / len(returns), 2) if returns else 0,
        "sample_nights": len(by_night),
        "fallback": "system_default" if quality == "insufficient_data" else None,
    }
    return PersonalBaselineV2(elder_id=elder_id, baseline_type="night_routine", period_end=now, sample_count=len(by_night), metrics=metrics, quality=quality)


def _bathroom_baseline(elder_id: str, segments: list[dict[str, Any]], now: datetime) -> PersonalBaselineV2:
    stays = [item for item in segments if item.get("segment_type") == "bathroom_stay" and item.get("status") == "closed"]
    durations = [float(item.get("duration_seconds") or 0) for item in stays]
    quality = "stable" if len(durations) >= 3 else "insufficient_data"
    metrics = {
        "bathroom_stay_avg_sec": round(mean(durations), 1) if durations else 0,
        "bathroom_stay_p90_sec": _percentile(durations, 0.9) if durations else DEFAULT_BATHROOM_STAY_P90,
        "night_bathroom_visits_avg": round(len(durations) / max(1, LOOKBACK_DAYS), 2),
        "sample_count": len(durations),
        "fallback": "system_default" if quality == "insufficient_data" else None,
    }
    return PersonalBaselineV2(elder_id=elder_id, baseline_type="bathroom_routine", period_end=now, sample_count=len(durations), metrics=metrics, quality=quality)


def _vital_baseline(elder_id: str, segments: list[dict[str, Any]], now: datetime, baseline_type: str, segment_type: str) -> PersonalBaselineV2:
    windows = [item for item in segments if item.get("segment_type") == segment_type]
    values = [float((item.get("features") or {}).get("avg")) for item in windows if (item.get("features") or {}).get("avg") is not None]
    quality = "stable" if len(values) >= 20 else "insufficient_data"
    if baseline_type == "heart_rate_daily":
        metrics = {
            "daily_avg": round(mean(values), 1) if values else 0,
            "night_avg": round(mean(values), 1) if values else 0,
            "p10": _percentile(values, 0.1) if values else 60,
            "p50": _percentile(values, 0.5) if values else 75,
            "p90": _percentile(values, 0.9) if values else DEFAULT_HEART_RATE_P90,
            "sample_count": len(values),
            "fallback": "system_default" if quality == "insufficient_data" else None,
        }
    else:
        metrics = {
            "avg": round(mean(values), 1) if values else 0,
            "night_avg": round(mean(values), 1) if values else 0,
            "p10": _percentile(values, 0.1) if values else DEFAULT_SPO2_P10,
            "p50": _percentile(values, 0.5) if values else 96,
            "p90": _percentile(values, 0.9) if values else 98,
            "low_count_avg_per_day": 0,
            "sample_count": len(values),
            "fallback": "system_default" if quality == "insufficient_data" else None,
        }
    return PersonalBaselineV2(elder_id=elder_id, baseline_type=baseline_type, period_end=now, sample_count=len(values), metrics=metrics, quality=quality)


def build_candidates(
    elder_id: str,
    segments: list[dict[str, Any]],
    baselines: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[AiReviewCandidateV2]:
    existing_keys = {str((item.get("features") or {}).get("dedupe_key")) for item in existing}
    baseline_map = {item.get("baseline_type"): item for item in baselines}
    night_p90 = float((baseline_map.get("night_routine", {}).get("metrics") or {}).get("night_wake_duration_p90_sec") or DEFAULT_NIGHT_WAKE_P90)
    night_count_p90 = float((baseline_map.get("night_routine", {}).get("metrics") or {}).get("night_wake_count_p90") or DEFAULT_WAKE_COUNT_P90)
    bathroom_p90 = float((baseline_map.get("bathroom_routine", {}).get("metrics") or {}).get("bathroom_stay_p90_sec") or DEFAULT_BATHROOM_STAY_P90)
    heart_metrics = baseline_map.get("heart_rate_daily", {}).get("metrics") or {}
    spo2_metrics = baseline_map.get("spo2_daily", {}).get("metrics") or {}
    heart_p90 = float(heart_metrics.get("p90") or DEFAULT_HEART_RATE_P90)
    heart_p10 = float(heart_metrics.get("p10") or DEFAULT_HEART_RATE_P10)
    spo2_p10 = float(spo2_metrics.get("p10") or DEFAULT_SPO2_P10)
    candidates: list[AiReviewCandidateV2] = []
    night_wakes_by_night: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for segment in segments:
        if segment.get("segment_type") == "night_wake":
            features = segment.get("features") or {}
            night_key = str(features.get("night_key") or _night_key(_parse_time(segment.get("start_at"))))
            if night_key != _night_key(now) or not _is_night(now):
                continue
            night_wakes_by_night[night_key].append(segment)
    for night_key, night_segments in night_wakes_by_night.items():
        if len(night_segments) <= night_count_p90:
            continue
        source_ids = [str(item.get("segment_id")) for item in night_segments if item.get("segment_id")]
        key = f"night_behavior_anomaly:wake_count:{elder_id}:{night_key}"
        if key in existing_keys:
            continue
        candidates.append(
            AiReviewCandidateV2(
                elder_id=elder_id,
                candidate_type="night_behavior_anomaly",
                reason="night wake count exceeds personal p90",
                source_segment_ids=source_ids,
                features={
                    "dedupe_key": key,
                    "night_key": night_key,
                    "wake_count": len(night_segments),
                    "baseline_wake_count_p90": night_count_p90,
                    "segments": night_segments,
                },
            )
        )
    for segment in segments:
        features = segment.get("features") or {}
        segment_id = str(segment.get("segment_id"))
        if (
            segment.get("segment_type") == "night_wake"
            and _is_current_open_night_wake(segment, now)
            and float(segment.get("duration_seconds") or 0) > night_p90
        ):
            key = f"night_behavior_anomaly:{segment_id}"
            if key not in existing_keys:
                candidates.append(
                    AiReviewCandidateV2(
                        elder_id=elder_id,
                        candidate_type="night_behavior_anomaly",
                        reason="起夜持续时间超过个人90分位",
                        source_segment_ids=[segment_id],
                        features={"dedupe_key": key, "duration_seconds": segment.get("duration_seconds"), "baseline_p90_seconds": night_p90, "segment": segment},
                    )
                )
        if (
            segment.get("segment_type") == "bathroom_stay"
            and segment.get("status") == "open"
            and _is_recent_segment(segment, now)
            and float(segment.get("duration_seconds") or 0) > bathroom_p90
        ):
            key = f"night_behavior_anomaly:{segment_id}"
            if key not in existing_keys:
                candidates.append(
                    AiReviewCandidateV2(
                        elder_id=elder_id,
                        candidate_type="night_behavior_anomaly",
                        reason="卫生间停留超过个人90分位",
                        source_segment_ids=[segment_id],
                        features={"dedupe_key": key, "duration_seconds": segment.get("duration_seconds"), "baseline_p90_seconds": bathroom_p90, "segment": segment},
                    )
                )
        if False and segment.get("segment_type") == "heart_rate_window" and float(features.get("p90") or 0) > heart_p90:
            key = f"vital_baseline_anomaly:{segment_id}"
            if key not in existing_keys:
                candidates.append(AiReviewCandidateV2(elder_id=elder_id, candidate_type="vital_baseline_anomaly", reason="心率窗口高于个人90分位", source_segment_ids=[segment_id], features={"dedupe_key": key, "segment": segment, "baseline_p90": heart_p90}))
        if False and segment.get("segment_type") == "spo2_window" and float(features.get("p10") or 100) < spo2_p10:
            key = f"vital_baseline_anomaly:{segment_id}"
            if key not in existing_keys:
                candidates.append(AiReviewCandidateV2(elder_id=elder_id, candidate_type="vital_baseline_anomaly", reason="血氧窗口低于个人10分位", source_segment_ids=[segment_id], features={"dedupe_key": key, "segment": segment, "baseline_p10": spo2_p10}))
        if segment.get("segment_type") == "heart_rate_window":
            min_value = float(features.get("min") or 0)
            max_value = float(features.get("max") or 0)
            latest_value = float(features.get("latest_value") or 0)
            sample_count = int(features.get("sample_count") or segment.get("observation_count") or 0)
            if sample_count < MIN_VITAL_CANDIDATE_SAMPLES or not _is_recent_segment(segment, now):
                continue
            if latest_value > heart_p90 and max_value <= HEART_RATE_P1_HIGH and float(features.get("p90") or 0) > heart_p90:
                key = f"vital_baseline_anomaly:{segment_id}:heart_rate:high"
                if key not in existing_keys:
                    candidates.append(
                        AiReviewCandidateV2(
                            elder_id=elder_id,
                            candidate_type="vital_baseline_anomaly",
                            reason="heart rate window above personal p90",
                            source_segment_ids=[segment_id],
                            features=_vital_candidate_features(segment, "heart_rate", "high", key, baseline_p90=heart_p90),
                        )
                    )
            elif latest_value < heart_p10 and min_value >= HEART_RATE_P1_LOW and float(features.get("p10") or 999) < heart_p10:
                key = f"vital_baseline_anomaly:{segment_id}:heart_rate:low"
                if key not in existing_keys:
                    candidates.append(
                        AiReviewCandidateV2(
                            elder_id=elder_id,
                            candidate_type="vital_baseline_anomaly",
                            reason="heart rate window below personal p10",
                            source_segment_ids=[segment_id],
                            features=_vital_candidate_features(segment, "heart_rate", "low", key, baseline_p10=heart_p10),
                        )
                    )
        if segment.get("segment_type") == "spo2_window":
            min_value = float(features.get("min") or 100)
            latest_value = float(features.get("latest_value") or 100)
            sample_count = int(features.get("sample_count") or segment.get("observation_count") or 0)
            if (
                sample_count >= MIN_VITAL_CANDIDATE_SAMPLES
                and _is_recent_segment(segment, now)
                and latest_value < spo2_p10
                and min_value >= SPO2_P1_LOW
                and float(features.get("p10") or 100) < spo2_p10
            ):
                key = f"vital_baseline_anomaly:{segment_id}:spo2:low"
                if key not in existing_keys:
                    candidates.append(
                        AiReviewCandidateV2(
                            elder_id=elder_id,
                            candidate_type="vital_baseline_anomaly",
                            reason="spo2 window below personal p10",
                            source_segment_ids=[segment_id],
                            features=_vital_candidate_features(segment, "spo2", "low", key, baseline_p10=spo2_p10),
                        )
                    )
    return candidates


def _vital_candidate_features(
    segment: dict[str, Any],
    metric: str,
    direction: str,
    dedupe_key: str,
    *,
    baseline_p90: float | None = None,
    baseline_p10: float | None = None,
) -> dict[str, Any]:
    features = segment.get("features") if isinstance(segment.get("features"), dict) else {}
    summary = {
        "segment_id": segment.get("segment_id"),
        "segment_type": segment.get("segment_type"),
        "start_at": segment.get("start_at"),
        "end_at": segment.get("end_at"),
    }
    result = {
        "dedupe_key": dedupe_key,
        "metric": metric,
        "direction": direction,
        "latest_value": features.get("latest_value"),
        "min": features.get("min"),
        "max": features.get("max"),
        "p10": features.get("p10"),
        "p90": features.get("p90"),
        "sample_count": features.get("sample_count"),
        "window_seconds": segment.get("duration_seconds") or WINDOW_SECONDS,
        "segment_summary": {key: value for key, value in summary.items() if value is not None},
    }
    if baseline_p90 is not None:
        result["baseline_p90"] = baseline_p90
    if baseline_p10 is not None:
        result["baseline_p10"] = baseline_p10
    return {key: value for key, value in result.items() if value is not None}
