"""elevenlabs-service: proxy HTTP a ElevenLabs TTS con la misma interfaz
que kokoro-service. POST /speak con streaming de audio PCM int16 24kHz.

Usa ElevenLabs Text-to-Speech API v1.
"""
from __future__ import annotations

import io
import logging
import os
from typing import AsyncIterator

import httpx
import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("elevenlabs-service")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE = os.getenv("ELEVENLABS_VOICE", "9BWtsMINqrJLrRakOkie")  # Aria (Spanish)
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
TARGET_SR = int(os.getenv("TARGET_SAMPLERATE", "24000"))
CHUNK_MS = int(os.getenv("CHUNK_MS", "100"))

app = FastAPI(title="elevenlabs-service", version="1.0.0")


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None
    model: str | None = None
    speed: float = 1.0


@app.get("/health")
async def health():
    return {
        "status": "ok" if ELEVENLABS_API_KEY else "no_api_key",
        "voice": ELEVENLABS_VOICE,
        "model": ELEVENLABS_MODEL,
    }


@app.post("/speak")
async def speak(req: SpeakRequest):
    if not ELEVENLABS_API_KEY:
        raise HTTPException(503, "ELEVENLABS_API_KEY not configured")

    if not req.text.strip():
        raise HTTPException(400, "text is empty")

    voice_id = req.voice or ELEVENLABS_VOICE
    model_id = req.model or ELEVENLABS_MODEL

    async def audio_stream() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": req.text,
                    "model_id": model_id,
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                        "speed": req.speed,
                    },
                },
            )
            if resp.status_code != 200:
                logger.error("elevenlabs error %d: %s", resp.status_code, resp.text[:200])
                raise HTTPException(502, f"ElevenLabs error {resp.status_code}")

            #  Decode MP3 → raw PCM int16 mono 24kHz
            full_data = await resp.aread()
            audio_np, sr = sf.read(io.BytesIO(full_data), dtype="int16")

            #  Resample to TARGET_SR if needed
            if sr != TARGET_SR and len(audio_np) > 0:
                if audio_np.ndim > 1:
                    audio_np = audio_np.mean(axis=1)
                audio_np = audio_np.astype(np.float32) / 32768.0
                new_len = int(len(audio_np) * TARGET_SR / sr)
                audio_np = np.interp(
                    np.linspace(0, len(audio_np) - 1, new_len),
                    np.arange(len(audio_np)),
                    audio_np,
                ).astype(np.float32)
                audio_np = (audio_np * 32767).astype(np.int16)

            if audio_np.ndim > 1:
                audio_np = audio_np.mean(axis=1).astype(np.int16)

            #  Chunk por ~CHUNK_MS ms
            chunk_size = int(TARGET_SR * CHUNK_MS / 1000)
            for i in range(0, len(audio_np), chunk_size):
                yield audio_np[i : i + chunk_size].tobytes()

    return StreamingResponse(audio_stream(), media_type="audio/raw")


def main():
    import uvicorn

    port = int(os.getenv("PORT", "8206"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()