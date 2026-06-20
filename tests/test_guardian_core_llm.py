from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GuardianCoreLlmConfigTests(unittest.TestCase):
    def test_core_llm_mock_overrides_shared_llm_mock(self) -> None:
        script = textwrap.dedent(
            f"""
            import asyncio
            import os
            import sys

            os.environ["CORE_LLM_MOCK"] = "true"
            os.environ["LLM_MOCK"] = "false"
            sys.path.insert(0, {str(ROOT / "packages" / "guardian-shared")!r})
            sys.path.insert(0, {str(ROOT / "apps" / "guardian-core")!r})

            from app.agent.llm_client import LLMClient
            from app.config import settings

            assert settings.llm_mock is True

            async def main():
                client = LLMClient()

                async def fail_if_called(context):
                    raise AssertionError("guardian-core should not call HTTP LLM when CORE_LLM_MOCK=true")

                client._call_openai_compatible = fail_if_called
                result = await client.analyze({{
                    "rule_result": {{
                        "risk_level": "P2",
                        "event_type": "long_static",
                        "risk_score": 0.6,
                        "summary": "test rule",
                    }}
                }})
                assert result["risk_level"] == "P2"

            asyncio.run(main())
            """
        )
        completed = subprocess.run([sys.executable, "-c", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)


if __name__ == "__main__":
    unittest.main()
