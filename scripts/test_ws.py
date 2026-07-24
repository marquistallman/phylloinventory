import asyncio
import json
import websockets

async def test():
    try:
        async with websockets.connect("ws://127.0.0.1:8100/ws/transcribe") as ws:
            print("connected")
            await ws.send(json.dumps({"type": "ping"}))
            r = await asyncio.wait_for(ws.recv(), timeout=3)
            print(f"got: {r}")
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}")

asyncio.run(test())
