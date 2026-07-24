"""api-gateway: punto de entrada unico para la CLI.

Responsabilidades:
- POST /query      -> rutea a needle-service o openrouter-service
                       segun LLM_BACKEND. Devuelve tool_calls + pending_ids.
- GET  /status/{id} -> consulta el estado de un pending_evaluation.
- GET  /inventory   -> consulta directa al DB (lectura).
- GET  /sospechosos -> auditoria (delegada al DB).
- GET  /health      -> ping.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from llm_common.db import fetch, fetchrow, close_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api-gateway")

LLM_BACKEND = os.getenv("LLM_BACKEND", "needle").lower()  # needle | openrouter
NEEDLE_URL = os.getenv("NEEDLE_URL", "http://needle-service:8081")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "http://openrouter-service:8082")
KALMAN_URL = os.getenv("KALMAN_URL", "http://kalman-worker:8300")
VOICE_URL = os.getenv("VOICE_URL", "http://voice-service:8100")


def _llm_url() -> str:
    if LLM_BACKEND == "openrouter":
        return OPENROUTER_URL
    return NEEDLE_URL


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("api-gateway up. LLM_BACKEND=%s -> %s", LLM_BACKEND, _llm_url())
    yield
    await close_pool()


app = FastAPI(title="api-gateway", version="2.0.0", lifespan=lifespan)


# =====================================================================
#  Schemas
# =====================================================================

class QueryRequest(BaseModel):
    text: str
    session_id: str | None = None
    pending_alert: dict | None = None  # para enriquecer el contexto si hay


class QueryResponse(BaseModel):
    backend: str
    tool_calls: list[dict] = []
    pending: list[dict] = []
    raw_output: str = ""


# =====================================================================
#  Endpoints
# =====================================================================

@app.get("/health")
async def health():
    """Ping al LLM backend + DB + voice + kalman."""
    out: dict[str, Any] = {"status": "ok", "backend": LLM_BACKEND, "services": {}}
    async with httpx.AsyncClient(timeout=3) as client:
        for name, url in [("needle", NEEDLE_URL), ("openrouter", OPENROUTER_URL), ("voice", VOICE_URL), ("kalman", KALMAN_URL)]:
            if name == "openrouter" and LLM_BACKEND != "openrouter":
                continue
            try:
                r = await client.get(f"{url}/health")
                out["services"][name] = r.json() if r.status_code == 200 else {"status": "down"}
            except Exception as e:
                out["services"][name] = {"status": "down", "error": str(e)}

    try:
        await fetchrow("SELECT 1 AS ok")
        out["db"] = "ok"
    except Exception as e:
        out["db"] = f"down: {e}"

    return out


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """Rutea al LLM service activo y devuelve tool_calls + pending_ids.

    La alerta pendiente viaja ESTRUCTURADA: cada LLM service decide como
    resolverla (regex determinista -> modelo). Aplanarla en el texto del
    query hacia que el pipeline generico disparara escrituras fantasma.
    """
    payload: dict[str, Any] = {
        "query": req.text,
        "tools": "[]",  # el LLM service usa su propio schema interno
        "session_id": req.session_id or "default",
        "mode": "full",
    }
    if req.pending_alert:
        payload["pending_alert"] = req.pending_alert

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{_llm_url()}/infer", json=payload)
    except httpx.RequestError as e:
        raise HTTPException(503, f"LLM backend unreachable: {e}")

    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)

    data = r.json()
    return QueryResponse(
        backend=LLM_BACKEND,
        tool_calls=data.get("tool_calls", []),
        pending=data.get("pending", []),
        raw_output=data.get("raw_output", ""),
    )


@app.get("/status/{pending_id}")
async def status(pending_id: int):
    """Devuelve el estado actual de un pending_evaluation."""
    row = await fetchrow(
        """
        SELECT id, session_id, tool_name, status, decision,
               residual, umbral, movimiento_id, payload, created_at, resolved_at
        FROM pending_evaluations
        WHERE id = $1
        """,
        pending_id,
    )
    if not row:
        raise HTTPException(404, "pending not found")
    return row


@app.get("/inventory")
async def inventory(producto: str | None = None):
    if producto:
        row = await fetchrow(
            "SELECT nombre, stock_actual, media_kalman, varianza_kalman FROM productos WHERE nombre = $1",
            producto,
        )
        if not row:
            raise HTTPException(404, f"producto '{producto}' no encontrado")
        return row
    return await fetch(
        "SELECT nombre, stock_actual, media_kalman, varianza_kalman FROM productos ORDER BY nombre"
    )


@app.get("/sospechosos")
async def sospechosos(producto: str | None = None):
    return await fetch("SELECT * FROM investigar_sospechosos($1)", producto)


def main():
    import uvicorn
    port = int(os.getenv("PORT", "8200"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
