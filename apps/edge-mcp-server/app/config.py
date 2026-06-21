from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT_DIR / "configs"
DATA_DIR = ROOT_DIR / "data"


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _normalize_sqlite_url(raw: str) -> str:
    if raw.startswith("sqlite:///") and not raw.startswith("sqlite:////"):
        sqlite_path = raw.replace("sqlite:///", "", 1)
        if sqlite_path and not os.path.isabs(sqlite_path):
            absolute = (ROOT_DIR / sqlite_path).resolve()
            absolute.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{absolute}"
    return raw


@dataclass(frozen=True)
class Settings:
    app_env: str
    elder_id: str
    mqtt_host: str
    mqtt_port: int
    database_url: str
    orchestrator_url: str | None
    simulate_device_when_mqtt_unavailable: bool
    auto_personal_baseline_enabled: bool
    auto_candidate_enabled: bool
    config_dir: Path
    data_dir: Path


def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    default_db = f"sqlite:///{DATA_DIR / 'guardian.db'}"
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        elder_id=os.getenv("ELDER_ID", "elder_001"),
        mqtt_host=os.getenv("MQTT_HOST", "localhost"),
        mqtt_port=_env_int("MQTT_PORT", 1883),
        database_url=_normalize_sqlite_url(os.getenv("DATABASE_URL", default_db)),
        orchestrator_url=os.getenv("ORCHESTRATOR_URL") or None,
        simulate_device_when_mqtt_unavailable=_env_bool("SIMULATE_DEVICE_WHEN_MQTT_UNAVAILABLE", True),
        auto_personal_baseline_enabled=_env_bool("AUTO_PERSONAL_BASELINE_ENABLED", False),
        auto_candidate_enabled=_env_bool("AUTO_CANDIDATE_ENABLED", True),
        config_dir=CONFIG_DIR,
        data_dir=DATA_DIR,
    )


settings = get_settings()
