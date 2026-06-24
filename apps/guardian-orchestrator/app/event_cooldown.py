from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable

from guardian_shared.enums import EventType
from guardian_shared.v2 import NormalizedEventV2


DEDUPE_P3_ENVIRONMENT_EVENTS = {
    EventType.CO2_HIGH.value,
    EventType.TEMPERATURE_HIGH.value,
    EventType.TEMPERATURE_LOW.value,
    "humidity_abnormal",
}
DEDUPE_P1_VITAL_EVENTS = {
    EventType.HEART_RATE_ABNORMAL.value,
    EventType.SPO2_LOW.value,
}
DEDUPE_P0_GAS_EVENTS = {
    EventType.GAS_LEAK.value,
}


def _risk_text(value: Any) -> str:
    return str(value).split(".")[-1].upper()


@dataclass(frozen=True)
class CooldownResult:
    suppressed: bool
    dedupe_key: str
    remaining_sec: float = 0.0


class P3EnvironmentCooldown:
    def __init__(self, cooldown_sec: int, clock: Callable[[], float] = monotonic) -> None:
        self.cooldown_sec = max(0, cooldown_sec)
        self.clock = clock
        self._expires_at: dict[str, float] = {}

    def check(self, event: NormalizedEventV2) -> CooldownResult:
        key = self.dedupe_key(event)
        if not self.should_cooldown(event) or self.cooldown_sec <= 0:
            return CooldownResult(False, key)

        now = self.clock()
        self._drop_expired(now)
        expires_at = self._expires_at.get(key)
        if expires_at and expires_at > now:
            return CooldownResult(True, key, expires_at - now)

        self._expires_at[key] = now + self.cooldown_sec
        return CooldownResult(False, key)

    @staticmethod
    def should_cooldown(event: NormalizedEventV2) -> bool:
        return (
            _risk_text(event.risk_level) == "P3"
            and str(event.source_kind) == "environment"
            and str(event.event_type) in DEDUPE_P3_ENVIRONMENT_EVENTS
        )

    @staticmethod
    def dedupe_key(event: NormalizedEventV2) -> str:
        return f"{event.elder_id}:{event.event_type}:{event.room or '-'}"

    def _drop_expired(self, now: float) -> None:
        expired = [key for key, expires_at in self._expires_at.items() if expires_at <= now]
        for key in expired:
            self._expires_at.pop(key, None)


class VitalEventCooldown:
    def __init__(self, cooldown_sec: int, clock: Callable[[], float] = monotonic) -> None:
        self.cooldown_sec = max(0, cooldown_sec)
        self.clock = clock
        self._expires_at: dict[str, float] = {}

    def check(self, event: NormalizedEventV2) -> CooldownResult:
        key = self.dedupe_key(event)
        if not self.should_cooldown(event) or self.cooldown_sec <= 0:
            return CooldownResult(False, key)

        now = self.clock()
        self._drop_expired(now)
        expires_at = self._expires_at.get(key)
        if expires_at and expires_at > now:
            return CooldownResult(True, key, expires_at - now)

        self._expires_at[key] = now + self.cooldown_sec
        return CooldownResult(False, key)

    @staticmethod
    def should_cooldown(event: NormalizedEventV2) -> bool:
        return (
            _risk_text(event.risk_level) == "P1"
            and str(event.source_kind) == "vital"
            and str(event.event_type) in DEDUPE_P1_VITAL_EVENTS
        )

    @staticmethod
    def dedupe_key(event: NormalizedEventV2) -> str:
        return f"{event.elder_id}:{event.event_type}:{_risk_text(event.risk_level)}"

    def _drop_expired(self, now: float) -> None:
        expired = [key for key, expires_at in self._expires_at.items() if expires_at <= now]
        for key in expired:
            self._expires_at.pop(key, None)


class GasLeakCooldown:
    def __init__(self, cooldown_sec: int, clock: Callable[[], float] = monotonic) -> None:
        self.cooldown_sec = max(0, cooldown_sec)
        self.clock = clock
        self._expires_at: dict[str, float] = {}

    def check(self, event: NormalizedEventV2) -> CooldownResult:
        key = self.dedupe_key(event)
        if not self.should_cooldown(event) or self.cooldown_sec <= 0:
            return CooldownResult(False, key)

        now = self.clock()
        self._drop_expired(now)
        expires_at = self._expires_at.get(key)
        if expires_at and expires_at > now:
            return CooldownResult(True, key, expires_at - now)

        self._expires_at[key] = now + self.cooldown_sec
        return CooldownResult(False, key)

    @staticmethod
    def should_cooldown(event: NormalizedEventV2) -> bool:
        return (
            _risk_text(event.risk_level) == "P0"
            and str(event.source_kind) == "environment"
            and str(event.event_type) in DEDUPE_P0_GAS_EVENTS
        )

    @staticmethod
    def dedupe_key(event: NormalizedEventV2) -> str:
        return f"{event.elder_id}:{event.event_type}:{event.room or '-'}"

    def _drop_expired(self, now: float) -> None:
        expired = [key for key, expires_at in self._expires_at.items() if expires_at <= now]
        for key in expired:
            self._expires_at.pop(key, None)
