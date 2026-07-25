FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

#  El contexto de build es la raiz del proyecto (ver docker-compose).
COPY services/llm_common/  /app/llm_common/
COPY services/needle_svc/  /app/needle_svc/

#  Pin a un commit concreto: pip + git+https consulta PyPI antes de clonar
#  y con el default (15s) suele hacer ReadTimeoutError. Subimos timeout,
#  agregamos retries y clonamos primero para evitar la consulta a PyPI.
ARG NEEDLE_COMMIT=6fdddb874dd6f46dabeea88d0bf239fc4a2474c4

RUN pip install --no-cache-dir --default-timeout=300 --retries 5 \
    -r /app/needle_svc/requirements.txt

RUN git clone --depth 1 https://github.com/cactus-compute/needle.git /tmp/needle \
 && cd /tmp/needle \
 && git fetch --depth 1 origin ${NEEDLE_COMMIT} \
 && git checkout ${NEEDLE_COMMIT} \
 && pip install --no-cache-dir --default-timeout=300 --retries 5 /tmp/needle \
 && rm -rf /tmp/needle

RUN mkdir -p /app/checkpoints

#  Para evitar conflicto entre el paquete pip "needle" y nuestro modulo
#  "needle_svc", forzamos que la lib de cactus sea solo una dependencia y
#  que el servicio se cargue como needle_svc.main.
ENV CHECKPOINT_PATH=/app/checkpoints/needle.pkl
ENV PYTHONPATH=/app

EXPOSE 8081
CMD ["python", "-u", "-m", "uvicorn", "needle_svc.main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
