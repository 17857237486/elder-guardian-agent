from __future__ import annotations

import json
import re
from typing import Any

from guardian_shared.schemas import AgentDecision


class OutputParser:
    def parse(self, payload: dict[str, Any]) -> AgentDecision:
        if "raw" in payload:
            payload = json.loads(self._clean_raw_json(payload["raw"]))
        return AgentDecision(**payload)

    def _clean_raw_json(self, raw: str) -> str:
        text = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return text
