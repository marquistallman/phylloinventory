"""Cliente de voz: captura audio del microfono local (sounddevice) y lo
manda al gateway por HTTP. El gateway se encarga de rutear al backend
activo (whisper local WS o elevenlabs HTTP) segun el toggle.

Audio efimero: se graba a un archivo temporal, se envia, y se borra
inmediatamente. Nunca se persiste en disco mas alla del ciclo de
transcripcion.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Optional

#  Carga .env / .env.example. Es idempotente — no rompe si la CLI ya lo cargo.
from .env_loader import load_env
load_env()

try:
    import sounddevice as sd  # type: ignore
except ImportError:
    sd = None  # type: ignore

from . import api_client


def available() -> bool:
    """True si sounddevice esta instalado (suficiente para grabar)."""
    return sd is not None


SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 100  # 100ms por chunk
RECORD_SECS_MAX = 60  # tope de seguridad


def _check_microphone() -> None:
    if sd is None:
        raise RuntimeError(
            "Falta sounddevice para capturar audio. "
            "pip install sounddevice"
        )


async def record_and_transcribe(stop_event: asyncio.Event) -> str:
    """Graba del microfono, envia al gateway y devuelve la transcripcion.

    `stop_event` debe ser .set() cuando el usuario quiera cortar (Enter).
    La implementacion graba a un .wav temporal y lo postea al endpoint
    /api/audio/transcribir — el gateway decide si va a Eleven Labs
    (cloud) o a Whisper local (vuelve a convertir si hace falta).
    """
    _check_microphone()

    frames_per_block = SAMPLE_RATE * BLOCK_MS // 1000
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop_rec = asyncio.Event()

    def callback(indata, frame_count, time_info, status):
        if stop_rec.is_set():
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))
        except Exception:
            pass

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=frames_per_block,
            callback=callback,
        )
    except Exception as e:
        raise RuntimeError(
            f"No se pudo abrir el microfono: {e}\n"
            f"  Comprueba que hay microfono disponible y permisos."
        )

    stream.start()
    chunks: list[bytes] = []
    try:
        #  Drenamos la queue hasta que stop_event se setee o pasemos RECORD_SECS_MAX.
        async def collector():
            elapsed = 0.0
            while not stop_event.is_set() and elapsed < RECORD_SECS_MAX:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=0.2)
                    chunks.append(chunk)
                except asyncio.TimeoutError:
                    elapsed += 0.2
                    continue
                #  VAD muy basico: avisar al usuario cuando esta hablando
                import numpy as np
                arr = np.frombuffer(chunk, dtype=np.int16)
                if arr.size:
                    rms = float((arr.astype("float32") / 32768.0).std())
                    if rms > 0.01:
                        sys.stdout.write("\r  [grabando...   ] ")
                        sys.stdout.flush()
            stop_rec.set()

        await collector()
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    if not chunks:
        return ""

    #  Escribir WAV temporal (PCM 16k mono int16 — formato raw del microfono)
    wav_bytes = _pcm_chunks_to_wav(b"".join(chunks), SAMPLE_RATE, CHANNELS)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = tmp.name
    try:
        result = await api_client.transcribe_audio(tmp_path, content_type="audio/wav")
        return result.get("text", "")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _pcm_chunks_to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """Empaqueta PCM int16 LE en un contenedor WAV (sin librerias externas)."""
    import struct
    data_size = len(pcm)
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        16,  # bits per sample
        b"data",
        data_size,
    )
    return header + pcm


# =====================================================================
#  Compatibilidad: check_voice_service se mantiene por si la UI lo usa,
#  pero el health check ahora se hace contra el gateway directamente.
# =====================================================================

def check_voice_service(http_url: str) -> bool:
    """Deprecated: el gateway es ahora el unico punto de entrada. Usar
    api_client.health() para chequear todo el stack de una."""
    import urllib.request
    try:
        with urllib.request.urlopen(http_url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _http_base_from_ws(ws_url: str) -> str:
    """ws://host:port/path -> http://host:port  (compatibilidad)"""
    s = ws_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    from urllib.parse import urlparse
    u = urlparse(s)
    return f"{u.scheme}://{u.netloc}"
