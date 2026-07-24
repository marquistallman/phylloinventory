FROM python:3.11-slim

WORKDIR /app

COPY services/llm_common/  /app/llm_common/
COPY services/openrouter/  /app/openrouter/

RUN pip install --no-cache-dir -r /app/openrouter/requirements.txt

ENV PYTHONPATH=/app

EXPOSE 8082
CMD ["python", "-u", "-m", "uvicorn", "openrouter.main:app", "--host", "0.0.0.0", "--port", "8082"]
