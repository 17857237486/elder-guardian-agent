from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from guardian_shared.enums import EventType
from guardian_shared.v2 import ActionCommandV2

from app.config import settings


class DevicePolicy:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.config_dir / "device_policy.yaml")
        self.policy = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def check(self, command: ActionCommandV2, *, event_type: str | None) -> tuple[bool, str]:
        key = f"{command.room}/{command.device}"
        device_rule = self.policy.get("devices", {}).get(key)
        if device_rule is None:
            return False, f"设备未在策略中登记: {key}"
        if str(command.action) not in set(device_rule.get("allowed_actions", [])):
            return False, f"设备 {key} 不允许动作 {command.action}"
        if event_type == EventType.GAS_LEAK.value:
            constraint = self.policy.get("event_constraints", {}).get(EventType.GAS_LEAK.value, {})
            allowed = set(constraint.get("allowed_devices", []))
            denied = set(constraint.get("denied_devices", []))
            if key in denied:
                return False, f"燃气泄漏场景禁止控制 {key}"
            if allowed and key not in allowed:
                return False, f"燃气泄漏场景不允许控制 {key}"
        return True, "allowed"

