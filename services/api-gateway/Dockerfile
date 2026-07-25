FROM python:3.11-slim

#  ffmpeg: el gateway lo necesita para decodificar uploads de audio (webm/mp3/wav)
#  a PCM 16k cuando el STT local (Whisper) es el backend activo. Tambien lo
#  usa el cliente CLI/PWA para que un mismo upload sirva para ambos paths
#  (cloud y local) sin pedirle a la UI que grabe en PCM crudo.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/llm_common/   /app/llm_common/
COPY services/api-gateway/  /app/api-gateway/

RUN pip install --no-cache-dir -r /app/api-gateway/requirements.txt

ENV PYTHONPATH=/app

EXPOSE 8200
CMD ["python", "-u", "-m", "uvicorn", "api-gateway.main:app", "--host", "0.0.0.0", "--port", "8200"]
