"""openrouter-service: proxy HTTP a OpenRouter con la misma interfaz /infer
que needle-service. Mismo modelo de respuesta, misma encolacion en
pending_evaluations.
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

#  Carga .env / .env.example ANTES de leer cualquier os.getenv().
from llm_common.env_loader import load_env
load_env()

from llm_common.db import (
    close_pool,
    enqueue_pending,
    get_pending_status,
    get_producto_nombres_bodega,
)
from llm_common.nlu import (
    build_alert_context,
    get_producto_nombres_from_candidates,
    normalize_args,
    normalize_producto,
    parse_confirmacion,
    parse_conteo_absoluto_rapido,
    parse_escritura_rapida,
)
from llm_common.schemas import TOOLS_OPENAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("openrouter-service")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE = os.getenv("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
# Default: DeepSeek V4 Flash — smart, tool calling solido, ~$0.09/M in.
# Alternativa gratis: google/gemma-4-31b-it:free
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
SYSTEM_PROMPT = os.getenv(
    "OPENROUTER_SYSTEM_PROMPT",
    """Eres un asistente de inventario en español rioplatense. Hablas natural y breve.

REGLAS OBLIGATORIAS (incumplir = error del sistema):

1. SI el usuario pide una accion de inventario (agregar, meter, sacar, quitar,
   consultar stock, cuanto hay, hay algo raro, sospechoso, confirmar, rechazar),
   DEBES llamar a la tool correspondiente. NO respondas solo con texto plano.

2. Tu salida SIEMPRE tiene que ser una o mas tool_calls para pedidos de
   inventario. La frase "listo, agrego 5 kilos de papa" sin tool_call NO
   cuenta como accion — el stock NO se modifica.

3. NO inventes numeros: cantidad y unidad vienen de lo que dijo el usuario.
   Si dijo "5 kilos" -> cantidad=5, unidad="kg" (o "Kilogram").

3.5. MUY IMPORTANTE — distingui "mover" de "contar":
   - "agrega/mete/suma 5 kilos de papa", "saca/quita 3 kilos" -> el usuario
     te dice un DELTA que hay que sumar o restar al stock actual. Usa
     agregar_inventario / remover_inventario.
   - "hay 5 kilos de papa", "tengo 10 unidades", "quedan 3 cajas", "contamos
     8 bolsas" -> el usuario te dice el TOTAL real que hay ahora mismo (un
     conteo fisico), NO un delta. Usa registrar_conteo con esa cantidad
     absoluta, aunque sea muy distinta del stock que el sistema tenia.

4. Despues de las tool_calls, podes agregar un content breve confirmando
   ("Listo", "Anotado", etc), pero las tools son lo importante.

5. Para saludos o preguntas que NO son de inventario (ej: "hola",
   "como te llamas"), responde brevemente sin tools.""",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY no configurada — el servicio fallara al inferir")
    yield
    await close_pool()


app = FastAPI(title="openrouter-service", version="1.0.0", lifespan=lifespan)


# =====================================================================
#  Schemas (mismos que needle-service)
# =====================================================================

class InferRequest(BaseModel):
    query: str
    tools: str
    session_id: str | None = None
    mode: str = "full"
    pending_alert: dict | None = None  # alerta Kalman activa en la sesion
    bodega_id: int | None = None


class ToolCall(BaseModel):
    name: str
    arguments: dict = {}


class PendingCall(BaseModel):
    pending_id: int
    tool_name: str
    arguments: dict


class InferResponse(BaseModel):
    tool_calls: list[ToolCall] = []
    raw_output: str = ""
    pending: list[PendingCall] = []
    mode: str = "full"


# =====================================================================
#  Llamada a OpenRouter
# =====================================================================

async def _call_openrouter(query: str) -> tuple[list[ToolCall], str]:
    if not OPENROUTER_API_KEY:
        raise HTTPException(503, "OPENROUTER_API_KEY not configured")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
                "tools": TOOLS_OPENAI,
                #  "required" fuerza al LLM a llamar al menos una tool. La idea
                #  es: si el usuario habla de inventario, LLAMA una tool (no
                #  responda solo "listo, agrego 5 kilos" sin ejecutar nada).
                #  Para "hola" el LLM va a tener que elegir la tool menos
                #  mala (probablemente investigar_sospechosos) — eso lo
                #  manejamos en el CLI como "no detecte ninguna accion util".
                "tool_choice": "required",
                "temperature": 0.1,
            },
        )
    if resp.status_code != 200:
        logger.error("openrouter %d: %s", resp.status_code, resp.text[:300])
        raise HTTPException(502, f"openrouter error {resp.status_code}")

    data = resp.json()
    msg = data["choices"][0]["message"]

    raw = msg.get("content", "") or json.dumps(msg, default=str)
    calls: list[ToolCall] = []

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCall(name=name, arguments=args))

    return calls, raw


# =====================================================================
#  Endpoints
# =====================================================================

@app.get("/health")
async def health():
    return {
        "status": "ok" if OPENROUTER_API_KEY else "no_api_key",
        "model": OPENROUTER_MODEL,
    }


@app.post("/infer", response_model=InferResponse)
async def infer(req: InferRequest):
    session_id = req.session_id or "default"
    t0 = time.time()

    #  Catalogo siempre (bodega o global): sin el, normalize_args no
    #  resuelve productos y toda escritura moria en el enqueue.
    candidatos = await get_producto_nombres_bodega(req.bodega_id)
    producto_nombres = get_producto_nombres_from_candidates(candidatos)

    alert_pid = 0
    if req.pending_alert:
        #  Con alerta activa: regex determinista primero; el modelo solo
        #  como respaldo y siempre con contexto de la alerta.
        alert_pid = int(req.pending_alert.get("pending_id")
                        or req.pending_alert.get("movimiento_id") or 0)
        conf = parse_confirmacion(req.query)
        if conf is not None:
            calls = [ToolCall(name="confirmar_movimiento",
                              arguments={"pending_id": alert_pid, "confirmar": conf})]
            raw = f"regex:confirmar={conf}"
        else:
            calls, raw = await _call_openrouter(build_alert_context(req.query, req.pending_alert))
    else:
        #  Conteo absoluto ("hay 3 papas") se prueba ANTES que
        #  escritura ("agrega 3 papas"): no son lo mismo, ver
        #  registrar_conteo vs agregar/remover_inventario.
        fp = parse_conteo_absoluto_rapido(req.query) or parse_escritura_rapida(req.query)
        fp_prod = normalize_producto(fp["producto"], producto_nombres) if fp else ""
        if fp and fp_prod:
            #  Escritura/conteo determinista: ni llamada a la API externa
            calls = [ToolCall(name=fp["tool"], arguments={
                "producto": fp_prod, "cantidad": fp["cantidad"], "unidad": fp["unidad"] or ""})]
            raw = f"regex:{fp['tool']}"
        else:
            calls, raw = await _call_openrouter(req.query)

    dt_ms = int((time.time() - t0) * 1000)
    logger.info("infer %dms session=%s calls=%s", dt_ms, session_id, [(c.name, c.arguments) for c in calls])

    normalized: list[ToolCall] = []
    pending: list[PendingCall] = []
    for call in calls:
        args = normalize_args(call.name, call.arguments, req.query, producto_nombres)
        if call.name == "confirmar_movimiento" and alert_pid > 0:
            args["pending_id"] = alert_pid  # el id real es el del estado, no del modelo
        normalized.append(ToolCall(name=call.name, arguments=args))
        try:
            if call.name in ("agregar_inventario", "remover_inventario", "registrar_conteo", "confirmar_movimiento"):
                pid = await enqueue_pending(
                    session_id=session_id,
                    tool_name=call.name,
                    arguments=args,
                    bodega_id=req.bodega_id,
                )
                pending.append(PendingCall(pending_id=pid, tool_name=call.name, arguments=args))
                logger.info("enqueued pending_id=%d tool=%s", pid, call.name)
            else:
                pending.append(PendingCall(pending_id=0, tool_name=call.name, arguments=args))
        except ValueError as e:
            logger.warning("enqueue validation: %s", e)
        except Exception as e:
            logger.exception("enqueue unexpected")
            raise HTTPException(500, f"queue error: {e}")

    return InferResponse(
        tool_calls=normalized,
        raw_output=raw,
        pending=pending,
        mode=req.mode,
    )


@app.get("/pending/{pending_id}")
async def get_pending(pending_id: int):
    row = await get_pending_status(pending_id)
    if not row:
        raise HTTPException(404, "pending not found")
    return row


def main():
    import uvicorn
    port = int(os.getenv("PORT", "8082"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
