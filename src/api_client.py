"""Cliente asincrono para el api-gateway. Lo usa src/cli.py."""
import asyncio
import os
import time
from typing import Any

import httpx

GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://127.0.0.1:8200")
VOICE_WS_URL = os.getenv("VOICE_WS_URL", "ws://127.0.0.1:8100/ws/transcribe")

POLL_TIMEOUT_S = float(os.getenv("CLI_POLL_TIMEOUT", "15"))
POLL_INTERVAL_S = float(os.getenv("CLI_POLL_INTERVAL", "0.2"))


async def health() -> dict:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{GATEWAY_URL}/health")
        r.raise_for_status()
        return r.json()


async def query(text: str, session_id: str, pending_alert: dict | None = None) -> dict:
    payload: dict[str, Any] = {"text": text, "session_id": session_id}
    if pending_alert:
        payload["pending_alert"] = pending_alert
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{GATEWAY_URL}/query", json=payload)
        r.raise_for_status()
        return r.json()


async def get_status(pending_id: int) -> dict:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{GATEWAY_URL}/status/{pending_id}")
        r.raise_for_status()
        return r.json()


async def poll_until_resolved(pending_id: int, timeout_s: float = POLL_TIMEOUT_S) -> dict:
    """Espera hasta que el pending deje de estar PENDING o se agote el timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            row = await get_status(pending_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {"status": "NOT_FOUND"}
            raise
        if row.get("status") not in ("PENDING", None):
            return row
        await asyncio.sleep(POLL_INTERVAL_S)
    return {"status": "TIMEOUT"}


async def get_inventory(producto: str | None = None) -> Any:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{GATEWAY_URL}/inventory", params={"producto": producto} if producto else {})
        r.raise_for_status()
        return r.json()


async def get_sospechosos(producto: str | None = None) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{GATEWAY_URL}/sospechosos", params={"producto": producto} if producto else {})
        r.raise_for_status()
        return r.json()
