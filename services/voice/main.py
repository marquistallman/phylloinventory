"""voice-service: WebSocket bidireccional que recibe audio PCM 16k
y devuelve transcripciones incrementales. Audio efimero: los chunks
nunca se persisten a disco; se procesan en streaming y se descartan
del buffer circular al transcribir.

Protocolo WS:
- Cliente envia mensajes binarios con frames de audio PCM int16 mono 16kHz.
- Cliente puede enviar {"type": "stop"} para forzar transcripcion final.
- Servidor responde con:
    {"type": "partial", "text": "..."}  (mientras habla)
    {"type": "final",   "text": "..."}  (cuando hay silencio o stop)
    {"type": "error",   "message": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("voice-service")

SAMPLE_RATE = int(os.getenv("VOICE_SAMPLE_RATE", "16000"))
MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")
LANGUAGE = os.getenv("WHISPER_LANGUAGE", "es")
SILENCE_RMS = float(os.getenv("VOICE_SILENCE_RMS", "0.01"))
SILENCE_SECS = float(os.getenv("VOICE_SILENCE_SECS", "0.8"))
MIN_UTTERANCE_SECS = float(os.getenv("VOICE_MIN_UTTERANCE", "0.5"))
MAX_BUFFER_SECS = float(os.getenv("VOICE_MAX_BUFFER", "30"))

_model: Any = None
_model_ready: asyncio.Event | None = None


def _load_model_sync() -> Any:
    """Carga bloqueante — se ejecuta en thread para no trabar el event loop."""
    from faster_whisper import WhisperModel
    logger.info("Cargando Whisper %s (cpu, int8) ...", MODEL_SIZE)
    m = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    logger.info("Whisper listo.")
    return m


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranque: pre-carga el modelo en background para no bloquear uvicorn.

    Si se cargara dentro del handler del WS, la primera conexion quedaria
    pegada ~30-60s mientras Whisper descarga pesos, y ademas bloquearia el
    event loop (WhisperModel es sync). Durante esa ventana, el resto de
    conexiones WS nuevas ven 'timed out during opening handshake'.
    """
    global _model, _model_ready
    _model_ready = asyncio.Event()
    asyncio.get_event_loop().run_in_executor(None, _set_model_when_loaded)
    yield
    #  Nada que cerrar: el modelo se libera con el proceso.


def _set_model_when_loaded() -> None:
    global _model
    try:
        _model = _load_model_sync()
    except Exception as e:
        logger.exception("Fallo cargando Whisper: %s", e)
        return
    #  Avisar al event loop que el modelo esta listo
    if _model_ready is not None:
        _model_ready.set()


app = FastAPI(title="voice-service", version="1.0.0", lifespan=lifespan)


def _rms(int16_samples: np.ndarray) -> float:
    if int16_samples.size == 0:
        return 0.0
    f = int16_samples.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(f * f) + 1e-12))


async def _wait_model(timeout_s: float = 120.0) -> Any:
    """Espera (sin bloquear el event loop) a que el modelo este listo."""
    if _model is not None:
        return _model
    assert _model_ready is not None
    try:
        await asyncio.wait_for(_model_ready.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise RuntimeError("Whisper aun no esta listo (timeout)")
    if _model is None:
        raise RuntimeError("Whisper fallo al cargar; revisa los logs del contenedor")
    return _model


async def _transcribe(model, audio_f32: np.ndarray) -> str:
    if audio_f32.size < int(SAMPLE_RATE * MIN_UTTERANCE_SECS):
        return ""
    #  Whisper corre bloqueante; lo mandamos a un thread.
    def _run():
        segments, _ = model.transcribe(
            audio_f32,
            language=LANGUAGE,
            vad_filter=False,  # ya hicimos nuestro VAD por energia
            beam_size=1,
        )
        return " ".join(seg.text for seg in segments).strip()

    return await asyncio.to_thread(_run)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_size": MODEL_SIZE,
        "model_loaded": _model is not None,
        "sample_rate": SAMPLE_RATE,
    }


@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()
    logger.info("ws client connected")

    try:
        model = await _wait_model(timeout_s=120.0)
    except Exception as e:
        logger.error("Modelo no disponible: %s", e)
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
        await ws.close()
        return

    buffer = bytearray()
    last_voice_ts = time.time()
    speaking = False
    last_partial = ""

    max_buf_bytes = int(SAMPLE_RATE * 2 * MAX_BUFFER_SECS)  # int16 = 2 bytes

    async def emit_partial(text: str):
        nonlocal last_partial
        if text and text != last_partial:
            last_partial = text
            await ws.send_json({"type": "partial", "text": text})

    async def emit_final(text: str):
        nonlocal last_partial, speaking
        if text:
            await ws.send_json({"type": "final", "text": text})
        else:
            await ws.send_json({"type": "final", "text": ""})
        last_partial = ""
        speaking = False

    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=0.25)
            except asyncio.TimeoutError:
                #  Tick periodico: si hubo silencio, transcribimos
                if speaking and buffer:
                    idle = time.time() - last_voice_ts
                    if idle >= SILENCE_SECS:
                        audio = np.frombuffer(buffer, dtype=np.int16).astype(np.float32) / 32768.0
                        text = await _transcribe(model, audio)
                        #  BORRADO DEL BUFFER: ya no se necesita el audio
                        buffer.clear()
                        await emit_final(text)
                continue

            if msg["type"] == "websocket.disconnect":
                break

            if "bytes" in msg and msg["bytes"] is not None:
                chunk = msg["bytes"]
                buffer.extend(chunk)
                if len(buffer) > max_buf_bytes:
                    #  overflow: descartamos lo mas viejo
                    del buffer[: len(buffer) - max_buf_bytes]

                arr = np.frombuffer(chunk, dtype=np.int16)
                rms = _rms(arr)
                if rms >= SILENCE_RMS:
                    last_voice_ts = time.time()
                    speaking = True
                continue

            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    continue
                t = data.get("type")
                if t == "stop":
                    if buffer:
                        audio = np.frombuffer(buffer, dtype=np.int16).astype(np.float32) / 32768.0
                        text = await _transcribe(model, audio)
                        buffer.clear()  #  borrado inmediato
                        await emit_final(text)
                    else:
                        await emit_final("")
                elif t == "ping":
                    await ws.send_json({"type": "pong", "t": time.time()})

    except WebSocketDisconnect:
        logger.info("ws client disconnected")
    except Exception as e:
        logger.exception("ws error")
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        #  Garantia: el audio se va con la sesion. Cero persistencia.
        buffer.clear()
        logger.info("ws session closed, audio buffer purged")


def main():
    import uvicorn
    port = int(os.getenv("PORT", "8100"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
