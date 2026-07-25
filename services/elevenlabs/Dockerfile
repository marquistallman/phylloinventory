FROM python:3.11-slim

WORKDIR /app

#  Codigo compartido (mismo patron que el resto de servicios; no se usa
#  pero lo copiamos para que las utils del repo esten disponibles si en
#  el futuro se necesitan).
COPY services/llm_common/   /app/llm_common/
COPY services/elevenlabs/   /app/elevenlabs/

RUN pip install --no-cache-dir -r /app/elevenlabs/requirements.txt

ENV PYTHONPATH=/app
ENV PORT=8206

EXPOSE 8206
CMD ["python", "-u", "-m", "uvicorn", "elevenlabs.main:app", "--host", "0.0.0.0", "--port", "8206"]
