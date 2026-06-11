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


def get_settings() -> Settings:
    return Settings(
        elder_id=os.getenv("ELDER_ID", "elder_001"),
        edge_api_base=os.getenv("EDGE_API_BASE", "http://localhost:8010"),
        llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        llm_api_key=os.getenv("LLM_API_KEY", "change-me"),
        llm_model=os.getenv("LLM_MODEL", "qwen2.5:4b"),
        llm_mock=_env_bool("LLM_MOCK", True),
        llm_timeout_sec=_env_int("LLM_TIMEOUT_SEC", 120),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 512),
        llm_max_concurrent_workflows=max(1, _env_int("LLM_MAX_CONCURRENT_WORKFLOWS", 1)),
        llm_chain_timeout_sec=max(30, _env_int("LLM_CHAIN_TIMEOUT_SEC", 120)),
        llm_step_retries=max(0, _env_int("LLM_STEP_RETRIES", 1)),
    )


settings = get_settings()
