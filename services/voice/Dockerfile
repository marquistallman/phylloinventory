FROM python:3.11-slim

WORKDIR /app

COPY services/voice/ /app/voice/

RUN pip install --no-cache-dir -r /app/voice/requirements.txt

ENV PYTHONPATH=/app

EXPOSE 8100
CMD ["python", "-u", "-m", "uvicorn", "voice.main:app", "--host", "0.0.0.0", "--port", "8100"]
