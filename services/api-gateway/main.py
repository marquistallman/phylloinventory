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
KOKORO_URL = os.getenv("KOKORO_URL", "http://kokoro-service:8205")
ELEVENLABS_URL = os.getenv("ELEVENLABS_URL", "http://elevenlabs-service:8206")


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
        for name, url in [("needle", NEEDLE_URL), ("openrouter", OPENROUTER_URL), ("voice", VOICE_URL), ("kalman", KALMAN_URL), ("kokoro", KOKORO_URL), ("elevenlabs", ELEVENLABS_URL)]:
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


@app.get("/catalog")
async def catalog(bodega_id: int | None = None):
    """Catalogo de productos (1 fila por producto abstracto).

    Sin bodega_id: vista global (lo que muestra el CLI por defecto).
    Con bodega_id: igual pero acotado a los productos que tienen stock en
    esa bodega (util para sugerir candidatos a contar).
    """
    if bodega_id is not None:
        return await fetch(
            """
            SELECT DISTINCT
                pc.id, pc.nombre, pc.codigo_articulo, pc.unidad,
                pc.q_proceso, pc.r_medicion, pc.umbral_sigma
            FROM productos_catalogo pc
            JOIN stock s ON s.producto_id = pc.id
            WHERE s.bodega_id = $1
            ORDER BY pc.nombre
            """,
            bodega_id,
        )
    return await fetch(
        """SELECT id, nombre, codigo_articulo, unidad,
                  q_proceso, r_medicion, umbral_sigma
           FROM productos_catalogo
           ORDER BY nombre"""
    )


@app.get("/inventory")
async def inventory(
    producto: str | None = None,
    bodega_id: int | None = None,
):
    """Stock por (producto, bodega). Sin filtros -> catalogo (no 1400 filas).

    Comportamiento:
      sin params           -> 1 fila por producto (sin stock per-bodega)
      ?producto=X          -> stock de X en todas las bodegas donde exista
      ?bodega_id=Y         -> stock de todos los productos en bodega Y
      ?producto=X&bodega=Y -> fila unica
    """
    if producto and bodega_id is not None:
        row = await fetchrow(
            """SELECT peb.nombre, peb.bodega_id, b.nombre AS bodega,
                      peb.unidad, peb.stock_actual, peb.media_kalman, peb.varianza_kalman
               FROM productos_en_bodega peb
               JOIN bodegas b ON b.id = peb.bodega_id
               WHERE peb.nombre = $1 AND peb.bodega_id = $2""",
            producto, bodega_id,
        )
        if not row:
            raise HTTPException(404, f"producto '{producto}' no encontrado en bodega {bodega_id}")
        return row

    if producto:
        rows = await fetch(
            """SELECT peb.nombre, peb.bodega_id, b.nombre AS bodega,
                      peb.unidad, peb.stock_actual, peb.media_kalman, peb.varianza_kalman
               FROM productos_en_bodega peb
               JOIN bodegas b ON b.id = peb.bodega_id
               WHERE peb.nombre = $1
               ORDER BY b.nombre""",
            producto,
        )
        if not rows:
            raise HTTPException(404, f"producto '{producto}' no encontrado")
        return rows

    if bodega_id is not None:
        return await fetch(
            """SELECT peb.nombre, peb.codigo_articulo, peb.unidad,
                      peb.stock_actual, peb.media_kalman, peb.varianza_kalman
               FROM productos_en_bodega peb
               WHERE peb.bodega_id = $1
               ORDER BY peb.nombre""",
            bodega_id,
        )

    #  Sin filtros: devuelvo el catalogo (1 fila por producto, sin duplicar
    #  por bodega). Esto arregla el "el CLI carga 1400 filas" — antes
    #  haciamos SELECT * FROM productos sin filtro y explotaba con 48 bodegas.
    return await fetch(
        """SELECT id, nombre, codigo_articulo, unidad
           FROM productos_catalogo
           ORDER BY nombre"""
    )


@app.get("/sospechosos")
async def sospechosos(producto: str | None = None):
    return await fetch("SELECT * FROM investigar_sospechosos($1)", producto)


@app.get("/api/bodegas")
async def list_bodegas(q: str | None = None):
    """Lista todas las bodegas. Opcional: ?q= filtra por nombre (ILIKE)."""
    if q:
        return await fetch(
            "SELECT id, nombre FROM bodegas WHERE nombre ILIKE $1 ORDER BY nombre",
            f"%{q}%",
        )
    return await fetch("SELECT id, nombre FROM bodegas ORDER BY nombre")


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
        "SELECT COUNT(*) AS total FROM productos_en_bodega WHERE bodega_id = $1",
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
        "SELECT COUNT(*) AS total FROM productos_en_bodega WHERE bodega_id = $1",
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
        "SELECT COUNT(*) AS total FROM productos_en_bodega WHERE bodega_id = $1",
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

    bodega_id = sesion["bodega_id"]

    # ── Fast path 1: escritura con direccion (agregar/remover) ──
    escritura = nlu.parse_escritura_rapida(req.texto)
    if escritura:
        from llm_common.fuzzy_search import fuzzy_match_product

        candidatos = await fetch(
            "SELECT id, nombre, unidad FROM productos_en_bodega WHERE bodega_id = $1",
            bodega_id,
        )
        match = fuzzy_match_product(escritura["producto"], candidatos)
        if match:
            cantidad_normalizada, unidad_final = nlu.normalize_unidad(
                escritura["cantidad"],
                escritura.get("unidad"),
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
                "via": "regex_escritura",
                "tool": escritura["tool"],
                "producto": match["nombre"],
                "cantidad": cantidad_normalizada,
                "unidad": unidad_final,
                "message": f"Registro encolado via fast path ({escritura['tool']}).",
            }

    # ── Fast path 2: lectura (consultas de inventario) ──
    lectura = nlu.parse_lectura_rapida(req.texto)
    if lectura:
        producto = lectura.get("producto")
        if producto:
            from llm_common.fuzzy_search import fuzzy_match_product

            candidatos = await fetch(
                "SELECT id, nombre, unidad, stock_actual FROM productos_en_bodega WHERE bodega_id = $1",
                bodega_id,
            )
            match = fuzzy_match_product(producto, candidatos, threshold=70)
            if match:
                return {
                    "success": True,
                    "via": "regex_lectura",
                    "tool": "consultar_inventario",
                    "producto": match["nombre"],
                    "stock_actual": match["stock_actual"],
                    "unidad": match["unidad"],
                    "message": f"Stock de {match['nombre']}: {match['stock_actual']} {match['unidad']}",
                }

        # Consulta general (sin producto especifico) — devolver catalogo entero
        rows = await get_catalogo_bodega(bodega_id, sesion_id=req.sesion_id)
        return {
            "success": True,
            "via": "regex_lectura",
            "tool": "consultar_inventario",
            "catalogo": rows,
            "total": len(rows),
            "message": f"Catalogo de la bodega ({len(rows)} productos)",
        }

    # ── Fast path 3: investigacion/auditoria ──
    investigacion = nlu.parse_investigacion_rapida(req.texto)
    if investigacion:
        sospechosos = await fetch("SELECT * FROM investigar_sospechosos($1)", None)
        return {
            "success": True,
            "via": "regex_investigacion",
            "tool": "investigar_sospechosos",
            "sospechosos": sospechosos,
            "total": len(sospechosos),
            "message": f"Auditoria: {len(sospechosos)} movimientos sospechosos encontrados",
        }

    # ── Fallback: LLM service ──
    async with httpx.AsyncClient(timeout=60) as client:
        payload: dict[str, Any] = {
            "query": req.texto,
            "tools": "[]",
            "session_id": str(req.sesion_id),
            "mode": "full",
            "bodega_id": bodega_id,
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
            peb.unidad,
            p.codigo_articulo,
            rc.stock_sistema,
            rc.cantidad_normalizada AS stock_contado,
            (rc.cantidad_normalizada - rc.stock_sistema) AS diferencia,
            rc.decision_kalman
        FROM registros_conteo rc
        JOIN productos p            ON p.id = rc.producto_id
        JOIN productos_en_bodega peb ON peb.producto_id = p.id AND peb.bodega_id = rc.bodega_id
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
            peb.unidad,
            p.codigo_articulo,
            peb.stock_actual AS stock_sistema,
            NULL::FLOAT AS stock_contado,
            NULL::FLOAT AS diferencia,
            'no_contado' AS decision_kalman
        FROM productos_en_bodega peb
        JOIN productos p ON p.id = peb.producto_id
        WHERE peb.bodega_id = $1
        AND NOT EXISTS (
            SELECT 1 FROM registros_conteo rc
            WHERE rc.producto_id = peb.producto_id
              AND rc.bodega_id   = peb.bodega_id
              AND rc.sesion_id   = $2
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
            peb.unidad,
            rc.cantidad_normalizada AS cantidad_contada,
            rc.stock_sistema,
            (rc.cantidad_normalizada - rc.stock_sistema) AS diferencia,
            pe.residual,
            pe.umbral,
            pe.decision,
            pe.created_at
        FROM registros_conteo rc
        JOIN productos p            ON p.id = rc.producto_id
        JOIN productos_en_bodega peb ON peb.producto_id = p.id AND peb.bodega_id = rc.bodega_id
        JOIN pending_evaluations pe  ON pe.id = rc.pending_id
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


# =====================================================================
#  Manager — Estadisticas
# =====================================================================

@app.get("/api/stats/general")
async def stats_general():
    """Dashboard general: totales, sesiones activas, sospechosos pendientes."""
    total_bodegas = await fetchrow("SELECT COUNT(*)::int AS n FROM bodegas WHERE nombre != 'bodega_default'")
    total_productos = await fetchrow("SELECT COUNT(*)::int AS n FROM productos")
    total_stock = await fetchrow(
        "SELECT COUNT(*)::int AS n FROM stock WHERE stock_actual != 0"
    )
    sesiones_activas = await fetchrow(
        "SELECT COUNT(*)::int AS n FROM sesiones_conteo WHERE estado = 'activa'"
    )
    sospechosos_pendientes = await fetchrow(
        "SELECT COUNT(*)::int AS n FROM pending_evaluations WHERE status = 'SOSPECHOSA'"
    )
    movimientos_hoy = await fetchrow(
        "SELECT COUNT(*)::int AS n FROM inventario_movimientos WHERE creado_en::date = CURRENT_DATE"
    )
    ultimas_5_sesiones = await fetch(
        """
        SELECT sc.id, b.nombre AS bodega, sc.estado, sc.iniciada_por,
               sc.creado_en, sc.finalizado_en
        FROM sesiones_conteo sc
        JOIN bodegas b ON b.id = sc.bodega_id
        ORDER BY sc.creado_en DESC
        LIMIT 5
        """
    )

    return {
        "bodegas": total_bodegas["n"] if total_bodegas else 0,
        "productos_catalogo": total_productos["n"] if total_productos else 0,
        "productos_con_stock": total_stock["n"] if total_stock else 0,
        "sesiones_activas": sesiones_activas["n"] if sesiones_activas else 0,
        "sospechosos_pendientes": sospechosos_pendientes["n"] if sospechosos_pendientes else 0,
        "movimientos_hoy": movimientos_hoy["n"] if movimientos_hoy else 0,
        "ultimas_sesiones": ultimas_5_sesiones,
    }


@app.get("/api/stats/bodega/{bodega_id}")
async def stats_bodega(bodega_id: int):
    """Estadisticas de una bodega especifica."""
    bodega = await fetchrow("SELECT id, nombre FROM bodegas WHERE id = $1", bodega_id)
    if not bodega:
        raise HTTPException(404, "Bodega no encontrada")

    total_prods = await fetchrow(
        "SELECT COUNT(*)::int AS n FROM stock WHERE bodega_id = $1", bodega_id
    )
    con_stock = await fetchrow(
        "SELECT COUNT(*)::int AS n FROM stock WHERE bodega_id = $1 AND stock_actual > 0", bodega_id
    )
    stock_negativo = await fetchrow(
        "SELECT COUNT(*)::int AS n FROM stock WHERE bodega_id = $1 AND stock_actual < 0", bodega_id
    )
    sesiones = await fetchrow(
        "SELECT COUNT(*)::int AS n FROM sesiones_conteo WHERE bodega_id = $1", bodega_id
    )
    ultima_sesion = await fetchrow(
        """
        SELECT id, estado, iniciada_por, creado_en, finalizado_en
        FROM sesiones_conteo
        WHERE bodega_id = $1
        ORDER BY creado_en DESC
        LIMIT 1
        """,
        bodega_id,
    )

    return {
        "bodega": bodega["nombre"],
        "total_productos": total_prods["n"] if total_prods else 0,
        "con_stock_positivo": con_stock["n"] if con_stock else 0,
        "con_stock_negativo": stock_negativo["n"] if stock_negativo else 0,
        "total_sesiones": sesiones["n"] if sesiones else 0,
        "ultima_sesion": ultima_sesion,
    }


@app.get("/api/stats/sesiones")
async def stats_sesiones(limit: int = 10):
    """Resumen de las ultimas N sesiones con metricas."""
    rows = await fetch(
        """
        SELECT
            sc.id, b.nombre AS bodega, sc.estado, sc.iniciada_por,
            sc.creado_en, sc.finalizado_en,
            COUNT(rc.id) AS productos_contados,
            COUNT(rc.id) FILTER (WHERE rc.decision_kalman = 'SOSPECHOSA') AS alertas_kalman,
            COUNT(rc.id) FILTER (WHERE rc.decision_kalman = 'ACEPTADA') AS aceptados
        FROM sesiones_conteo sc
        JOIN bodegas b ON b.id = sc.bodega_id
        LEFT JOIN registros_conteo rc ON rc.sesion_id = sc.id
        GROUP BY sc.id, b.nombre
        ORDER BY sc.creado_en DESC
        LIMIT $1
        """,
        limit,
    )
    return {"sesiones": rows, "total": len(rows)}


def main():
    import uvicorn
    port = int(os.getenv("PORT", "8200"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
