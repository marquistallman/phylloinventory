"""Cliente asincrono para el api-gateway. Lo usa src/cli.py.

Toda la logica de audio, config y backends pasa por el gateway — el
cliente nunca habla directo con voice-service, kokoro-service ni
elevenlabs-service. Asi un solo toggle (POST /api/config) cambia el
comportamiento de CLI y PWA por igual.
"""
import asyncio
import os
import time
from typing import Any

import httpx

#  Carga .env / .env.example para que GATEWAY_URL, VOICE_WS_URL,
#  CLI_POLL_TIMEOUT, etc. reflejen el .env del usuario si existe.
from .env_loader import load_env
load_env()

GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://127.0.0.1:8200")
# Mantenido por compatibilidad — el gateway hace el routing ahora.
VOICE_WS_URL = os.getenv("VOICE_WS_URL", "ws://127.0.0.1:8100/ws/transcribe")

POLL_TIMEOUT_S = float(os.getenv("CLI_POLL_TIMEOUT", "15"))
POLL_INTERVAL_S = float(os.getenv("CLI_POLL_INTERVAL", "0.2"))


# =====================================================================
#  Health / query / inventory (existentes, sin cambios)
# =====================================================================

async def health() -> dict:
    #  El /health del gateway hace hasta 5 sub-pings (needle, kokoro, voice,
    #  elevenlabs, db) cada uno con su propio timeout de 3s. 15s de piso.
    #  Sumamos margen para el setup de la conexion.
    async with httpx.AsyncClient(timeout=20) as client:
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
    try:
        bodegas = await list_bodegas()
    except Exception:
        return None
    if not bodegas:
        return None
    q = query.lower().strip()
    for b in bodegas:
        if b["nombre"] == q:
            return int(b["id"])
    for b in bodegas:
        if b["nombre"].startswith(q):
            return int(b["id"])
    try:
        from llm_common.fuzzy_search import fuzzy_match
    except Exception:
        return None
    m = fuzzy_match(query, [{"id": b["id"], "nombre": b["nombre"]} for b in bodegas])
    return int(m["id"]) if m else None


# =====================================================================
#  Config runtime (toggle cloud + overrides)
# =====================================================================

async def get_config() -> dict:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{GATEWAY_URL}/api/config")
        r.raise_for_status()
        return r.json()


async def set_config(
    cloud_enabled: bool | None = None,
    llm: str | None = None,
    stt: str | None = None,
    tts: str | None = None,
) -> dict:
    payload: dict[str, Any] = {}
    if cloud_enabled is not None:
        payload["cloud_enabled"] = cloud_enabled
    if llm is not None:
        payload["llm"] = llm
    if stt is not None:
        payload["stt"] = stt
    if tts is not None:
        payload["tts"] = tts
    if not payload:
        return await get_config()
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.post(f"{GATEWAY_URL}/api/config", json=payload)
        r.raise_for_status()
        return r.json()


# =====================================================================
#  Audio: transcribir (STT) y speak (TTS)
#  Pasan siempre por el gateway — el cliente no sabe ni le importa
#  si el backend activo es cloud o local.
# =====================================================================

async def transcribe_audio(file_path: str, content_type: str | None = None) -> dict:
    """Envia un archivo de audio al gateway para transcripcion.

    Devuelve {text, backend, fallback_used, language, ...} o levanta
    HTTPError si el gateway rechaza (ej: audio no decodificable).
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)
    if content_type is None:
        ext = os.path.splitext(file_path)[1].lower()
        content_type = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
            ".webm": "audio/webm",
            ".flac": "audio/flac",
        }.get(ext, "application/octet-stream")
    async with httpx.AsyncClient(timeout=60) as client:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f.read(), content_type)}
            r = await client.post(f"{GATEWAY_URL}/api/audio/transcribir", files=files)
        r.raise_for_status()
        return r.json()


async def speak_remote(text: str, voice_id: str | None = None, speed: float | None = None) -> dict:
    """Pide al gateway que sintetice `text`. Devuelve
    {audio: <bytes>, sample_rate, channels, backend, fallback_used, content_type}.
    """
    payload: dict[str, Any] = {"text": text}
    if voice_id:
        payload["voice_id"] = voice_id
    if speed is not None:
        payload["speed"] = speed
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
    ) as client:
        r = await client.post(f"{GATEWAY_URL}/api/audio/speak", json=payload)
        r.raise_for_status()
        return {
            "audio": r.content,
            "sample_rate": int(r.headers.get("x-sample-rate", "24000")),
            "channels": int(r.headers.get("x-channels", "1")),
            "backend": r.headers.get("x-backend", "?"),
            "fallback_used": r.headers.get("x-fallback-used", "false") == "true",
            "content_type": r.headers.get("content-type", "audio/pcm"),
        }


async def list_voices() -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{GATEWAY_URL}/api/audio/voices")
        r.raise_for_status()
        return r.json()
