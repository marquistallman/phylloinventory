FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

#  El contexto de build es la raiz del proyecto (ver docker-compose).
COPY services/llm_common/  /app/llm_common/
COPY services/needle_svc/  /app/needle_svc/

RUN pip install --no-cache-dir -r /app/needle_svc/requirements.txt
RUN pip install --no-cache-dir git+https://github.com/cactus-compute/needle.git
RUN mkdir -p /app/checkpoints

#  Para evitar conflicto entre el paquete pip "needle" y nuestro modulo
#  "needle_svc", forzamos que la lib de cactus sea solo una dependencia y
#  que el servicio se cargue como needle_svc.main.
ENV CHECKPOINT_PATH=/app/checkpoints/needle.pkl
ENV PYTHONPATH=/app

EXPOSE 8081
CMD ["python", "-u", "-m", "uvicorn", "needle_svc.main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
