from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))
sys.path.insert(0, str(ROOT / "apps" / "guardian-orchestrator"))

from app.night_activity import (
    NightActivityMonitor,
    SHANGHAI,
    most_recent_cutoff,
    next_trigger_at,
    presence_value,
)


def local_time(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


def observation(present: bool, at: datetime, observation_id: str = "obs-1") -> dict:
    return {
        "observation_id": observation_id,
        "elder_id": "elder_001",
        "kind": "device_state",
        "payload": {"room": "bedroom", "device": "presence_sensor", "present": present, "state": "present" if present else "absent"},
        "observed_at": at.isoformat(),
    }


class NightActivityPureRuleTests(unittest.TestCase):
    def test_2158_absence_triggers_at_2203(self) -> None:
        absent = local_time(2026, 6, 14, 21, 58)
        self.assertEqual(next_trigger_at(absent, absent), local_time(2026, 6, 14, 22, 3))

    def test_absence_longer_than_five_minutes_triggers_at_2200(self) -> None:
        absent = local_time(2026, 6, 14, 21, 50)
        self.assertEqual(next_trigger_at(absent, absent), local_time(2026, 6, 14, 22, 0))

    def test_0558_absence_cannot_trigger_after_0600(self) -> None:
        absent = local_time(2026, 6, 15, 5, 58)
        self.assertIsNone(next_trigger_at(absent, absent))

    def test_only_explicit_bedroom_presence_is_recognized(self) -> None:
        self.assertIs(presence_value(observation(False, local_time(2026, 6, 14, 22))), False)
        light_off = {"kind": "device_state", "payload": {"room": "bedroom", "device": "light", "state": "off"}}
        self.assertIsNone(presence_value(light_off))

    def test_cutoff_is_latest_shanghai_0600(self) -> None:
        self.assertEqual(most_recent_cutoff(local_time(2026, 6, 14, 23)), local_time(2026, 6, 14, 6))
        self.assertEqual(most_recent_cutoff(local_time(2026, 6, 15, 3)), local_time(2026, 6, 14, 6))


class NightActivityMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_minutes_absent_creates_one_p2_event(self) -> None:
        now = local_time(2026, 6, 14, 22, 5)
        callback = AsyncMock()
        monitor = NightActivityMonitor(callback, now=lambda: now)
        yield_loop = asyncio.sleep
        sleeps = 0

        async def fake_sleep(_: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        with patch("app.night_activity.asyncio.sleep", side_effect=fake_sleep):
            await monitor.observe(observation(False, now - timedelta(minutes=5)))
            await yield_loop(0)
            await yield_loop(0)

        callback.assert_awaited_once()
        event = callback.await_args.args[0]
        self.assertEqual(str(event.event_type), "night_abnormal_activity")
        self.assertEqual(str(event.risk_level), "P2")
        self.assertEqual(event.rule_trace["duration_seconds"], 300)
        await monitor.close()

    async def test_present_cancels_pending_absence(self) -> None:
        now = local_time(2026, 6, 14, 22, 0)
        callback = AsyncMock()
        monitor = NightActivityMonitor(callback, now=lambda: now)
        blocker = asyncio.Event()
        yield_loop = asyncio.sleep

        async def blocked_sleep(_: float) -> None:
            await blocker.wait()

        with patch("app.night_activity.asyncio.sleep", side_effect=blocked_sleep):
            await monitor.observe(observation(False, now))
            first_task = monitor.states["elder_001"].task
            await monitor.observe(observation(False, now + timedelta(minutes=1), "obs-2"))
            self.assertIs(monitor.states["elder_001"].task, first_task)
            await monitor.observe(observation(True, now + timedelta(minutes=4), "obs-3"))
            await yield_loop(0)

        self.assertNotIn("elder_001", monitor.states)
        callback.assert_not_awaited()

    async def test_restore_ignores_state_before_latest_cutoff(self) -> None:
        now = local_time(2026, 6, 14, 23)
        callback = AsyncMock()
        monitor = NightActivityMonitor(callback, now=lambda: now)
        await monitor.restore([observation(False, local_time(2026, 6, 14, 5, 50))])
        self.assertFalse(monitor.states)

    async def test_restore_resumes_recent_absence(self) -> None:
        now = local_time(2026, 6, 14, 22)
        monitor = NightActivityMonitor(AsyncMock(), now=lambda: now)
        await monitor.restore([observation(False, local_time(2026, 6, 14, 21, 58))])
        self.assertEqual(monitor.states["elder_001"].absent_since, local_time(2026, 6, 14, 21, 58))
        await monitor.close()


if __name__ == "__main__":
    unittest.main()
