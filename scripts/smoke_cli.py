"""Test rapido: arranca la CLI, captura salida, mata."""
import os
import subprocess
import sys
import threading
import time

env = os.environ.copy()
env["API_GATEWAY_URL"] = "http://127.0.0.1:8200"
env["VOICE_WS_URL"] = "ws://127.0.0.1:8100/ws/transcribe"

p = subprocess.Popen(
    [sys.executable, "-u", "-m", "src.cli"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=env,
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
)

def feed():
    time.sleep(3)
    try:
        p.stdin.write("salir\n".encode())
        p.stdin.flush()
    except Exception:
        pass

t = threading.Thread(target=feed, daemon=True)
t.start()

try:
    out, _ = p.communicate(timeout=15)
    sys.stdout.buffer.write(out[:3000])
    sys.stdout.buffer.write(b"\n")
except subprocess.TimeoutExpired:
    p.kill()
    print("CLI timeout, killed")

