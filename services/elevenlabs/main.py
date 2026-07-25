"""elevenlabs-service: proxy HTTP a la API de Eleven Labs.

Expone la misma interfaz que los servicios locales (voice + kokoro) pero
apuntando a la API cloud. Asi el api-gateway puede rutear transparente
segun el toggle `cloud_enabled` y/o los overrides por-backend (stt/tts).

Endpoints:
  GET  /health        -> {status, has_key, stt_model, tts_model, default_voice}
  POST /transcribe    -> multipart file -> {text, language, backend: "elevenlabs"}
  POST /speak         -> json {text, voice_id?, model_id?, output_format?}
                        -> stream audio/pcm (int16 mono 24k LE por default)
  GET  /voices        -> {voices: [...], default_voice_id}  (cache 1h en memoria)

Eleven Labs API refs:
  STT:  POST https://api.elevenlabs.io/v1/speech-to-text
  TTS:  POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
  Vcs:  GET  https://api.elevenlabs.io/v1/voices
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

#  Carga .env / .env.example ANTES de leer cualquier os.getenv().
from llm_common.env_loader import load_env
load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("elevenlabs-service")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_BASE = os.getenv("ELEVENLABS_BASE", "https://api.elevenlabs.io")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel
ELEVENLABS_STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1")
ELEVENLABS_TTS_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2")
# pcm_24000 = int16 LE mono 24kHz: encaja exacto con tts_client.py (SAMPLE_RATE=24000)
# Otras opciones validas: pcm_16000, pcm_22050, pcm_44100, mp3_22050_32, mp3_44100_128
ELEVENLABS_TTS_OUTPUT = os.getenv("ELEVENLABS_TTS_OUTPUT", "pcm_24000")
PORT = int(os.getenv("PORT", "8206"))

# Cache simple de voces (1h). Evita martillar la API si la PWA lo llama al
# renderizar un selector.
_voices_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}
_VOICES_TTL_S = 3600.0

# =====================================================================
#  App
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not ELEVENLABS_API_KEY:
        logger.warning("ELEVENLABS_API_KEY no configurada — el servicio fallara al usar")
    else:
        logger.info(
            "Eleven Labs listo (stt=%s, tts=%s, voice=%s, output=%s)",
            ELEVENLABS_STT_MODEL, ELEVENLABS_TTS_MODEL,
            ELEVENLABS_VOICE_ID, ELEVENLABS_TTS_OUTPUT,
        )
    yield


app = FastAPI(title="elevenlabs-service", version="1.0.0", lifespan=lifespan)


def _auth_headers(accept: str | None = None) -> dict[str, str]:
    h = {"xi-api-key": ELEVENLABS_API_KEY}
    if accept:
        h["Accept"] = accept
    return h


# =====================================================================
#  Health
# =====================================================================

@app.get("/health")
async def health():
    return {
        "status": "ok" if ELEVENLABS_API_KEY else "no_api_key",
        "has_key": bool(ELEVENLABS_API_KEY),
        "stt_model": ELEVENLABS_STT_MODEL,
        "tts_model": ELEVENLABS_TTS_MODEL,
        "default_voice": ELEVENLABS_VOICE_ID,
        "tts_output": ELEVENLABS_TTS_OUTPUT,
    }


# =====================================================================
#  STT — POST /transcribe
# =====================================================================

@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language_code: str = Form("es"),
    model_id: str | None = Form(None),
):
    if not ELEVENLABS_API_KEY:
        raise HTTPException(503, "ELEVENLABS_API_KEY not configured")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "audio file vacio")
    fname = file.filename or "audio.webm"
    ctype = file.content_type or "application/octet-stream"

    model = model_id or ELEVENLABS_STT_MODEL
    # Eleven Labs espera multipart: file=<bytes>, model_id, language_code
    files = {"file": (fname, raw, ctype)}
    data = {"model_id": model, "language_code": language_code}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{ELEVENLABS_BASE}/v1/speech-to-text",
                headers=_auth_headers("application/json"),
                files=files,
                data=data,
            )
    except httpx.RequestError as e:
        logger.error("elevenlabs STT transport error: %s", e)
        raise HTTPException(502, f"elevenlabs unreachable: {e}")

    if r.status_code != 200:
        logger.error("elevenlabs STT %d: %s", r.status_code, r.text[:300])
        raise HTTPException(502, f"elevenlabs STT error {r.status_code}: {r.text[:200]}")

    payload = r.json()
    text = (payload.get("text") or "").strip()
    lang = payload.get("language_code") or language_code
    logger.info("STT %dB -> %d chars (lang=%s)", len(raw), len(text), lang)
    return {
        "text": text,
        "language": lang,
        "backend": "elevenlabs",
        "model": model,
    }


# =====================================================================
#  TTS — POST /speak  (streaming, PCM por default)
# =====================================================================

@app.post("/speak")
async def speak(payload: dict):
    if not ELEVENLABS_API_KEY:
        raise HTTPException(503, "ELEVENLABS_API_KEY not configured")

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text vacio")

    voice_id = payload.get("voice_id") or ELEVENLABS_VOICE_ID
    model_id = payload.get("model_id") or ELEVENLABS_TTS_MODEL
    output_format = payload.get("output_format") or ELEVENLABS_TTS_OUTPUT
    # voice_settings: opcionales, defaults sensatos de Eleven Labs
    voice_settings = payload.get("voice_settings") or {
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
    }

    body = {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings,
    }
    params = {"output_format": output_format}
    headers = _auth_headers()

    is_pcm = output_format.startswith("pcm_")
    media_type = "audio/pcm" if is_pcm else "audio/mpeg"
    sample_rate = _parse_sample_rate(output_format) if is_pcm else None

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10)) as client:
                async with client.stream(
                    "POST",
                    f"{ELEVENLABS_BASE}/v1/text-to-speech/{voice_id}",
                    headers={**headers, "Accept": media_type, "Content-Type": "application/json"},
                    params=params,
                    json=body,
                ) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        logger.error("elevenlabs TTS %d: %s", r.status_code, err[:300])
                        raise HTTPException(r.status_code, f"elevenlabs TTS error: {err[:200].decode('utf-8', 'replace')}")
                    async for chunk in r.aiter_bytes(chunk_size=4096):
                        if chunk:
                            yield chunk
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("elevenlabs TTS stream error")
            raise HTTPException(502, f"elevenlabs TTS stream error: {e}")

    extra_headers = {
        "X-Voice-Id": voice_id,
        "X-Model-Id": model_id,
        "X-Output-Format": output_format,
    }
    if sample_rate:
        extra_headers["X-Sample-Rate"] = str(sample_rate)
        extra_headers["X-Channels"] = "1"
        extra_headers["X-Sample-Width"] = "2"
        extra_headers["X-Endian"] = "little"

    return StreamingResponse(stream(), media_type=media_type, headers=extra_headers)


def _parse_sample_rate(fmt: str) -> int | None:
    # pcm_24000 -> 24000
    try:
        return int(fmt.split("_")[1])
    except (IndexError, ValueError):
        return None


# =====================================================================
#  Voices — GET /voices  (cache 1h)
# =====================================================================

@app.get("/voices")
async def voices():
    if not ELEVENLABS_API_KEY:
        raise HTTPException(503, "ELEVENLABS_API_KEY not configured")

    now = time.time()
    if _voices_cache["data"] and (now - _voices_cache["fetched_at"]) < _VOICES_TTL_S:
        return _voices_cache["data"]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{ELEVENLABS_BASE}/v1/voices",
                headers=_auth_headers(),
            )
    except httpx.RequestError as e:
        logger.error("elevenlabs voices transport error: %s", e)
        raise HTTPException(502, f"elevenlabs unreachable: {e}")

    if r.status_code != 200:
        logger.error("elevenlabs voices %d: %s", r.status_code, r.text[:300])
        raise HTTPException(502, f"elevenlabs voices error {r.status_code}")

    raw = r.json()
    voices = []
    for v in raw.get("voices", []):
        voices.append({
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "category": v.get("category"),
            "labels": v.get("labels") or {},
            "preview_url": v.get("preview_url"),
            "description": v.get("description"),
        })

    payload = {
        "voices": voices,
        "default_voice_id": ELEVENLABS_VOICE_ID,
        "fetched_at": now,
    }
    _voices_cache["data"] = payload
    _voices_cache["fetched_at"] = now
    logger.info("voices cache refreshed: %d voces", len(voices))
    return payload


# =====================================================================
#  Entrypoint
# =====================================================================

def main():
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
