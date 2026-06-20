from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_edge_script(script: str, *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{Path(directory, 'behavior.db').as_posix()}"
        env["ORCHESTRATOR_URL"] = ""
        paths = [ROOT / "apps" / "edge-mcp-server", ROOT / "packages" / "guardian-shared"]
        env["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


class BehaviorBaselineTests(unittest.TestCase):
    def test_presence_and_vital_segments_baselines_and_candidates(self) -> None:
        script = textwrap.dedent(
            """
            from datetime import datetime, timedelta, timezone

            from app.behavior_worker import (
                build_baselines,
                build_candidates,
                build_presence_segments,
                build_vital_segments,
            )

            elder_id = "elder_001"

            def obs(obs_id, kind, at, payload):
                return {
                    "observation_id": obs_id,
                    "elder_id": elder_id,
                    "kind": kind,
                    "source": "test",
                    "payload": payload,
                    "observed_at": at.isoformat(),
                }

            base = datetime(2026, 6, 18, 14, 10, tzinfo=timezone.utc)  # 22:10 Asia/Shanghai
            observations = [
                obs("bed_on", "device_state", base, {"room": "bedroom", "present": True, "state": "present"}),
                obs("bed_off", "device_state", base + timedelta(minutes=50), {"room": "bedroom", "present": False, "state": "absent"}),
                obs("bath_on", "device_state", base + timedelta(minutes=52), {"room": "bathroom", "present": True, "state": "present"}),
                obs("bath_off", "device_state", base + timedelta(minutes=58), {"room": "bathroom", "present": False, "state": "absent"}),
                obs("bed_back", "device_state", base + timedelta(minutes=62), {"room": "bedroom", "present": True, "state": "present"}),
            ]
            segments = build_presence_segments(observations, now=base + timedelta(minutes=70))
            types = {item.segment_type for item in segments}
            assert {"room_stay", "night_sleep", "night_wake", "bathroom_stay"}.issubset(types), types
            night_wake = [item for item in segments if item.segment_type == "night_wake"][0]
            assert night_wake.duration_seconds == 720, night_wake
            assert night_wake.features["returned_to_bedroom"] is True
            assert night_wake.features["bathroom_stay_seconds"] == 360

            vital_obs = []
            for index in range(18):
                vital_obs.append(
                    obs(
                        f"vital_{index}",
                        "vital",
                        base + timedelta(seconds=index * 10),
                        {"heart_rate": 70 + index, "spo2": 97 - (index % 3)},
                    )
                )
            vital_segments = build_vital_segments(vital_obs)
            vital_types = {item.segment_type for item in vital_segments}
            assert {"heart_rate_window", "spo2_window"}.issubset(vital_types), vital_types
            heart = [item for item in vital_segments if item.segment_type == "heart_rate_window"][0]
            assert heart.features["sample_count"] == 18
            assert heart.features["max"] == 87

            segment_dicts = [item.model_dump(mode="json") for item in segments + vital_segments]
            for day in range(1, 4):
                shifted = dict(night_wake.model_dump(mode="json"))
                shifted["segment_id"] = f"seg_extra_{day}"
                shifted["start_at"] = (base - timedelta(days=day)).isoformat()
                shifted["end_at"] = (base - timedelta(days=day, minutes=-12)).isoformat()
                segment_dicts.append(shifted)
            baselines = build_baselines(elder_id, segment_dicts, now=base + timedelta(days=1))
            baseline_map = {item.baseline_type: item for item in baselines}
            assert baseline_map["night_routine"].quality == "stable"
            assert baseline_map["night_routine"].metrics["night_wake_count_p90"] >= 1

            baseline_records = [
                {
                    "baseline_type": "night_routine",
                    "metrics": {"night_wake_duration_p90_sec": 480, "night_wake_count_p90": 2},
                },
                {"baseline_type": "bathroom_routine", "metrics": {"bathroom_stay_p90_sec": 300}},
                {"baseline_type": "heart_rate_daily", "metrics": {"p90": 75}},
                {"baseline_type": "spo2_daily", "metrics": {"p10": 95}},
            ]
            candidates = build_candidates(elder_id, segment_dicts, baseline_records, [], now=base + timedelta(days=1))
            candidate_types = {item.candidate_type for item in candidates}
            assert "night_behavior_anomaly" in candidate_types
            assert "vital_baseline_anomaly" in candidate_types
            assert all(item.status == "pending" for item in candidates)
            """
        )
        result = run_edge_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_behavior_baseline_candidate_http_api(self) -> None:
        script = textwrap.dedent(
            """
            from fastapi.testclient import TestClient

            from app.database import Base, engine
            from app.main import app

            Base.metadata.create_all(bind=engine)
            client = TestClient(app)

            segment = {
                "segment_id": "seg_test_night_wake",
                "elder_id": "elder_001",
                "segment_type": "night_wake",
                "start_at": "2026-06-18T23:10:00+08:00",
                "duration_seconds": 720,
                "room": "bedroom",
                "features": {"rooms": ["bedroom", "bathroom", "living_room"], "returned_to_bedroom": False},
                "status": "open",
            }
            baseline = {
                "elder_id": "elder_001",
                "baseline_type": "night_routine",
                "scope": "default",
                "timezone": "Asia/Shanghai",
                "lookback_days": 14,
                "sample_count": 14,
                "quality": "stable",
                "metrics": {"night_wake_duration_p90_sec": 480, "night_wake_count_p90": 2},
            }
            candidate = {
                "candidate_id": "cand_test_night_wake",
                "elder_id": "elder_001",
                "candidate_type": "night_behavior_anomaly",
                "priority": "low",
                "reason": "night wake exceeds personal p90",
                "source_segment_ids": ["seg_test_night_wake"],
                "features": {"duration_seconds": 720, "baseline_p90_seconds": 480},
            }

            assert client.post("/api/v2/behavior-segments", json=segment).status_code == 200
            assert client.post("/api/v2/personal-baselines", json=baseline).status_code == 200
            assert client.post("/api/v2/ai-review-candidates", json=candidate).status_code == 200

            state = client.get("/api/v2/dashboard/state?elder_id=elder_001").json()
            assert state["behavior_segments"][0]["segment_id"] == "seg_test_night_wake"
            assert state["personal_baselines"][0]["baseline_type"] == "night_routine"
            assert state["ai_review_candidates"][0]["candidate_id"] == "cand_test_night_wake"

            patched = client.patch("/api/v2/ai-review-candidates/cand_test_night_wake", json={"status": "dismissed"})
            assert patched.status_code == 200
            assert patched.json()["ai_review_candidate"]["status"] == "dismissed"
            """
        )
        result = run_edge_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mild_vital_baseline_anomalies_create_candidates_without_hard_rule_duplicates(self) -> None:
        script = textwrap.dedent(
            """
            from datetime import datetime, timedelta, timezone

            from app.behavior_worker import build_candidates, build_vital_segments

            elder_id = "elder_001"
            base = datetime(2026, 6, 20, 9, 0, tzinfo=timezone.utc)

            def obs(obs_id, seconds, heart_rate, spo2):
                return {
                    "observation_id": obs_id,
                    "elder_id": elder_id,
                    "kind": "vital",
                    "payload": {"heart_rate": heart_rate, "spo2": spo2},
                    "observed_at": (base + timedelta(seconds=seconds)).isoformat(),
                }

            observations = []
            for index in range(6):
                observations.append(obs(f"hr_high_{index}", index * 10, 115, 96))
                observations.append(obs(f"hr_low_{index}", 300 + index * 10, 50, 96))
                observations.append(obs(f"spo2_low_{index}", 600 + index * 10, 78, 94))
                observations.append(obs(f"hard_hr_{index}", 900 + index * 10, 138, 96))
                observations.append(obs(f"hard_spo2_{index}", 1200 + index * 10, 80, 90))

            segments = [item.model_dump(mode="json") for item in build_vital_segments(observations)]
            baselines = [
                {"baseline_type": "heart_rate_daily", "metrics": {"p10": 58, "p90": 100}},
                {"baseline_type": "spo2_daily", "metrics": {"p10": 95}},
            ]
            candidates = build_candidates(elder_id, segments, baselines, [], now=base + timedelta(hours=1))
            vital_candidates = [item for item in candidates if item.candidate_type == "vital_baseline_anomaly"]
            keys = {item.features["dedupe_key"] for item in vital_candidates}
            features = {(item.features["metric"], item.features["direction"]) for item in vital_candidates}

            assert ("heart_rate", "high") in features, features
            assert ("heart_rate", "low") in features, features
            assert ("spo2", "low") in features, features
            assert all("hard_hr" not in key for key in keys), keys
            assert all("hard_spo2" not in key for key in keys), keys
            assert all("segment" not in item.features for item in vital_candidates), vital_candidates

            repeated = build_candidates(
                elder_id,
                segments,
                baselines,
                [item.model_dump(mode="json") for item in vital_candidates],
                now=base + timedelta(hours=1),
            )
            assert not [item for item in repeated if item.candidate_type == "vital_baseline_anomaly"], repeated
            """
        )
        result = run_edge_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
