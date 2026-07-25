FROM python:3.11-slim

WORKDIR /app

#  Paquete compartido (env_loader, fuzzy_search, nlu, db, narrator, etc).
#  Sin esto los servicios que importan from llm_common import ... fallan
#  con ModuleNotFoundError al arrancar.
COPY services/llm_common/  /app/llm_common/
COPY services/voice/      /app/voice/

RUN pip install --no-cache-dir -r /app/voice/requirements.txt

ENV PYTHONPATH=/app

EXPOSE 8100
CMD ["python", "-u", "-m", "uvicorn", "voice.main:app", "--host", "0.0.0.0", "--port", "8100"]
