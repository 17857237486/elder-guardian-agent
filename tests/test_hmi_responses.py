from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HmiResponsePersistenceTests(unittest.TestCase):
    def test_responses_are_persisted_ordered_limited_and_idempotent(self) -> None:
        script = textwrap.dedent(
            """
            from datetime import datetime, timedelta, timezone

            from app.database import Base, engine, SessionLocal
            from app import models, repository
            from guardian_shared.v2 import HmiPromptV2, HmiResponseV2

            Base.metadata.create_all(bind=engine)
            start = datetime(2026, 6, 15, tzinfo=timezone.utc)
            with SessionLocal() as db:
                for index in range(12):
                    prompt = HmiPromptV2(
                        prompt_id=f"prompt_{index}",
                        workflow_id=f"workflow_{index}",
                        event_id=f"event_{index}",
                        elder_id="elder_001",
                        risk_level="P2",
                        event_type="long_static",
                        message="请确认状态",
                        created_at=start + timedelta(seconds=index),
                    )
                    repository.create_hmi_prompt(db, prompt)
                    response = HmiResponseV2(
                        prompt_id=prompt.prompt_id,
                        event_id=prompt.event_id,
                        elder_id=prompt.elder_id,
                        response_type="safe" if index % 2 == 0 else "help",
                        response_text="我没事" if index % 2 == 0 else "需要帮助",
                        created_at=start + timedelta(seconds=index),
                    )
                    assert repository.respond_hmi_prompt(db, response) is not None
                    assert repository.respond_hmi_prompt(db, response) is None

                rows = repository.list_hmi_responses(db, "elder_001", limit=10)
                assert len(rows) == 10
                assert rows[0]["prompt_id"] == "prompt_11"
                assert rows[-1]["prompt_id"] == "prompt_2"
                assert rows[0]["outcome"] == "family_alert"
                state = repository.dashboard_state(db, "elder_001")
                assert len(state["hmi_responses"]) == 10
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{Path(directory, 'hmi.db').as_posix()}"
            paths = [ROOT / "apps" / "edge-mcp-server", ROOT / "packages" / "guardian-shared"]
            env["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_resolved_events_do_not_drive_current_risk_level(self) -> None:
        script = textwrap.dedent(
            """
            from datetime import datetime, timedelta, timezone

            from app.database import Base, engine, SessionLocal
            from app import repository
            from guardian_shared.v2 import NormalizedEventV2

            Base.metadata.create_all(bind=engine)
            start = datetime(2026, 6, 15, tzinfo=timezone.utc)
            with SessionLocal() as db:
                repository.create_event(
                    db,
                    NormalizedEventV2(
                        event_id="event_resolved_p3",
                        elder_id="elder_001",
                        event_type="humidity_abnormal",
                        risk_level="P3",
                        state="resolved",
                        room="living_room",
                        summary="living_room humidity 82.0% is outside the safe comfort range.",
                        created_at=start,
                        updated_at=start,
                    ),
                )
                state = repository.dashboard_state(db, "elder_001")
                assert state["current_risk_level"] == "P4"
                assert state["current_hmi_prompt"] is None

                repository.create_event(
                    db,
                    NormalizedEventV2(
                        event_id="event_active_p2",
                        elder_id="elder_001",
                        event_type="long_static",
                        risk_level="P2",
                        state="wait_response",
                        room="living_room",
                        summary="请确认状态",
                        created_at=start + timedelta(seconds=1),
                        updated_at=start + timedelta(seconds=1),
                    ),
                )
                state = repository.dashboard_state(db, "elder_001")
                assert state["current_risk_level"] == "P2"
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{Path(directory, 'hmi.db').as_posix()}"
            paths = [ROOT / "apps" / "edge-mcp-server", ROOT / "packages" / "guardian-shared"]
            env["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
