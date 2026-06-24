from __future__ import annotations

import os
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Settings:
    elder_id: str
    edge_api_base: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_mock: bool
    llm_timeout_sec: int
    llm_max_tokens: int
    llm_max_concurrent_workflows: int
    llm_chain_timeout_sec: int
    llm_step_retries: int
    snapshot_root: str
    vision_api_base: str
    vision_frame_wait_sec: int
    cloud_llm_enabled: bool
    cloud_llm_base_url: str
    cloud_llm_api_key: str
    cloud_llm_model: str
    cloud_llm_timeout_sec: int
    p3_environment_cooldown_sec: int
    p1_vital_cooldown_sec: int
    p0_gas_cooldown_sec: int
    p0_vital_cooldown_sec: int


def get_settings() -> Settings:
    return Settings(
        elder_id=os.getenv("ELDER_ID", "elder_001"),
        edge_api_base=os.getenv("EDGE_API_BASE", "http://localhost:8010"),
        llm_base_url=os.getenv("LLM_BASE_URL", "http://172.30.0.1:8001/v1"),
        llm_api_key=os.getenv("LLM_API_KEY", "local-rk3588"),
        llm_model=os.getenv("LLM_MODEL", "internvl3.5-4b-rk3588"),
        llm_mock=_env_bool("LLM_MOCK", False),
        llm_timeout_sec=_env_int("LLM_TIMEOUT_SEC", 120),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 512),
        llm_max_concurrent_workflows=max(1, _env_int("LLM_MAX_CONCURRENT_WORKFLOWS", 1)),
        llm_chain_timeout_sec=max(30, _env_int("LLM_CHAIN_TIMEOUT_SEC", 300)),
        llm_step_retries=max(0, _env_int("LLM_STEP_RETRIES", 1)),
        snapshot_root=os.getenv("SNAPSHOT_ROOT", "/app/data/snapshots"),
        vision_api_base=os.getenv("VISION_API_BASE", "http://vision-service:8101"),
        vision_frame_wait_sec=max(1, _env_int("VISION_FRAME_WAIT_SEC", 5)),
        cloud_llm_enabled=_env_bool("CLOUD_LLM_ENABLED", False),
        cloud_llm_base_url=os.getenv("CLOUD_LLM_BASE_URL", ""),
        cloud_llm_api_key=os.getenv("CLOUD_LLM_API_KEY", ""),
        cloud_llm_model=os.getenv("CLOUD_LLM_MODEL", ""),
        cloud_llm_timeout_sec=max(5, _env_int("CLOUD_LLM_TIMEOUT_SEC", 60)),
        p3_environment_cooldown_sec=max(0, _env_int("P3_ENVIRONMENT_COOLDOWN_SEC", 120)),
        p1_vital_cooldown_sec=max(0, _env_int("P1_VITAL_COOLDOWN_SEC", 120)),
        p0_gas_cooldown_sec=max(0, _env_int("P0_GAS_COOLDOWN_SEC", 120)),
        p0_vital_cooldown_sec=max(0, _env_int("P0_VITAL_COOLDOWN_SEC", 120)),
    )


settings = get_settings()
