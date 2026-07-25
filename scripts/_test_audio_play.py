"""Test directo: reproduce el PCM de kokoro en sounddevice sin pasar por HTTP."""
import os, sys, numpy as np
import sounddevice as sd

path = os.path.join(os.environ["TEMP"], "kokoro_test.pcm")
if not os.path.exists(path):
    print("No hay PCM previo, genera uno primero con scripts/_test_tts.py")
    sys.exit(1)

raw = open(path, "rb").read()
samples = np.frombuffer(raw, dtype=np.int16)
print(f"{len(samples)} samples ({len(samples)/24000:.2f}s @ 24kHz)")

sd.play(samples, samplerate=24000, blocking=True)
print("OK reproduccion completada")
