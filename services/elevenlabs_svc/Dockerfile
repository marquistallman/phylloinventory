FROM python:3.11-slim

WORKDIR /app
COPY services/elevenlabs_svc/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/elevenlabs_svc/ ./elevenlabs_svc/
COPY services/llm_common/ ./llm_common/

ENV PYTHONPATH=/app
EXPOSE 8206

CMD ["uvicorn", "elevenlabs_svc.main:app", "--host", "0.0.0.0", "--port", "8206"]