"""kokoro-service: TTS via Kokoro-82M/84M.

Expone un unico endpoint POST /speak que recibe texto y devuelve audio
**streamed** en PCM int16 mono 24kHz, un chunk por oracion (Kokoro ya
genera por oracion via `split_pattern=r'\n+'`). El cliente puede ir
reproduciendo cada chunk a medida que llega.

Protocolo:
  POST /speak
  Content-Type: application/json
  Body: {"text": "...", "voice": "ef_dora", "speed": 1.0}
  Response:
    Transfer-Encoding: chunked
    Content-Type: audio/pcm
    X-Sample-Rate: 24000
    X-Channels: 1
    Body: <bytes int16 mono 24kHz, concatenados por oracion>

El modelo se pre-carga en el lifespan (mismo patron que voice-service) para
no bloquear el event loop de uvicorn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

#  Carga .env / .env.example ANTES de leer cualquier os.getenv().
from llm_common.env_loader import load_env
load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kokoro-service")

LANG_CODE = os.getenv("LANG_CODE", "e")  # 'e' = Spanish (es)
VOICE = os.getenv("VOICE", "ef_dora")
SPEED = float(os.getenv("SPEED", "1.0"))
PORT = int(os.getenv("PORT", "8205"))
SAMPLE_RATE = 24000

_pipeline = None
_pipeline_ready: asyncio.Event | None = None


def _load_pipeline_sync():
    """Carga bloqueante (Kokoro es sync) — corre en un executor."""
    from kokoro import KPipeline
    logger.info("Cargando Kokoro (lang=%s, voice=%s) ...", LANG_CODE, VOICE)
    p = KPipeline(lang_code=LANG_CODE)
    logger.info("Kokoro listo.")
    return p


def _set_pipeline_when_loaded() -> None:
    global _pipeline
    try:
        _pipeline = _load_pipeline_sync()
    except Exception as e:
        logger.exception("Fallo cargando Kokoro: %s", e)
        return
    if _pipeline_ready is not None:
        _model_ready_loop.call_soon_threadsafe(_model_ready_set)


_model_ready_loop: asyncio.AbstractEventLoop | None = None


def _model_ready_set() -> None:
    if _pipeline_ready is not None:
        _pipeline_ready.set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-carga Kokoro en background (no bloquea el WS/HTTP)."""
    global _pipeline_ready, _model_ready_loop
    _pipeline_ready = asyncio.Event()
    _model_ready_loop = asyncio.get_event_loop()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _set_pipeline_when_loaded)
    yield


app = FastAPI(title="kokoro-service", version="1.0.0", lifespan=lifespan)


async def _wait_pipeline(timeout_s: float = 180.0):
    if _pipeline is not None:
        return _pipeline
    assert _pipeline_ready is not None
    try:
        await asyncio.wait_for(_pipeline_ready.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise RuntimeError("Kokoro no esta listo (timeout)")
    if _pipeline is None:
        raise RuntimeError("Kokoro fallo al cargar; revisa los logs del contenedor")
    return _pipeline


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": _pipeline is not None,
        "voice": VOICE,
        "lang": LANG_CODE,
        "speed": SPEED,
        "sample_rate": SAMPLE_RATE,
    }


def _synth_chunks(text: str, voice: str | None, speed: float | None) -> AsyncIterator[bytes]:
    """Genera audio por oracion. Cada yield es un chunk PCM int16 mono 24k.

    Kokoro trocea por '\\n+' por defecto. Cada (gs, ps, audio) sale
    apenas el modelo termina esa oracion — perfecto para streaming real.
    """
    pipeline = _pipeline
    if pipeline is None:
        raise RuntimeError("Modelo no cargado")
    v = voice or VOICE
    s = speed if speed is not None else SPEED

    #  Asegurar que el texto no este vacio
    t = (text or "").strip()
    if not t:
        return

    for gs, ps, audio in pipeline(t, voice=v, speed=s, split_pattern=r"\n+"):
        if audio is None or len(audio) == 0:
            continue
        #  Kokoro devuelve float32 en [-1, 1]; pasamos a int16 LE.
        samples = (np.asarray(audio, dtype=np.float32) * 32767.0)
        samples = np.clip(samples, -32768, 32767).astype(np.int16)
        yield samples.tobytes()


@app.post("/speak")
async def speak(request: Request):
    """Stream de audio TTS. Body: {text, voice?, speed?}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body invalido, esperaba JSON"}, status_code=400)

    text = body.get("text", "")
    voice = body.get("voice")
    speed = body.get("speed")

    if not text or not text.strip():
        return JSONResponse({"error": "text vacio"}, status_code=400)

    try:
        await _wait_pipeline()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    return StreamingResponse(
        _synth_chunks(text, voice, speed),
        media_type="audio/pcm",
        headers={
            "X-Sample-Rate": str(SAMPLE_RATE),
            "X-Channels": "1",
            "X-Sample-Width": "2",
            "X-Endian": "little",
        },
    )


def main():
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
