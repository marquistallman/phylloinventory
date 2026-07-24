FROM python:3.11-slim

WORKDIR /app

COPY services/llm_common/   /app/llm_common/
COPY services/api-gateway/  /app/api-gateway/

RUN pip install --no-cache-dir -r /app/api-gateway/requirements.txt

ENV PYTHONPATH=/app

EXPOSE 8200
CMD ["python", "-u", "-m", "uvicorn", "api-gateway.main:app", "--host", "0.0.0.0", "--port", "8200"]
