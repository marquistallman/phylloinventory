"""Cliente TTS: hace POST /speak al kokoro-service y reproduce el audio
streamed con sounddevice. La reproduccion corre en un thread aparte para
no bloquear el event loop de la CLI.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

import httpx
import numpy as np

#  Carga .env / .env.example para KOKORO_URL y DISABLE_TTS.
from .env_loader import load_env
load_env()

try:
    import sounddevice as sd  # type: ignore
except ImportError:
    sd = None  # type: ignore

#  Logging: que se vea en consola cuando algo va mal, no solo en debug.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("tts-client")

#  Cancelacion: cada TTS nueva interrumpe a la anterior. Asi el usuario
#  siempre escucha lo mas reciente, sin colas que se acumulan.
_cancel_event: threading.Event = threading.Event()
_play_lock = threading.Lock()

TTS_URL = os.getenv("KOKORO_URL", "http://127.0.0.1:8205")
SAMPLE_RATE = 24000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 100  #  chunk de salida de 100ms


def _audio_available() -> bool:
    return sd is not None


async def speak(text: str, voice: Optional[str] = None, speed: Optional[float] = None) -> bool:
    """Envia texto al TTS y reproduce en stream. Devuelve True si sonó.

    Si sounddevice no esta disponible o el servicio no responde, devuelve
    False y no rompe el flujo.
    """
    if not text or not text.strip():
        return False
    if not _audio_available():
        logger.debug("sounddevice no disponible, TTS skip")
        return False

    #  Lo mandamos a un thread aparte para no bloquear el event loop.
    #  Dentro del thread, se hace la peticion HTTP con httpx (sync) y se
    #  va reproduciendo por chunks.
    return await asyncio.get_event_loop().run_in_executor(
        None, _speak_blocking, text, voice, speed
    )


def _speak_blocking(text: str, voice: Optional[str], speed: Optional[float]) -> bool:
    """Reproduce TTS. Si ya hay otra sonando, la interrumpe (cancela su
    reproduccion) y arranca la nueva. Asi no se solapan audios y el
    usuario siempre oye lo mas reciente."""
    global _cancel_event
    with _play_lock:
        #  Avisar al hilo anterior (si lo hay) que pare.
        _cancel_event.set()
        #  Y usar un evento NUEVO para esta reproduccion.
        _cancel_event = threading.Event()
        my_cancel = _cancel_event
    return _speak_with_cancel(text, voice, speed, my_cancel)


def _speak_with_cancel(text: str, voice: Optional[str], speed: Optional[float], cancel: threading.Event) -> bool:
    payload: dict = {"text": text}
    if voice:
        payload["voice"] = voice
    if speed is not None:
        payload["speed"] = speed

    blocksize = SAMPLE_RATE * BLOCK_MS // 1000
    stream = None
    try:
        try:
            resp = httpx.post(
                f"{TTS_URL}/speak",
                json=payload,
                timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
            )
        except Exception as e:
            logger.warning("TTS HTTP fail: %s", e)
            return False

        if resp.status_code != 200:
            logger.warning("TTS status %d: %s", resp.status_code, resp.text[:200])
            return False

        try:
            stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=blocksize,
            )
            stream.start()
        except Exception as e:
            logger.warning("No se pudo abrir audio output: %s", e)
            return False

        #  Iteramos por chunks. Si cancel.is_set() => nos interrumpieron.
        total_bytes = 0
        for chunk in resp.iter_bytes(chunk_size=4096):
            if cancel.is_set():
                logger.info("TTS interrumpido por nueva reproduccion")
                return False
            if not chunk:
                continue
            samples = np.frombuffer(chunk, dtype=np.int16)
            if samples.size:
                stream.write(samples)
            total_bytes += len(chunk)
        logger.info("TTS reprodujo %.2fs (%d bytes) a %s", total_bytes / 2 / SAMPLE_RATE, total_bytes, TTS_URL)
        return True

    except Exception as e:
        logger.warning("TTS playback error: %s", e)
        return False
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


async def is_available(timeout_s: float = 2.0) -> bool:
    """Ping rapido al servicio. Si no responde, el caller sabe que no hay TTS."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(f"{TTS_URL}/health")
            return r.status_code == 200
    except Exception:
        return False


async def play_pcm(pcm_bytes: bytes, sample_rate: int = 24000) -> bool:
    """Reproduce un buffer PCM int16 LE ya recibido (sin pasar por HTTP).

    Usado por el comando `tts` de la CLI cuando el gateway devuelve el
    audio (puede venir de kokoro o elevenlabs, indistinguible: ambos
    devuelven PCM int16 LE mono al sample rate que indiquen los headers).
    """
    if not _audio_available() or not pcm_bytes:
        return False
    return await asyncio.get_event_loop().run_in_executor(
        None, _play_pcm_blocking, pcm_bytes, sample_rate,
    )


def _play_pcm_blocking(pcm_bytes: bytes, sample_rate: int) -> bool:
    global _cancel_event
    with _play_lock:
        _cancel_event.set()
        _cancel_event = threading.Event()
        my_cancel = _cancel_event
    return _speak_with_cancel_bytes(pcm_bytes, sample_rate, my_cancel)


def _speak_with_cancel_bytes(pcm_bytes: bytes, sample_rate: int, cancel: threading.Event) -> bool:
    blocksize = sample_rate * BLOCK_MS // 1000
    stream = None
    try:
        try:
            stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=blocksize,
            )
            stream.start()
        except Exception as e:
            logger.warning("No se pudo abrir audio output: %s", e)
            return False
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        if samples.size:
            #  Cortar en bloques de blocksize para poder chequear cancel
            for i in range(0, samples.size, blocksize):
                if cancel.is_set():
                    return False
                stream.write(samples[i:i + blocksize])
        logger.info("TTS reprodujo %d muestras a %dHz", samples.size, sample_rate)
        return True
    except Exception as e:
        logger.warning("TTS playback error: %s", e)
        return False
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
