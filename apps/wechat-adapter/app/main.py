from __future__ import annotations

import logging
import os

import httpx
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mock WeChat Adapter")
GUARDIAN_CORE_URL = os.getenv("GUARDIAN_CORE_URL", "http://localhost:8000")


@app.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True, "service": "wechat-adapter", "guardian_core_url": GUARDIAN_CORE_URL}


@app.post("/notify")
async def notify(payload: dict) -> dict:
    logger.warning("[MOCK WECHAT NOTIFY] %s", payload)
    return {"ok": True}


@app.post("/family/message")
async def family_message(payload: dict) -> dict:
    text = payload.get("text") or payload.get("message") or "老人现在怎么样？"
    logger.info("[MOCK FAMILY MESSAGE] %s", text)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{GUARDIAN_CORE_URL}/api/wechat/message", json={"text": text})
        response.raise_for_status()
        return response.json()

