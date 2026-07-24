"""Smoke test: verifica que la cadena voice -> ws -> whisper funciona.

Sin microfono real (sounddevice abrira el default device), asi que
probamos solo el handshake y el protocolo ping/pong. La transcripcion
completa requiere hardware de audio.
"""
import asyncio
import json
from src.voice_client import (
    record_and_transcribe,
    check_voice_service,
    _http_base_from_ws,
    available,
)


async def test_handshake():
    if not available():
        print("FAIL: sounddevice/websockets no disponibles")
        return False
    base = _http_base_from_ws("ws://127.0.0.1:8100/ws/transcribe")
    ok = check_voice_service(f"{base}/health")
    print(f"pre-check HTTP /health: {'OK' if ok else 'FAIL'}")
    if not ok:
        return False
    try:
        import websockets
        async with websockets.connect(
            "ws://127.0.0.1:8100/ws/transcribe",
            open_timeout=30.0,
        ) as ws:
            await ws.send(json.dumps({"type": "ping"}))
            r = await asyncio.wait_for(ws.recv(), timeout=5)
            print(f"WS ping/pong: OK ({r})")
        return True
    except Exception as e:
        print(f"WS FAIL: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    asyncio.run(test_handshake())
