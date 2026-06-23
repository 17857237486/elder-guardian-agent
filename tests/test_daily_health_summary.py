from __future__ import annotations

import json
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
        env["DATABASE_URL"] = f"sqlite:///{Path(directory, 'daily.db').as_posix()}"
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


class DailyHealthSummaryTests(unittest.TestCase):
    def test_edge_builds_and_persists_local_daily_summary(self) -> None:
        script = textwrap.dedent(
            """
            import json

            from fastapi.testclient import TestClient

            from app.database import Base, engine
            from app.main import app

            Base.metadata.create_all(bind=engine)
            client = TestClient(app)

            elder_id = "elder_001"
            for index, heart_rate in enumerate([72, 76, 80]):
                response = client.post(
                    "/api/v2/observations",
                    json={
                        "observation_id": f"obs_vital_{index}",
                        "elder_id": elder_id,
                        "kind": "vital",
                        "source": "test",
                        "payload": {"heart_rate": heart_rate, "spo2": 96 - index},
                        "observed_at": f"2026-06-23T08:0{index}:00+08:00",
                    },
                )
                assert response.status_code == 200, response.text

            response = client.post(
                "/api/v2/behavior-segments",
                json={
                    "segment_id": "seg_bath_daily",
                    "elder_id": elder_id,
                    "segment_type": "bathroom_stay",
                    "start_at": "2026-06-23T09:00:00+08:00",
                    "end_at": "2026-06-23T09:03:00+08:00",
                    "duration_seconds": 180,
                    "room": "bathroom",
                    "features": {},
                    "status": "closed",
                },
            )
            assert response.status_code == 200, response.text

            response = client.post(
                "/api/v2/daily-health-summaries/generate",
                json={
                    "elder_id": elder_id,
                    "date": "2026-06-23",
                    "timezone": "Asia/Shanghai",
                    "use_cloud": False,
                    "generated_by": "unit_test",
                },
            )
            assert response.status_code == 200, response.text
            summary = response.json()["daily_health_summary"]
            stats = summary["local_stats"]

            assert summary["status"] == "local_ready", summary
            assert summary["risk_level"] == "P4", summary
            assert stats["vitals"]["heart_rate"]["count"] == 3, stats
            assert stats["vitals"]["heart_rate"]["avg"] == 76.0, stats
            assert stats["vitals"]["spo2"]["min"] == 94.0, stats
            assert stats["behavior"]["bathroom_visits"] == 1, stats
            assert stats["behavior"]["bathroom_stay_max_sec"] == 180, stats
            assert stats["data_quality"]["status"] == "sufficient", stats

            listed = client.get(f"/api/v2/daily-health-summaries?elder_id={elder_id}").json()
            assert listed["daily_health_summaries"][0]["summary_id"] == summary["summary_id"], listed
            print(json.dumps(summary, ensure_ascii=False))
            """
        )
        result = run_edge_script(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_background_mqtt_exposes_daily_summary_controls(self) -> None:
        backend = (ROOT / "Background_MQTT" / "backend.py").read_text(encoding="utf-8")
        html = (ROOT / "Background_MQTT" / "frontend" / "index.html").read_text(encoding="utf-8")

        self.assertIn("/api/daily-health-summary", backend)
        self.assertIn("/api/v2/daily-health-summaries/generate", backend)
        self.assertIn('{"type": "daily_health_summary"', backend)
        self.assertIn("daily-summary-local", html)
        self.assertIn("daily-summary-cloud", html)
        self.assertIn("daily-vitals", html)
        self.assertIn("renderDailyHealthSummary", html)
        self.assertIn("generateDailyHealthSummary(true)", html)


if __name__ == "__main__":
    unittest.main()
