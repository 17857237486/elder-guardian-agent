from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT_DIR / "configs"
DATA_DIR = ROOT_DIR / "data"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _normalize_sqlite_url(raw: str) -> str:
    if raw.startswith("sqlite:///") and not raw.startswith("sqlite:////"):
        sqlite_path = raw.replace("sqlite:///", "", 1)
        if sqlite_path and not os.path.isabs(sqlite_path):
            absolute = (APP_DIR / sqlite_path).resolve()
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
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_mock: bool
    llm_timeout_sec: int
    llm_max_tokens: int
    hmi_response_timeout_sec: int
    dashboard_ws_path: str
    wechat_mock: bool
    root_dir: Path
    config_dir: Path
    data_dir: Path


def get_settings() -> Settings:
    _load_dotenv(ROOT_DIR / ".env")
    _load_dotenv(ROOT_DIR / ".env.example")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    default_db = f"sqlite:///{DATA_DIR / 'guardian.db'}"
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        elder_id=os.getenv("ELDER_ID", "elder_001"),
        mqtt_host=os.getenv("MQTT_HOST", "localhost"),
        mqtt_port=_env_int("MQTT_PORT", 1883),
        database_url=_normalize_sqlite_url(os.getenv("DATABASE_URL", default_db)),
        llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        llm_api_key=os.getenv("LLM_API_KEY", "change-me"),
        llm_model=os.getenv("LLM_MODEL", "qwen2.5:7b"),
        llm_mock=_env_bool("CORE_LLM_MOCK", _env_bool("LLM_MOCK", True)),
        llm_timeout_sec=_env_int("LLM_TIMEOUT_SEC", 120),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 512),
        hmi_response_timeout_sec=_env_int("HMI_RESPONSE_TIMEOUT_SEC", 30),
        dashboard_ws_path=os.getenv("DASHBOARD_WS_PATH", "/ws/dashboard"),
        wechat_mock=_env_bool("WECHAT_MOCK", True),
        root_dir=ROOT_DIR,
        config_dir=CONFIG_DIR,
        data_dir=DATA_DIR,
    )


settings = get_settings()
