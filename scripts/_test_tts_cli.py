"""Smoke test del flujo TTS de la CLI: lanza la misma coroutine que el
comando `tts <texto>`, reproduce el audio y verifica que llega al
output device."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import tts_client

async def main():
    text = " ".join(sys.argv[1:]) or "Hola, prueba de Kokoro funcionando."
    print(f">> tts '{text}'  (URL={tts_client.TTS_URL})")
    ok = await tts_client.speak(text)
    print(">> result:", "ok" if ok else "fail")
    sys.exit(0 if ok else 1)

asyncio.run(main())
