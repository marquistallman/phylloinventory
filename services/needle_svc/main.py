"""needle-service: sirve el modelo Needle (26M) y encola tool_calls en
pending_evaluations. Interfaz /infer identica a openrouter-service.

Multi-worker async: uvicorn con --workers N (procesos) o asyncio
(concurrencia dentro del proceso). El Dockerfile default usa 1 proceso
con asyncio + semaforo para que varias requests puedan inferse
simultaneamente hasta donde el modelo lo permita.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

#  Carga .env / .env.example ANTES de leer cualquier os.getenv().
from llm_common.env_loader import load_env
load_env()

from llm_common.db import enqueue_pending, get_pending_status, close_pool, get_producto_nombres_bodega
from llm_common.nlu import (
    build_alert_context,
    extract_producto,
    normalize_args,
    normalize_producto,
    parse_confirmacion,
    parse_conteo_absoluto_rapido,
    parse_escritura_rapida,
    get_producto_nombres_from_candidates,
)
from llm_common.schemas import L1_TOOLS, L2_READ, L2_WRITE, L3_ARGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("needle-service")

#  Estado global del modelo (cargado en startup)
model_state: dict[str, Any] = {}

#  Semaforo para limitar inferencias concurrentes (evita OOM)
_infer_sem: asyncio.Semaphore | None = None
MAX_INFER = int(os.getenv("MAX_INFER_INFLIGHT", "4"))


# =====================================================================
#  Lifecycle
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _infer_sem
    _infer_sem = asyncio.Semaphore(MAX_INFER)

    checkpoint_path = os.getenv("CHECKPOINT_PATH", "/app/checkpoints/needle.pkl")
    if not os.path.exists(checkpoint_path):
        logger.info("Checkpoint no encontrado. Descargando de HuggingFace...")
        from huggingface_hub import hf_hub_download
        checkpoint_path = hf_hub_download(
            repo_id="Cactus-Compute/needle",
            filename="needle.pkl",
            local_dir=os.path.dirname(checkpoint_path),
        )
        logger.info("Descargado a %s", checkpoint_path)

    logger.info("Cargando Needle desde %s ...", checkpoint_path)
    from needle import (
        SimpleAttentionNetwork,
        get_tokenizer,
        load_checkpoint,
    )
    params, config = load_checkpoint(checkpoint_path)
    model = SimpleAttentionNetwork(config)
    tokenizer = get_tokenizer()
    model_state["model"] = model
    model_state["params"] = params
    model_state["tokenizer"] = tokenizer
    logger.info("Needle cargado.")

    yield

    await close_pool()


app = FastAPI(title="needle-service", version="2.0.0", lifespan=lifespan)


# =====================================================================
#  Schemas
# =====================================================================

class InferRequest(BaseModel):
    query: str
    tools: str
    session_id: str | None = None
    mode: str = "full"
    pending_alert: dict | None = None
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


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool


# =====================================================================
#  Tool call parsing (rescate de JSON truncado/duplicado)
# =====================================================================

def _parse_tool_calls(raw: str) -> list[ToolCall]:
    raw = (raw or "").strip()
    if not raw:
        return []

    objs: list[dict] = []
    try:
        parsed = json.loads(raw)
        objs = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for m in re.finditer(r"[\[{]", raw):
            try:
                obj, _ = decoder.raw_decode(raw, m.start())
            except json.JSONDecodeError:
                continue
            objs = obj if isinstance(obj, list) else [obj]
            break

    calls: list[ToolCall] = []
    seen: set[str] = set()
    for o in objs:
        if not isinstance(o, dict) or "name" not in o:
            continue
        args = o.get("arguments")
        if not isinstance(args, dict):
            args = {}
        key = json.dumps([o["name"], args], sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        calls.append(ToolCall(name=str(o["name"]), arguments=args))
    return calls


# =====================================================================
#  Pipeline L1 -> L2 -> L3 (mismo flujo que el agent.py original)
# =====================================================================

def _infer_raw(query: str, tools: list[dict]) -> tuple[list[ToolCall], str]:
    from needle import generate
    assert _infer_sem is not None
    result = generate(
        model_state["model"],
        model_state["params"],
        model_state["tokenizer"],
        query=query,
        tools=json.dumps(tools),
        stream=False,
    )
    raw = result if isinstance(result, str) else json.dumps(result, default=str)
    return _parse_tool_calls(raw), raw


def _is_suspicious(query: str, result: list[ToolCall], producto_nombres: set[str] | None = None) -> bool:
    if not result:
        return True
    tool_name = result[0].name
    has_p = extract_producto(query, producto_nombres) is not None
    has_n = bool(re.search(r"(\d+)", query))
    if has_p and has_n and tool_name in ("investigar_sospechosos", "consultar_inventario"):
        return True
    if has_p and not has_n and tool_name in ("agregar_inventario", "remover_inventario", "registrar_conteo"):
        return True
    return False


def _pipeline(query: str, l1_options: list[dict], full_raw: list[str], producto_nombres: set[str] | None = None) -> list[ToolCall] | None:
    c1, r1 = _infer_raw(query, l1_options)
    full_raw.append(r1)
    if not c1:
        return None

    l1_choice = c1[0].name
    full_raw.append(f"L1:{l1_choice}")

    l2_tools = {"leer_inventario": L2_READ, "modificar_inventario": L2_WRITE}.get(l1_choice, [])
    c2, r2 = _infer_raw(query, l2_tools)
    full_raw.append(r2)
    if not c2:
        return None

    l2_choice = c2[0].name
    full_raw.append(f"L2:{l2_choice}")

    schema = L3_ARGS.get(l2_choice)
    if schema:
        c3, r3 = _infer_raw(query, [schema])
        full_raw.append(r3)
        if c3:
            args = normalize_args(l2_choice, c3[0].arguments, query, producto_nombres)
            return [ToolCall(name=l2_choice, arguments=args)]

    return None


_L2_BY_L1 = {"leer_inventario": L2_READ, "modificar_inventario": L2_WRITE}
_READ_TOOLS = ("investigar_sospechosos", "consultar_inventario")
_WRITE_TOOLS = ("agregar_inventario", "remover_inventario", "registrar_conteo")


def _confirmacion_fast_path(query: str, alert: dict, producto_nombres: set[str] | None = None) -> tuple[list[ToolCall], str]:
    """Resolucion de alerta pendiente. NUNCA cae al pipeline generico:
    el contexto de la alerta dispararia escrituras fantasma.

    1) Regex determinista (un si/no no deberia depender de un modelo de 26M)
    2) Needle solo con el schema de confirmar_movimiento
    3) [] -> la CLI mantiene la alerta viva y re-pregunta
    """
    pid = int(alert.get("pending_id") or alert.get("movimiento_id") or 0)
    raw: list[str] = []

    conf = parse_confirmacion(query)
    if conf is not None:
        raw.append(f"regex:confirmar={conf}")
        return [ToolCall(name="confirmar_movimiento",
                         arguments={"pending_id": pid, "confirmar": conf})], " | ".join(raw)

    schema = L3_ARGS.get("confirmar_movimiento")
    if schema:
        ctx = build_alert_context(query, alert)
        c3, r3 = _infer_raw(ctx, [schema])
        raw.append(r3)
        for tc in c3:
            if tc.name == "confirmar_movimiento":
                args = normalize_args("confirmar_movimiento", tc.arguments, ctx)
                if pid > 0:
                    args["pending_id"] = pid  # el id real es el del estado, no del modelo
                return [ToolCall(name="confirmar_movimiento", arguments=args)], " | ".join(raw)

    return [], " | ".join(raw)


def _regex_write_fast_path(query: str, producto_nombres: set[str]) -> ToolCall | None:
    """Escritura determinista sin modelo ("añade 5 kilos de papa").

    Solo dispara si el producto resuelve contra el catalogo; si no, se
    deja que el pipeline del modelo lo intente.
    """
    fp = parse_escritura_rapida(query)
    if not fp:
        return None
    prod = normalize_producto(fp["producto"], producto_nombres)
    if not prod:
        return None
    return ToolCall(name=fp["tool"], arguments={
        "producto": prod,
        "cantidad": fp["cantidad"],
        "unidad": fp["unidad"] or "",
    })


def _regex_conteo_fast_path(query: str, producto_nombres: set[str]) -> ToolCall | None:
    """Conteo ABSOLUTO determinista sin modelo ("hay 3 kilos de papa").

    Se prueba ANTES que _regex_write_fast_path: "hay 3 papas" no es un
    delta a sumar, es el total real ahora mismo. Mismo criterio: solo
    dispara si el producto resuelve contra el catalogo.
    """
    fp = parse_conteo_absoluto_rapido(query)
    if not fp:
        return None
    prod = normalize_producto(fp["producto"], producto_nombres)
    if not prod:
        return None
    return ToolCall(name=fp["tool"], arguments={
        "producto": prod,
        "cantidad": fp["cantidad"],
        "unidad": fp["unidad"] or "",
    })


async def _run_inference(query: str, producto_nombres: set[str] | None = None) -> tuple[list[ToolCall], str]:
    """Ejecuta el pipeline L1/L2/L3 con un semaforo para limitar concurrencia."""
    assert _infer_sem is not None
    async with _infer_sem:
        full_raw: list[str] = []
        loop = asyncio.get_event_loop()
        first = await loop.run_in_executor(None, _pipeline, query, L1_TOOLS.copy(), full_raw, producto_nombres)

        if first and not _is_suspicious(query, first, producto_nombres):
            return first, " | ".join(full_raw)

        #  Retry: si la eleccion L1/L2 no cuadra con la query, probamos alternativas
        has_p = extract_producto(query, producto_nombres) is not None
        has_n = bool(re.search(r"(\d+)", query))
        needs_write = has_p and has_n
        needs_read = has_p and not has_n

        failed_l1: str | None = None
        failed_l2: str | None = None
        for r in full_raw:
            if r.startswith("L1:"):
                failed_l1 = r[3:]
            elif r.startswith("L2:"):
                failed_l2 = r[3:]

        if not failed_l1:
            return first or [], " | ".join(full_raw)

        #  La query pide escritura pero L2 eligio lectura (o al reves) -> otra L2
        wrong_l2 = (
            (needs_write and failed_l2 in _READ_TOOLS)
            or (needs_read and failed_l2 in _WRITE_TOOLS)
        )
        if wrong_l2:
            alt_l2 = [t for t in _L2_BY_L1.get(failed_l1, []) if t["name"] != failed_l2]
            if alt_l2:
                full_raw.append("retry:L2")
                c2, r2 = await loop.run_in_executor(None, _infer_raw, query, alt_l2)
                full_raw.append(r2)
                if c2:
                    l2c = c2[0].name
                    full_raw.append(f"L2:{l2c}")
                    still_wrong = (
                        (needs_write and l2c in _READ_TOOLS)
                        or (needs_read and l2c in _WRITE_TOOLS)
                    )
                    if still_wrong:
                        full_raw.append("escalate:L1")
                    else:
                        schema = L3_ARGS.get(l2c)
                        if schema:
                            c3, r3 = await loop.run_in_executor(None, _infer_raw, query, [schema])
                            full_raw.append(r3)
                            if c3:
                                args = normalize_args(l2c, c3[0].arguments, query, producto_nombres)
                                return [ToolCall(name=l2c, arguments=args)], " | ".join(full_raw)

        #  Reintentar con la otra L1
        alt_l1 = [t for t in L1_TOOLS if t["name"] != failed_l1]
        full_raw.append("retry:L1")
        result2 = await loop.run_in_executor(None, _pipeline, query, alt_l1, full_raw, producto_nombres)
        if result2:
            return result2, " | ".join(full_raw)

        return first or [], " | ".join(full_raw)


# =====================================================================
#  Endpoints
# =====================================================================

@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok" if model_state.get("model") is not None else "loading",
        model_loaded=model_state.get("model") is not None,
    )


@app.post("/infer", response_model=InferResponse)
async def infer(req: InferRequest):
    if model_state.get("model") is None:
        raise HTTPException(503, "model not loaded")

    session_id = req.session_id or "default"
    t0 = time.time()

    #  Catalogo SIEMPRE: de la bodega si viene, global si no. Sin catalogo
    #  la normalizacion no resuelve ningun producto y toda escritura moria
    #  en el enqueue con producto ''.
    candidatos = await get_producto_nombres_bodega(req.bodega_id)
    producto_nombres = get_producto_nombres_from_candidates(candidatos)

    if req.pending_alert:
        calls, raw = _confirmacion_fast_path(req.query, req.pending_alert, producto_nombres)
    else:
        fp_call = _regex_conteo_fast_path(req.query, producto_nombres) or _regex_write_fast_path(req.query, producto_nombres)
        if fp_call is not None:
            calls, raw = [fp_call], f"regex:{fp_call.name}"
        else:
            calls, raw = await _run_inference(req.query, producto_nombres)

    dt_ms = int((time.time() - t0) * 1000)
    logger.info("infer %dms session=%s calls=%s", dt_ms, session_id, [(c.name, c.arguments) for c in calls])

    pending: list[PendingCall] = []
    for call in calls:
        try:
            if call.name in ("agregar_inventario", "remover_inventario", "registrar_conteo", "confirmar_movimiento"):
                pid = await enqueue_pending(
                    session_id=session_id,
                    tool_name=call.name,
                    arguments=call.arguments,
                    bodega_id=req.bodega_id,
                )
                pending.append(PendingCall(pending_id=pid, tool_name=call.name, arguments=call.arguments))
                logger.info("enqueued pending_id=%d tool=%s args=%s", pid, call.name, call.arguments)
            else:
                #  Lecturas se devuelven en el response, no se encolan
                pending.append(PendingCall(pending_id=0, tool_name=call.name, arguments=call.arguments))
        except ValueError as e:
            logger.warning("enqueue error: %s", e)
            #  No abortamos: devolvemos la tool call sin pending para que
            #  la CLI sepa que fue error de validacion
        except Exception as e:
            logger.exception("enqueue unexpected error")
            raise HTTPException(500, f"queue error: {e}")

    return InferResponse(
        tool_calls=calls,
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


# =====================================================================
#  main
# =====================================================================

def main():
    import uvicorn
    port = int(os.getenv("PORT", "8081"))
    workers = int(os.getenv("UVICORN_WORKERS", "1"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        log_level="info",
    )


if __name__ == "__main__":
    main()
