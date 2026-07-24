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

from llm_common.db import fetch, fetchrow, close_pool, enqueue_pending, enqueue_registro_manual, get_catalogo_bodega, get_pending_status
from llm_common import nlu

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
    pending_alert: dict | None = None
    bodega_id: int | None = None


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
        "tools": "[]",
        "session_id": req.session_id or "default",
        "mode": "full",
    }
    if req.pending_alert:
        payload["pending_alert"] = req.pending_alert
    if req.bodega_id:
        payload["bodega_id"] = req.bodega_id

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


# =====================================================================
#  Sesiones de conteo
# =====================================================================

class IniciarSesionRequest(BaseModel):
    bodega_id: int
    iniciada_por: str = "anonimo"


class FinalizarSesionRequest(BaseModel):
    sesion_id: int


class RegistroManualRequest(BaseModel):
    sesion_id: int
    producto_id: int
    cantidad: float
    unidad: str


class RegistroVozRequest(BaseModel):
    sesion_id: int
    texto: str


@app.post("/api/sesion/iniciar")
async def iniciar_sesion(req: IniciarSesionRequest):
    row = await fetchrow(
        "INSERT INTO sesiones_conteo (bodega_id, iniciada_por) VALUES ($1, $2) RETURNING id, bodega_id, estado, creado_en",
        req.bodega_id, req.iniciada_por,
    )
    if not row:
        raise HTTPException(500, "No se pudo crear la sesion")

    total_productos = await fetchrow(
        "SELECT COUNT(*) AS total FROM productos WHERE bodega_id = $1",
        req.bodega_id,
    )
    return {
        "sesion_id": row["id"],
        "bodega_id": row["bodega_id"],
        "estado": row["estado"],
        "creado_en": row["creado_en"].isoformat() if row.get("creado_en") else None,
        "total_productos": total_productos["total"] if total_productos else 0,
    }


@app.post("/api/sesion/finalizar")
async def finalizar_sesion(req: FinalizarSesionRequest):
    sesion = await fetchrow(
        "SELECT id, bodega_id, estado FROM sesiones_conteo WHERE id = $1",
        req.sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    if sesion["estado"] != "activa":
        raise HTTPException(400, f"Sesion ya esta {sesion['estado']}")

    await fetch(
        "UPDATE sesiones_conteo SET estado = 'finalizada', finalizado_en = NOW() WHERE id = $1",
        req.sesion_id,
    )

    stats = await fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'ACEPTADA') AS aceptados,
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'SOSPECHOSA') AS alertas,
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'PENDIENTE') AS pendientes,
            COUNT(*) AS total_contados
        FROM registros_conteo rc
        WHERE rc.sesion_id = $1
        """,
        req.sesion_id,
    )

    total_prods = await fetchrow(
        "SELECT COUNT(*) AS total FROM productos WHERE bodega_id = $1",
        sesion["bodega_id"],
    )

    return {
        "sesion_id": req.sesion_id,
        "estado": "finalizada",
        "total_productos": total_prods["total"] if total_prods else 0,
        "contados": stats["total_contados"] if stats else 0,
        "aceptados": stats["aceptados"] if stats else 0,
        "alertas": stats["alertas"] if stats else 0,
        "pendientes_kalman": stats["pendientes"] if stats else 0,
    }


@app.get("/api/sesion/{sesion_id}/estado")
async def estado_sesion(sesion_id: int):
    sesion = await fetchrow(
        "SELECT id, bodega_id, estado, iniciada_por, creado_en, finalizado_en FROM sesiones_conteo WHERE id = $1",
        sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")

    stats = await fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'ACEPTADA') AS aceptados,
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'SOSPECHOSA') AS alertas,
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'PENDIENTE') AS pendientes,
            COUNT(*) AS total_contados
        FROM registros_conteo rc
        WHERE rc.sesion_id = $1
        """,
        sesion_id,
    )

    total_prods = await fetchrow(
        "SELECT COUNT(*) AS total FROM productos WHERE bodega_id = $1",
        sesion["bodega_id"],
    )

    return {
        "sesion_id": sesion["id"],
        "bodega_id": sesion["bodega_id"],
        "estado": sesion["estado"],
        "iniciada_por": sesion["iniciada_por"],
        "creado_en": sesion["creado_en"].isoformat() if sesion.get("creado_en") else None,
        "total_productos": total_prods["total"] if total_prods else 0,
        "contados": stats["total_contados"] if stats else 0,
        "aceptados": stats["aceptados"] if stats else 0,
        "alertas": stats["alertas"] if stats else 0,
        "pendientes": (total_prods["total"] if total_prods else 0) - (stats["total_contados"] if stats else 0),
    }


# =====================================================================
#  Registro de conteo (manual y por voz)
# =====================================================================

@app.post("/api/sesion/registrar-manual")
async def registrar_manual(req: RegistroManualRequest):
    sesion = await fetchrow(
        "SELECT id, bodega_id, estado FROM sesiones_conteo WHERE id = $1",
        req.sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    if sesion["estado"] != "activa":
        raise HTTPException(400, f"Sesion {sesion['estado']}")

    try:
        pending_id = await enqueue_registro_manual(
            session_id=str(req.sesion_id),
            producto_id=req.producto_id,
            cantidad=req.cantidad,
            unidad=req.unidad,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "success": True,
        "pending_id": pending_id,
        "message": "Registro encolado. El worker Kalman lo evaluara.",
    }


@app.post("/api/sesion/registrar-voz")
async def registrar_voz(req: RegistroVozRequest):
    sesion = await fetchrow(
        "SELECT id, bodega_id, estado FROM sesiones_conteo WHERE id = $1",
        req.sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    if sesion["estado"] != "activa":
        raise HTTPException(400, f"Sesion {sesion['estado']}")

    # 1. Fast path: regex para comandos de conteo
    conteo = nlu.parse_conteo_rapido(req.texto)
    if conteo:
        from llm_common.fuzzy_search import fuzzy_match_product
        candidatos = await fetch(
            "SELECT id, nombre, unidad FROM productos WHERE bodega_id = $1",
            sesion["bodega_id"],
        )
        match = fuzzy_match_product(conteo["producto"], candidatos)
        if match:
            cantidad_normalizada, unidad_final = nlu.normalize_unidad(
                conteo["cantidad"],
                conteo.get("unidad"),
                match["unidad"],
            )
            try:
                pending_id = await enqueue_registro_manual(
                    session_id=str(req.sesion_id),
                    producto_id=match["id"],
                    cantidad=cantidad_normalizada,
                    unidad=unidad_final,
                )
            except ValueError as e:
                raise HTTPException(400, str(e))

            return {
                "success": True,
                "pending_id": pending_id,
                "via": "regex_fastpath",
                "producto": match["nombre"],
                "cantidad": cantidad_normalizada,
                "unidad": unidad_final,
                "message": "Registro encolado via fast path.",
            }

    # 2. Fallback: LLM service
    async with httpx.AsyncClient(timeout=60) as client:
        payload: dict[str, Any] = {
            "query": req.texto,
            "tools": "[]",
            "session_id": str(req.sesion_id),
            "mode": "full",
            "bodega_id": sesion["bodega_id"],
        }
        r = await client.post(f"{_llm_url()}/infer", json=payload)

    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)

    data = r.json()
    return {
        "success": True,
        "via": "llm",
        "tool_calls": data.get("tool_calls", []),
        "pending": data.get("pending", []),
        "raw_output": data.get("raw_output", ""),
    }


# =====================================================================
#  Catalogo
# =====================================================================

@app.get("/api/catalogo/bodega/{bodega_id}")
async def catalogo_bodega(
    bodega_id: int,
    q: str | None = None,
    solo_pendientes: bool = False,
    sesion_id: int | None = None,
):
    return await get_catalogo_bodega(bodega_id, q=q, solo_pendientes=solo_pendientes, sesion_id=sesion_id)


# =====================================================================
#  Reportes
# =====================================================================

@app.get("/api/reporte/diferencias/{sesion_id}")
async def reporte_diferencias(sesion_id: int):
    sesion = await fetchrow(
        "SELECT id, bodega_id FROM sesiones_conteo WHERE id = $1",
        sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")

    rows = await fetch(
        """
        SELECT
            p.nombre,
            p.unidad,
            p.codigo_articulo,
            rc.stock_sistema,
            rc.cantidad_normalizada AS stock_contado,
            (rc.cantidad_normalizada - rc.stock_sistema) AS diferencia,
            rc.decision_kalman
        FROM registros_conteo rc
        JOIN productos p ON p.id = rc.producto_id
        WHERE rc.sesion_id = $1
        ORDER BY ABS(rc.cantidad_normalizada - rc.stock_sistema) DESC
        """,
        sesion_id,
    )

    # Productos NO contados
    pendientes = await fetch(
        """
        SELECT
            p.nombre,
            p.unidad,
            p.codigo_articulo,
            p.stock_actual AS stock_sistema,
            NULL::FLOAT AS stock_contado,
            NULL::FLOAT AS diferencia,
            'no_contado' AS decision_kalman
        FROM productos p
        WHERE p.bodega_id = $1
        AND p.id NOT IN (
            SELECT producto_id FROM registros_conteo WHERE sesion_id = $2
        )
        ORDER BY p.nombre
        """,
        sesion["bodega_id"],
        sesion_id,
    )

    return {
        "sesion_id": sesion_id,
        "contados": rows,
        "no_contados": pendientes,
        "total_contados": len(rows),
        "total_pendientes": len(pendientes),
    }


@app.get("/api/reporte/sospechosos/{sesion_id}")
async def reporte_sospechosos(sesion_id: int):
    rows = await fetch(
        """
        SELECT
            p.nombre,
            p.unidad,
            rc.cantidad_normalizada AS cantidad_contada,
            rc.stock_sistema,
            (rc.cantidad_normalizada - rc.stock_sistema) AS diferencia,
            pe.residual,
            pe.umbral,
            pe.decision,
            pe.created_at
        FROM registros_conteo rc
        JOIN productos p ON p.id = rc.producto_id
        JOIN pending_evaluations pe ON pe.id = rc.pending_id
        WHERE rc.sesion_id = $1 AND rc.decision_kalman = 'SOSPECHOSA'
        ORDER BY ABS(pe.residual) DESC
        """,
        sesion_id,
    )
    return {"sesion_id": sesion_id, "sospechosos": rows, "total": len(rows)}


@app.get("/api/pending/{pending_id}")
async def pending_status(pending_id: int):
    row = await get_pending_status(pending_id)
    if not row:
        raise HTTPException(404, "pending not found")
    return row


def main():
    import uvicorn
    port = int(os.getenv("PORT", "8200"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
