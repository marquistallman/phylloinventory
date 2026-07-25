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


async def get_inventory(producto: str | None = None, bodega_id: int | None = None) -> Any:
    """Sin filtros -> catalogo (1 fila por producto, ~30 filas).

    Con bodega_id -> stock por bodega. Con producto -> stock del producto.
    """
    params: dict[str, Any] = {}
    if producto:
        params["producto"] = producto
    if bodega_id is not None:
        params["bodega_id"] = bodega_id
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{GATEWAY_URL}/inventory", params=params)
        r.raise_for_status()
        return r.json()


async def get_catalog(bodega_id: int | None = None) -> list[dict]:
    """Catalogo abstracto de productos (sin repeticion por bodega)."""
    params = {"bodega_id": bodega_id} if bodega_id is not None else {}
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{GATEWAY_URL}/catalog", params=params)
        r.raise_for_status()
        return r.json()


async def get_sospechosos(producto: str | None = None) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{GATEWAY_URL}/sospechosos", params={"producto": producto} if producto else {})
        r.raise_for_status()
        return r.json()


async def list_bodegas() -> list[dict]:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{GATEWAY_URL}/api/bodegas")
        r.raise_for_status()
        return r.json()


async def find_bodega(query: str) -> int | None:
    """Resuelve nombre -> id de bodega (fuzzy). None si no hay match."""
    try:
        bodegas = await list_bodegas()
    except Exception:
        return None
    if not bodegas:
        return None
    q = query.lower().strip()
    # exacto primero
    for b in bodegas:
        if b["nombre"] == q:
            return int(b["id"])
    # prefijo
    for b in bodegas:
        if b["nombre"].startswith(q):
            return int(b["id"])
    # fuzzy (levenshtein simple)
    try:
        from llm_common.fuzzy_search import fuzzy_match
    except Exception:
        return None
    m = fuzzy_match(query, [{"id": b["id"], "nombre": b["nombre"]} for b in bodegas])
    return int(m["id"]) if m else None
