"""api-gateway: punto de entrada unico para la CLI y la PWA.

Responsabilidades:
- Enrutar a los backends de LLM (needle/openrouter), STT (whisper/elevenlabs)
  y TTS (kokoro/elevenlabs) segun un toggle runtime + overrides por-backend.
- Si el toggle cloud esta activo, intentar primero el backend cloud y caer
  al local en caso de error (HTTP 502/503/504, timeout, request error).
- Exponer proxies HTTP para audio (transcribir/speak) backend-agnosticos:
  la CLI/PWA nunca habla directo con voice-service ni kokoro-service ni
  elevenlabs-service — siempre pasa por aca.
- Exponer /api/config para que la UI consulte y modifique el toggle runtime.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from llm_common.db import (
    fetch,
    fetchrow,
    close_pool,
    enqueue_pending,
    enqueue_registro_manual,
    get_catalogo_bodega,
    get_pending_status,
)
from llm_common import nlu

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api-gateway")

# =====================================================================
#  Configuracion por env (defaults al arranque)
# =====================================================================

LLM_BACKEND = os.getenv("LLM_BACKEND", "needle").lower()  # needle | openrouter
STT_BACKEND = os.getenv("STT_BACKEND", "whisper").lower()  # whisper | elevenlabs
TTS_BACKEND = os.getenv("TTS_BACKEND", "kokoro").lower()   # kokoro  | elevenlabs

NEEDLE_URL = os.getenv("NEEDLE_URL", "http://needle-service:8081")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "http://openrouter-service:8082")
VOICE_URL = os.getenv("VOICE_URL", "http://voice-service:8100")  # host:port (sin /ws)
KOKORO_URL = os.getenv("KOKORO_URL", "http://kokoro-service:8205")
ELEVENLABS_URL = os.getenv("ELEVENLABS_URL", "http://elevenlabs-service:8206")
KALMAN_URL = os.getenv("KALMAN_URL", "http://kalman-worker:8300")

VOICE_WS_URL = VOICE_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/transcribe"

# =====================================================================
#  Estado runtime (toggle cloud + overrides por-backend, en memoria)
# =====================================================================

_cloud_toggle: bool = os.getenv("CLOUD_ENABLED", "false").lower() == "true"
_llm_override: str | None = None   # None = seguir el toggle
_stt_override: str | None = None
_tts_override: str | None = None

_lock = asyncio.Lock()


def _pick_llm() -> str:
    if _llm_override in ("needle", "openrouter"):
        return _llm_override
    return "openrouter" if _cloud_toggle else LLM_BACKEND


def _pick_stt() -> str:
    if _stt_override in ("whisper", "elevenlabs"):
        return _stt_override
    return "elevenlabs" if _cloud_toggle else STT_BACKEND


def _pick_tts() -> str:
    if _tts_override in ("kokoro", "elevenlabs"):
        return _tts_override
    return "elevenlabs" if _cloud_toggle else TTS_BACKEND


def _llm_url() -> str:
    return OPENROUTER_URL if _pick_llm() == "openrouter" else NEEDLE_URL


def _current_config() -> dict[str, Any]:
    return {
        "cloud_enabled": _cloud_toggle,
        "llm": _pick_llm(),
        "stt": _pick_stt(),
        "tts": _pick_tts(),
        "llm_override": _llm_override,
        "stt_override": _stt_override,
        "tts_override": _tts_override,
        "defaults": {"llm": LLM_BACKEND, "stt": STT_BACKEND, "tts": TTS_BACKEND},
    }


# =====================================================================
#  Helpers de routing y fallback
# =====================================================================

async def _post_with_fallback(
    client: httpx.AsyncClient,
    *,
    cloud_url: str | None,
    local_url: str,
    payload: dict,
    want_cloud: bool,
    label: str,
    timeout: float = 60.0,
) -> tuple[httpx.Response, str, bool]:
    """POST con fallback cloud->local.

    Retorna (response, backend_usado, fallback_used).
    Si want_cloud=True y cloud_url dado, intenta cloud primero; si falla,
    cae a local con fallback_used=True. Si want_cloud=False va directo a local.
    """
    if want_cloud and cloud_url:
        try:
            r = await client.post(cloud_url, json=payload, timeout=timeout)
            if r.status_code == 200:
                return r, "cloud", False
            logger.warning("%s cloud http %d, falling back", label, r.status_code)
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logger.warning("%s cloud transport error: %s, falling back", label, e)
        r = await client.post(local_url, json=payload, timeout=timeout)
        return r, "local", True
    r = await client.post(local_url, json=payload, timeout=timeout)
    return r, "local", False


# =====================================================================
#  App
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "api-gateway up. cloud=%s llm=%s stt=%s tts=%s",
        _cloud_toggle, _pick_llm(), _pick_stt(), _pick_tts(),
    )
    yield
    await close_pool()


app = FastAPI(title="api-gateway", version="3.0.0", lifespan=lifespan)


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
    backend_requested: str
    fallback_used: bool
    tool_calls: list[dict] = []
    pending: list[dict] = []
    raw_output: str = ""


class ConfigUpdate(BaseModel):
    cloud_enabled: bool | None = None
    llm: str | None = None      # "needle" | "openrouter" | "auto"
    stt: str | None = None      # "whisper" | "elevenlabs" | "auto"
    tts: str | None = None      # "kokoro" | "elevenlabs" | "auto"


# =====================================================================
#  /api/config  (toggle runtime)
# =====================================================================

@app.get("/api/config")
async def get_config():
    return _current_config()


@app.post("/api/config")
async def update_config(req: ConfigUpdate):
    global _cloud_toggle, _llm_override, _stt_override, _tts_override
    async with _lock:
        if req.cloud_enabled is not None:
            _cloud_toggle = bool(req.cloud_enabled)
        for field, name in (("llm", "_llm_override"), ("stt", "_stt_override"), ("tts", "_tts_override")):
            v = getattr(req, field)
            if v is None:
                continue
            v = v.lower()
            if v == "auto":
                globals()[name] = None
            elif v in _VALID_OVERRIDES[field]:
                globals()[name] = v
            else:
                raise HTTPException(400, f"{field} invalido: {v}")
        logger.info(
            "config update: cloud=%s llm=%s stt=%s tts=%s",
            _cloud_toggle, _pick_llm(), _pick_stt(), _pick_tts(),
        )
    return _current_config()


_VALID_OVERRIDES = {
    "llm": ("needle", "openrouter"),
    "stt": ("whisper", "elevenlabs"),
    "tts": ("kokoro", "elevenlabs"),
}


# =====================================================================
#  Health
# =====================================================================

@app.get("/health")
async def health():
    out: dict[str, Any] = {
        "status": "ok",
        "config": _current_config(),
        "services": {},
    }
    async with httpx.AsyncClient(timeout=3) as client:
        targets = [
            ("needle", NEEDLE_URL),
            ("openrouter", OPENROUTER_URL),
            ("kokoro", KOKORO_URL),
            ("elevenlabs", ELEVENLABS_URL),
        ]
        if _pick_stt() == "whisper":
            targets.append(("voice", VOICE_URL))
        for name, url in targets:
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


# =====================================================================
#  /query  (LLM con fallback cloud->local)
# =====================================================================

@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    pick = _pick_llm()
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

    async with httpx.AsyncClient(timeout=60) as client:
        if pick == "openrouter":
            r, used, fallback = await _post_with_fallback(
                client,
                cloud_url=f"{OPENROUTER_URL}/infer",
                local_url=f"{NEEDLE_URL}/infer",
                payload=payload,
                want_cloud=True,
                label="LLM",
            )
        else:
            r, used, fallback = await _post_with_fallback(
                client,
                cloud_url=None,
                local_url=f"{NEEDLE_URL}/infer",
                payload=payload,
                want_cloud=False,
                label="LLM",
            )

    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)

    data = r.json()
    return QueryResponse(
        backend=used,
        backend_requested=pick,
        fallback_used=fallback,
        tool_calls=data.get("tool_calls", []),
        pending=data.get("pending", []),
        raw_output=data.get("raw_output", ""),
    )


# =====================================================================
#  /api/audio/transcribir  (STT con fallback cloud->local)
# =====================================================================

@app.post("/api/audio/transcribir")
async def audio_transcribir(file: UploadFile = File(...)):
    pick = _pick_stt()
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "audio file vacio")
    fname = file.filename or "audio.webm"
    ctype = file.content_type or "application/octet-stream"

    async with httpx.AsyncClient(timeout=60) as client:
        if pick == "elevenlabs":
            files = {"file": (fname, raw, ctype)}
            data = {"language_code": "es"}
            try:
                r = await client.post(
                    f"{ELEVENLABS_URL}/transcribe",
                    files=files, data=data, timeout=60,
                )
                if r.status_code == 200:
                    out = r.json()
                    out["backend_requested"] = pick
                    out["fallback_used"] = False
                    return out
                logger.warning("STT elevenlabs http %d, falling back to whisper: %s",
                               r.status_code, r.text[:200])
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.warning("STT elevenlabs transport error: %s, falling back", e)
            # Fallback: Whisper local (necesita PCM 16k)
            pcm = await _ffmpeg_to_pcm16k(raw, ctype)
            if pcm is None:
                raise HTTPException(500, "no se pudo decodificar el audio para fallback local")
            text = await _whisper_ws_transcribe(pcm)
            return {
                "text": text,
                "language": "es",
                "backend": "whisper",
                "backend_requested": pick,
                "fallback_used": True,
            }
        # pick == "whisper"
        pcm = await _ffmpeg_to_pcm16k(raw, ctype)
        if pcm is None:
            raise HTTPException(400, "no se pudo decodificar el audio (formato no soportado)")
        text = await _whisper_ws_transcribe(pcm)
        return {
            "text": text,
            "language": "es",
            "backend": "whisper",
            "backend_requested": pick,
            "fallback_used": False,
        }


async def _ffmpeg_to_pcm16k(raw: bytes, content_type: str) -> bytes | None:
    """Convierte cualquier formato a PCM int16 LE mono 16kHz via ffmpeg.

    Devuelve None si ffmpeg falla o no esta disponible.
    """
    #  Si ya parece PCM crudo, devolver tal cual.
    if content_type.startswith("audio/L16") or content_type == "audio/pcm":
        return raw
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", "16000",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error("ffmpeg no instalado en el contenedor")
        return None
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=raw), timeout=30,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        logger.warning("ffmpeg rc=%d: %s", proc.returncode, err.decode("utf-8", "replace")[:300])
        return None
    return out


async def _whisper_ws_transcribe(pcm_bytes: bytes) -> str:
    """Envia PCM int16 16k mono al voice-service por WS y devuelve la transcripcion final."""
    url = VOICE_WS_URL
    try:
        async with websockets.connect(url, open_timeout=30, max_size=2**23) as ws:
            #  Enviar en chunks de ~100ms (3200 bytes) para imitar el flujo del CLI
            chunk = 3200
            for i in range(0, len(pcm_bytes), chunk):
                await ws.send(pcm_bytes[i:i + chunk])
            await ws.send(json.dumps({"type": "stop"}))
            while True:
                msg = await ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                t = data.get("type")
                if t == "final":
                    return (data.get("text") or "").strip()
                if t == "error":
                    raise HTTPException(502, f"voice-service: {data.get('message')}")
    except websockets.exceptions.WebSocketException as e:
        raise HTTPException(502, f"voice-service WS error: {e}")
    return ""


# =====================================================================
#  /api/audio/speak  (TTS con fallback cloud->local)
# =====================================================================

class SpeakRequest(BaseModel):
    text: str
    voice_id: str | None = None   # Eleven Labs voice (si backend=elevenlabs)
    speed: float | None = None    # Kokoro


@app.post("/api/audio/speak")
async def audio_speak(req: SpeakRequest):
    pick = _pick_tts()
    if pick == "elevenlabs":
        payload = {"text": req.text}
        if req.voice_id:
            payload["voice_id"] = req.voice_id
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10)) as client:
            try:
                async with client.stream(
                    "POST", f"{ELEVENLABS_URL}/speak", json=payload,
                ) as r:
                    if r.status_code == 200:
                        return _proxy_tts_stream(r, "elevenlabs", fallback=False)
                    logger.warning("TTS elevenlabs http %d, falling back to kokoro",
                                   r.status_code)
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.warning("TTS elevenlabs transport error: %s, falling back", e)
        # Fallback: Kokoro
        return await _kokoro_speak(req.text, req.speed, fallback=True)
    # pick == "kokoro"
    return await _kokoro_speak(req.text, req.speed, fallback=False)


async def _kokoro_speak(text: str, speed: float | None, fallback: bool) -> StreamingResponse:
    payload: dict[str, Any] = {"text": text}
    if speed is not None:
        payload["speed"] = speed
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10)) as client:
        try:
            async with client.stream(
                "POST", f"{KOKORO_URL}/speak", json=payload,
            ) as r:
                if r.status_code != 200:
                    raise HTTPException(r.status_code, f"kokoro error: {r.text[:200]}")
                return _proxy_tts_stream(r, "kokoro", fallback=fallback)
        except httpx.RequestError as e:
            raise HTTPException(503, f"kokoro unreachable: {e}")


def _proxy_tts_stream(upstream: httpx.Response, backend: str, fallback: bool) -> StreamingResponse:
    extra = {
        "X-Backend": backend,
        "X-Backend-Requested": "elevenlabs" if _pick_tts() == "elevenlabs" else "kokoro",
        "X-Fallback-Used": "true" if fallback else "false",
    }
    for h in ("X-Sample-Rate", "X-Channels", "X-Sample-Width", "X-Endian", "X-Voice-Id", "X-Model-Id"):
        if h in upstream.headers:
            extra[h] = upstream.headers[h]
    ctype = upstream.headers.get("content-type", "audio/pcm")

    async def gen():
        async for chunk in upstream.aiter_bytes(chunk_size=4096):
            if chunk:
                yield chunk

    return StreamingResponse(gen(), media_type=ctype, headers=extra)


# =====================================================================
#  /api/audio/voices  (proxy a elevenlabs-service con cache server-side)
# =====================================================================

@app.get("/api/audio/voices")
async def audio_voices():
    pick = _pick_tts()
    if pick != "elevenlabs":
        #  Sin elevenlabs activo, devolvemos la voz default que Kokoro usaria
        return {
            "voices": [
                {
                    "voice_id": "kokoro_default",
                    "name": "Kokoro (local)",
                    "category": "local",
                    "labels": {"lang": "es"},
                    "preview_url": None,
                }
            ],
            "default_voice_id": "kokoro_default",
            "backend": "kokoro",
        }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ELEVENLABS_URL}/voices")
            if r.status_code != 200:
                raise HTTPException(r.status_code, f"elevenlabs voices: {r.text[:200]}")
            out = r.json()
            out["backend"] = "elevenlabs"
            return out
    except httpx.RequestError as e:
        raise HTTPException(503, f"elevenlabs unreachable: {e}")


# =====================================================================
#  Endpoints existentes (sin cambios funcionales)
# =====================================================================

@app.get("/status/{pending_id}")
async def status(pending_id: int):
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
#  Registro de conteo
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

    conteo = nlu.parse_conteo_rapido(req.texto)
    if conteo:
        from llm_common.fuzzy_search import fuzzy_match_product
        candidatos = await fetch(
            """SELECT id, nombre, unidad
               FROM productos_en_bodega
               WHERE bodega_id = $1""",
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

    #  Fallback: LLM (con cloud->local)
    async with httpx.AsyncClient(timeout=60) as client:
        if _pick_llm() == "openrouter":
            r, used, fallback = await _post_with_fallback(
                client,
                cloud_url=f"{OPENROUTER_URL}/infer",
                local_url=f"{NEEDLE_URL}/infer",
                payload={
                    "query": req.texto,
                    "tools": "[]",
                    "session_id": str(req.sesion_id),
                    "mode": "full",
                    "bodega_id": sesion["bodega_id"],
                },
                want_cloud=True,
                label="LLM(voz)",
            )
        else:
            r, used, fallback = await _post_with_fallback(
                client,
                cloud_url=None,
                local_url=f"{NEEDLE_URL}/infer",
                payload={
                    "query": req.texto,
                    "tools": "[]",
                    "session_id": str(req.sesion_id),
                    "mode": "full",
                    "bodega_id": sesion["bodega_id"],
                },
                want_cloud=False,
                label="LLM(voz)",
            )

    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    data = r.json()
    return {
        "success": True,
        "via": "llm",
        "backend": used,
        "fallback_used": fallback,
        "tool_calls": data.get("tool_calls", []),
        "pending": data.get("pending", []),
        "raw_output": data.get("raw_output", ""),
    }


# =====================================================================
#  Catalogo / Reportes / Pending (sin cambios)
# =====================================================================

@app.get("/api/catalogo/bodega/{bodega_id}")
async def catalogo_bodega(
    bodega_id: int,
    q: str | None = None,
    solo_pendientes: bool = False,
    sesion_id: int | None = None,
):
    return await get_catalogo_bodega(bodega_id, q=q, solo_pendientes=solo_pendientes, sesion_id=sesion_id)


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
#  Entrypoint
# =====================================================================

def main():
    import uvicorn
    port = int(os.getenv("PORT", "8200"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
