FROM python:3.11-slim

# espeak-ng: backend de fonemas para Kokoro (es/en/fr/it/pt/zh/ja)
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

#  Codigo compartido (mismo patron que el resto de servicios)
COPY services/llm_common/  /app/llm_common/
COPY services/kokoro_svc/  /app/kokoro_svc/

#  Kokoro depende de torch. En CPU no necesitamos CUDA (eso mete ~3GB
#  de nvidia-* wheels), asi que instalamos torch desde el index CPU-only
#  ANTES de resolver las deps de kokoro.
RUN pip install --no-cache-dir --default-timeout=300 --retries 5 \
        --index-url https://download.pytorch.org/whl/cpu \
        torch
RUN pip install --no-cache-dir --default-timeout=300 --retries 5 \
        -r /app/kokoro_svc/requirements.txt

#  Defaults (override por env en docker-compose)
ENV PYTHONPATH=/app
ENV LANG_CODE=e
ENV VOICE=ef_dora
ENV SPEED=1.0
ENV PORT=8205

EXPOSE 8205
CMD ["python", "-u", "-m", "uvicorn", "kokoro_svc.main:app", \
     "--host", "0.0.0.0", "--port", "8205", "--workers", "1"]
