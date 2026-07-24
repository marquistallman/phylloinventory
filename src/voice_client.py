"""Cliente WebSocket para el voice-service.

Captura audio del microfono local (sounddevice), lo envia en chunks
PCM int16 mono 16k al servicio, y recibe transcripciones. El audio
nunca se guarda en disco: los frames se transmiten y se descartan.
"""
from __future__ import annotations

import asyncio
import json
import sys

try:
    import sounddevice as sd  # type: ignore
    import websockets  # type: ignore
except ImportError:
    sd = None
    websockets = None


def available() -> bool:
    return sd is not None and websockets is not None


def check_voice_service(http_url: str) -> bool:
    """Chequeo rapido HTTP para confirmar que voice-service esta vivo antes
    de abrir el WebSocket. Devuelve True si responde /health.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(http_url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 100  # 100ms por chunk

#  Timeout generoso: la primera conexion puede tardar mientras Whisper
#  carga el modelo en el servidor.
WS_OPEN_TIMEOUT_S = 30.0


def _http_base_from_ws(ws_url: str) -> str:
    """ws://host:port/path -> http://host:port"""
    s = ws_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    #  Quita el /path
    from urllib.parse import urlparse
    u = urlparse(s)
    return f"{u.scheme}://{u.netloc}"


async def record_and_transcribe(ws_url: str, stop_event: asyncio.Event) -> str:
    """Envia audio del microfono al voice-service y devuelve la transcripcion final.

    `stop_event` debe ser .set() cuando el usuario quiera cortar (Enter).
    """
    if not available():
        raise RuntimeError("Faltan sounddevice y/o websockets. pip install sounddevice websockets")

    #  Pre-check: si ni el HTTP responde, ni intentamos el WebSocket
    base = _http_base_from_ws(ws_url)
    if not check_voice_service(f"{base}/health"):
        raise RuntimeError(
            f"voice-service no responde en {base}/health. "
            f"Asegurate de haberlo levantado con: docker compose --profile with-voice up -d"
        )

    frames_per_block = SAMPLE_RATE * BLOCK_MS // 1000

    try:
        ws = await websockets.connect(ws_url, open_timeout=WS_OPEN_TIMEOUT_S, max_size=2**23)
    except websockets.exceptions.InvalidStatus as e:
        raise RuntimeError(f"voice-service rechazo la conexion (status {e.response.status_code})")
    except websockets.exceptions.InvalidURI as e:
        raise RuntimeError(f"URL de WebSocket invalida: {ws_url} ({e})")
    except OSError as e:
        raise RuntimeError(f"No se pudo conectar a {ws_url}: {e}")

    try:
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def callback(indata, frame_count, time_info, status):
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
                f"No se pudo abrir el dispositivo de audio: {e}\n"
                f"  Comprueba que hay microfono disponible y permisos."
            )

        stream.start()

        final_text = ""
        try:
            sender = asyncio.create_task(_send_loop(ws, queue, stop_event))
            receiver = asyncio.create_task(_recv_loop(ws))
            done, _ = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receiver in done:
                #  El server emitio "final" por silencio (o un error).
                sender.cancel()
                final_text = receiver.result()  # propaga si fue {"type":"error"}
            else:
                #  El usuario corto con Enter: el {"type":"stop"} ya se envio
                #  y el server esta transcribiendo. HAY QUE ESPERAR el final;
                #  cancelar el receiver aqui perdia la transcripcion siempre.
                try:
                    final_text = await asyncio.wait_for(receiver, timeout=15.0)
                except asyncio.TimeoutError:
                    receiver.cancel()
                    final_text = ""
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        return final_text
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def _send_loop(ws, queue: asyncio.Queue, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            chunk = await asyncio.wait_for(queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        try:
            await ws.send(chunk)
        except Exception:
            break
    #  Avisar al server que cierre el utterance
    try:
        await ws.send(json.dumps({"type": "stop"}))
    except Exception:
        pass


async def _recv_loop(ws) -> str:
    """Recibe hasta el "final" del server o hasta que cierre la conexion.

    No se detiene con stop_event: tras un {"type":"stop"} el server tarda
    un rato en transcribir y el "final" llega DESPUES. Salir antes perdia
    la transcripcion en el flujo push-to-talk.
    """
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        except Exception:
            return ""
        if isinstance(msg, (bytes, bytearray)):
            continue
        try:
            data = json.loads(msg)
        except Exception:
            continue
        t = data.get("type")
        if t == "final":
            return data.get("text", "")
        elif t == "partial":
            text = data.get("text", "")
            if text:
                sys.stdout.write(f"\r  [voz parcial] {text}   ")
                sys.stdout.flush()
        elif t == "error":
            raise RuntimeError(data.get("message", "voice error"))
