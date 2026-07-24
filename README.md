# Cactus Inventory - Microservicios

Demo de manejo de inventarios con Filtro de Kalman, refactorizado a una
**arquitectura de microservicios** donde cada pieza corre en Docker y se
comunica por HTTP/WS. La logica Kalman la ejecuta un **worker en Go** que
consume una **cola en PostgreSQL**.

## Arquitectura

```
              ┌──────────────────────────┐
              │  CLI (Python / Rich)     │  cliente delgado
              │  texto o voz (WS)        │
              └────────────┬─────────────┘
                           │ HTTP
       ┌───────────────────┼────────────────────────┐
       │ WS (audio chunks) │                        │
       ▼                   ▼                        ▼
┌──────────────┐  ┌────────────────────┐  ┌──────────────────────┐
│ voice-service│  │  api-gateway       │  │  status / health     │
│ Whisper + WS │  │  HTTP :8200        │  │  /status/{id}        │
│ :8100        │  │  LLM_BACKEND route │  └──────────────────────┘
└──────────────┘  └────────┬───────────┘
                           │ HTTP /infer
              ┌────────────┴─────────────┐
              ▼                          ▼
      ┌──────────────────┐       ┌──────────────────┐
      │ needle-service   │       │ openrouter-svc   │
      │ Python+FastAPI   │       │ Python+FastAPI   │
      │ :8081            │       │ :8082            │
      │ modelo 26M       │       │ API key externa  │
      │ asyncio multi-   │       │ (perfil)         │
      │ worker           │       │                  │
      └────────┬─────────┘       └────────┬─────────┘
               │ enqueue                  │
               └──────────┬───────────────┘
                          │ SQL INSERT pending_evaluations
                          ▼
              ┌────────────────────────────┐
              │ PostgreSQL :5432           │
              │  - productos               │
              │  - inventario_movimientos  │
              │  - auditoria_log           │
              │  - pending_evaluations  ◀── cola (sin triggers de data)
              │  - kalman_evaluar()        │
              │  - aplicar_movimiento_*()  │
              │  - confirmar_movimiento()  │
              └─────────────┬──────────────┘
                            │ SELECT ... FOR UPDATE SKIP LOCKED
                            │ (8 goroutines, polling 200ms)
                            ▼
              ┌────────────────────────────┐
              │ kalman-worker (Go)         │
              │ - lee pendientes           │
              │ - llama kalman_evaluar()   │
              │ - si PASA: aplica          │
              │ - si FALLA: marca SOSPECH. │
              │ - health/stats :8300       │
              └────────────────────────────┘

(futuro) tts-service: texto -> audio, WebSocket,
mismo principio "audio se procesa, audio se borra"
```

## Caracteristicas clave

- **Cero triggers de data en la DB.** Toda la logica Kalman vive en el
  worker Go. La DB expone solo funciones puras (`kalman_evaluar`,
  `aplicar_movimiento_aceptado`, `confirmar_movimiento`).
- **Cola en PostgreSQL** con `SELECT ... FOR UPDATE SKIP LOCKED` +
  `LISTEN/NOTIFY` como acelerador.
- **Paralelismo** en el worker: pool de N goroutines configurable
  (`KALMAN_WORKERS`).
- **Dos backends LLM** seleccionables por env (`LLM_BACKEND=needle|openrouter`).
- **Audio efimero**: el voice-service WebSocket nunca persiste audio a
  disco. Los chunks se procesan en un buffer circular y se descartan al
  transcribir.
- **CLI como cliente delgado** que solo habla HTTP/WS con el api-gateway.

## Requisitos

- Docker Desktop con `docker compose` (v2+)
- Python 3.10+ para la CLI (no necesita GPU)
- ~3 GB para las imagenes (needle pesa ~470 MB)

## Quick start

### 1. Levantar el stack minimo (needle + kalman + postgres + api-gateway)

```bash
docker compose up -d
```

La primera vez, needle descargara el modelo (~30s). Espera a ver:

```bash
docker logs -f cactus_needle
# 2026-07-24 ... INFO Needle cargado.
# INFO:     Application startup complete.
```

### 2. (Opcional) Activar voz

```bash
docker compose --profile with-voice up -d
```

Whisper `small` (~460 MB) se descarga la primera vez.

### 3. (Opcional) Activar OpenRouter como backend

```bash
export OPENROUTER_API_KEY=sk-or-...
export LLM_BACKEND=openrouter
docker compose --profile with-openrouter --profile with-voice up -d
```

### 4. Instalar la CLI (cliente)

```bash
pip install -r requirements.txt
```

Si quieres voz, asegurate de tener `sounddevice`:

```bash
pip install sounddevice
```

### 5. Ejecutar la CLI

```bash
python -m src.cli
```

Comandos disponibles:

```
texto libre             -> enviar al LLM
voz                     -> dictar por microfono (WS)
inventario              -> ver stock
sospechosos [producto]  -> auditoria Kalman
salir / exit / q        -> cerrar
ayuda / help            -> mostrar el banner
limpiar / clear         -> limpiar pantalla
```

## Endpoints del api-gateway (puerto 8200)

| Metodo | Path                  | Descripcion                              |
|--------|-----------------------|------------------------------------------|
| GET    | /health               | Estado de backends LLM y DB              |
| POST   | /query                | `{"text": "...", "session_id": "..."}`   |
| GET    | /status/{pending_id}  | Estado de un pending en la cola          |
| GET    | /inventory?producto=X | Stock actual                             |
| GET    | /sospechosos?producto=X | Top movimientos sospechosos           |

## Variables de entorno

### CLI

| Variable             | Default                     | Descripcion                        |
|----------------------|-----------------------------|------------------------------------|
| `API_GATEWAY_URL`    | `http://127.0.0.1:8200`     | URL del api-gateway                |
| `VOICE_WS_URL`       | `ws://127.0.0.1:8100/ws/...`| URL WebSocket del voice-service    |
| `CLI_POLL_TIMEOUT`   | `15`                        | Segundos max para esperar pending  |
| `CLI_POLL_INTERVAL`  | `0.2`                       | Segundos entre polls               |

### api-gateway

| Variable         | Default                          | Descripcion                       |
|------------------|----------------------------------|-----------------------------------|
| `LLM_BACKEND`    | `needle`                         | `needle` o `openrouter`           |
| `NEEDLE_URL`     | `http://needle-service:8081`     | URL del needle-service            |
| `OPENROUTER_URL` | `http://openrouter-service:8082` | URL del openrouter-service        |
| `DATABASE_URL`   | `postgres://...postgres:5432...` | DSN PostgreSQL                    |

### kalman-worker

| Variable          | Default        | Descripcion                              |
|-------------------|----------------|------------------------------------------|
| `DATABASE_URL`    | `postgres://...` | DSN PostgreSQL                          |
| `KALMAN_WORKERS`  | `runtime.NumCPU() * 2` | Numero de goroutines worker     |
| `POLL_INTERVAL`   | `200ms`        | Intervalo de poll a la cola              |
| `HTTP_ADDR`       | `:8300`        | Puerto del health/stats                  |

### needle-service

| Variable             | Default                        | Descripcion                  |
|----------------------|--------------------------------|------------------------------|
| `CHECKPOINT_PATH`    | `/app/checkpoints/needle.pkl`  | Ruta del modelo              |
| `MAX_INFER_INFLIGHT` | `4`                            | Concurrencia maxima          |
| `DATABASE_URL`       | `postgres://...`               | Para encolar pending         |

### openrouter-service

| Variable                | Default                              | Descripcion              |
|-------------------------|--------------------------------------|--------------------------|
| `OPENROUTER_API_KEY`    | (vacio)                              | API key de OpenRouter    |
| `OPENROUTER_BASE`       | `https://openrouter.ai/api/v1`       | Endpoint base            |
| `OPENROUTER_MODEL`      | `anthropic/claude-3.5-haiku`         | Modelo a usar            |
| `DATABASE_URL`          | `postgres://...`                     | Para encolar pending     |

### voice-service

| Variable              | Default | Descripcion                         |
|-----------------------|---------|-------------------------------------|
| `WHISPER_MODEL`       | `small` | Tamano del modelo Whisper           |
| `WHISPER_LANGUAGE`    | `es`    | Idioma                              |
| `VOICE_SILENCE_RMS`   | `0.01`  | Umbral RMS para detectar silencio   |
| `VOICE_SILENCE_SECS`  | `0.8`   | Segundos de silencio para transcribir |
| `VOICE_MIN_UTTERANCE` | `0.5`   | Minimo de audio para transcribir    |
| `VOICE_MAX_BUFFER`    | `30`    | Maximo de segundos de buffer        |

## Estructura del proyecto

```
storage/
├── docker-compose.yml            # postgres, kalman, needle, api-gateway (+voice, +openrouter)
├── db/
│   └── init.sql                  # Schema + funciones puras (sin triggers de data)
├── kalman-worker/                # Servicio Go (goroutine pool, cola)
│   ├── Dockerfile
│   ├── go.mod
│   └── main.go
├── services/
│   ├── llm_common/               # Modulo compartido (db pool, schemas de tools)
│   │   ├── __init__.py
│   │   ├── db.py                 # asyncpg + helpers de la cola
│   │   └── schemas.py            # tool schemas
│   ├── needle_svc/               # needle-service: 26M params + enqueue
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   ├── openrouter/               # openrouter-service: proxy a OpenRouter
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   ├── api-gateway/              # Punto de entrada HTTP para la CLI
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   └── voice/                    # voice-service: WebSocket + faster-whisper
│       ├── Dockerfile
│       ├── requirements.txt
│       └── main.py
├── src/                          # CLI cliente delgado
│   ├── __init__.py
│   ├── cli.py
│   ├── api_client.py             # httpx -> api-gateway
│   └── voice_client.py           # websockets -> voice-service
├── scripts/                      # SQL de prueba, smoke tests
└── requirements.txt              # Dependencias de la CLI
```

## Flujo end-to-end

1. Usuario teclea `agregar 3 cebollas` o habla `voz` y lo dice.
2. CLI hace `POST /query` al api-gateway.
3. api-gateway forwardea a needle-service u openrouter-service segun
   `LLM_BACKEND`.
4. LLM service parsea el tool_call y hace `INSERT INTO pending_evaluations`.
5. kalman-worker (Go, 8 goroutines) toma la fila con `FOR UPDATE SKIP LOCKED`.
6. Llama a `SELECT * FROM kalman_evaluar(...)` (funcion pura).
7. Si `PASA` -> `SELECT aplicar_movimiento_aceptado(...)` y marca
   `status=ACEPTADA`.
8. Si `FALLA` -> marca `status=SOSPECHOSA` con residual/umbral.
9. CLI hace `GET /status/{id}` cada 200ms hasta ver resolucion.
10. Si `SOSPECHOSA`, la CLI muestra el panel Kalman. El usuario responde
    `si/no`, el LLM emite `confirmar_movimiento` con `pending_id`, vuelve
    a encolarse, y el worker llama a `confirmar_movimiento(pending_id,
    bool)` que aplica (`CONFIRMADA_MANUAL`) o descarta (`RECHAZADA`).

## Operaciones utiles

```bash
# Ver estado de todos los servicios
docker compose ps

# Logs del kalman-worker
docker logs -f cactus_kalman_worker

# Insertar un pending de prueba directamente
docker exec -i cactus_postgres psql -U cactus -d inventario < scripts/seed_test.sql

# Ver ultimas 10 evaluaciones
docker exec cactus_postgres psql -U cactus -d inventario -c \
  "SELECT id, session_id, tool_name, status, decision, residual, movimiento_id FROM pending_evaluations ORDER BY id DESC LIMIT 10;"

# Auditar movimientos sospechosos
docker exec cactus_postgres psql -U cactus -d inventario -c \
  "SELECT * FROM investigar_sospechosos();"

# Probar la API manualmente
curl -s http://127.0.0.1:8200/health
curl -s http://127.0.0.1:8200/inventory
curl -s -X POST http://127.0.0.1:8200/query \
  -H "Content-Type: application/json" \
  -d '{"text":"agregar inventario 5 unidades de papa","session_id":"manual"}'
```

## Filtro de Kalman - Como funciona

1. **Prediccion**: `P_pred = P + Q`
2. **Medicion**: `z = stock_actual + delta_reportado`
3. **Innovacion**: `y = z - mu`
4. **Decision**: Si `|y| > 2.0 * sqrt(P_pred + R)` -> `FALLA`
5. **Actualizacion (solo si PASA)**: `K = P_pred / (P_pred + R)`,
   `mu_new = mu + K * y`, `P_new = (1 - K) * P_pred`

## Roadmap

- [ ] `tts-service` para responder con voz (mismo principio "audio efimero")
- [ ] SSE/WebSocket push en api-gateway para evitar polling
- [ ] Multi-tenant / autenticacion
- [ ] Metricas Prometheus (counters ya estan en `/stats` del kalman-worker)
- [ ] Kubernetes manifests

## Licencia

MIT
