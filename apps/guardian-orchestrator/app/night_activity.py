from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from guardian_shared.enums import EventType, RiskLevel
from guardian_shared.v2 import NormalizedEventV2


logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
ABSENCE_DURATION = timedelta(minutes=5)
NIGHT_START = time(22, 0)
NIGHT_END = time(6, 0)


def parse_observed_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def presence_value(observation: dict[str, Any]) -> bool | None:
    if str(observation.get("kind") or "") not in {"device_state", "vision"}:
        return None
    payload = observation.get("payload")
    if not isinstance(payload, dict) or str(payload.get("room") or "").lower() != "bedroom":
        return None
    present = payload.get("present")
    state = str(payload.get("state") or "").lower()
    if present is True or state == "present":
        return True
    if present is False or state == "absent":
        return False
    return None


def night_bounds(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(SHANGHAI)
    if local.time() < NIGHT_END:
        start_date = local.date() - timedelta(days=1)
    else:
        start_date = local.date()
    start = datetime.combine(start_date, NIGHT_START, SHANGHAI)
    return start, start + timedelta(hours=8)


def next_trigger_at(absent_since: datetime, now: datetime) -> datetime | None:
    start, end = night_bounds(now)
    due = max(absent_since.astimezone(SHANGHAI) + ABSENCE_DURATION, start)
    return due if due < end else None


def most_recent_cutoff(now: datetime) -> datetime:
    local = now.astimezone(SHANGHAI)
    cutoff = datetime.combine(local.date(), NIGHT_END, SHANGHAI)
    return cutoff if local >= cutoff else cutoff - timedelta(days=1)


@dataclass
class AbsenceState:
    absent_since: datetime
    observation_ids: list[str] = field(default_factory=list)
    source_kind: str = "device_state"
    triggered: bool = False
    task: asyncio.Task[None] | None = None


class NightActivityMonitor:
    def __init__(
        self,
        on_event: Callable[[NormalizedEventV2], Awaitable[Any]],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.on_event = on_event
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.states: dict[str, AbsenceState] = {}

    async def observe(self, observation: dict[str, Any]) -> None:
        present = presence_value(observation)
        if present is None:
            return
        elder_id = str(observation.get("elder_id") or "")
        if not elder_id:
            return
        if present:
            self._clear(elder_id)
            return
        observed_at = parse_observed_at(observation.get("observed_at"))
        state = self.states.get(elder_id)
        if state is None:
            state = AbsenceState(absent_since=observed_at, source_kind=str(observation.get("kind") or "device_state"))
            self.states[elder_id] = state
        observation_id = observation.get("observation_id")
        if observation_id and str(observation_id) not in state.observation_ids:
            state.observation_ids.append(str(observation_id))
        self._schedule(elder_id, state)

    async def restore(self, observations: list[dict[str, Any]]) -> None:
        latest: dict[str, dict[str, Any]] = {}
        cutoff = most_recent_cutoff(self.now())
        for observation in observations:
            if presence_value(observation) is None:
                continue
            if parse_observed_at(observation.get("observed_at")).astimezone(SHANGHAI) < cutoff:
                continue
            elder_id = str(observation.get("elder_id") or "")
            previous = latest.get(elder_id)
            if not previous or parse_observed_at(observation.get("observed_at")) > parse_observed_at(previous.get("observed_at")):
                latest[elder_id] = observation
        for observation in latest.values():
            if presence_value(observation) is False:
                await self.observe(observation)

    async def close(self) -> None:
        for elder_id in list(self.states):
            self._clear(elder_id)

    def _schedule(self, elder_id: str, state: AbsenceState) -> None:
        if state.triggered or (state.task and not state.task.done()):
            return
        state.task = asyncio.create_task(self._wait_and_trigger(elder_id, state))

    async def _wait_and_trigger(self, elder_id: str, state: AbsenceState) -> None:
        try:
            now = self.now()
            trigger_at = next_trigger_at(state.absent_since, now)
            if trigger_at is None:
                self._clear(elder_id, cancel_task=False)
                return
            await asyncio.sleep(max(0.0, (trigger_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()))
            if self.states.get(elder_id) is not state or state.triggered:
                return
            current = self.now()
            start, end = night_bounds(current)
            local_current = current.astimezone(SHANGHAI)
            if not (start <= local_current < end) or current - state.absent_since < ABSENCE_DURATION:
                self._clear(elder_id, cancel_task=False)
                return
            state.triggered = True
            duration = int((current - state.absent_since).total_seconds())
            event = NormalizedEventV2(
                elder_id=elder_id,
                event_type=EventType.NIGHT_ABNORMAL_ACTIVITY,
                risk_level=RiskLevel.P2,
                risk_score=0.68,
                room="bedroom",
                summary="夜间卧室持续无人 5 分钟，需要确认老人是否安全。",
                trigger_observation_ids=state.observation_ids[-20:],
                rule_trace={
                    "rule": "night_bedroom_absent_5m",
                    "timezone": "Asia/Shanghai",
                    "absent_since": state.absent_since.isoformat(),
                    "triggered_at": current.isoformat(),
                    "duration_seconds": duration,
                    "observation_ids": state.observation_ids[-20:],
                },
                source_kind=state.source_kind,
                evidence=[
                    {
                        "kind": state.source_kind,
                        "room": "bedroom",
                        "state": "absent",
                        "absent_since": state.absent_since.isoformat(),
                        "duration_seconds": duration,
                    }
                ],
                rule_risk_level=RiskLevel.P2,
                local_risk_level=RiskLevel.P2,
                final_risk_level=RiskLevel.P2,
                confidence=0.68,
            )
            try:
                await self.on_event(event)
            except Exception:
                logger.exception("night activity workflow failed elder_id=%s", elder_id)
            _, end = night_bounds(current)
            await asyncio.sleep(max(0.0, (end - self.now().astimezone(SHANGHAI)).total_seconds()))
            if self.states.get(elder_id) is state:
                self._clear(elder_id, cancel_task=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("night activity timer failed elder_id=%s", elder_id)
            self._clear(elder_id, cancel_task=False)

    def _clear(self, elder_id: str, *, cancel_task: bool = True) -> None:
        state = self.states.pop(elder_id, None)
        if cancel_task and state and state.task and state.task is not asyncio.current_task():
            state.task.cancel()
