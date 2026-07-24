@echo off
set API_GATEWAY_URL=http://127.0.0.1:8200
set VOICE_WS_URL=ws://127.0.0.1:8100/ws/transcribe
python -m src.cli
