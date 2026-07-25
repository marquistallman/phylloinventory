# Phylloinventory — Documentacion de Implementacion

## Estado: Fases 1-9 completadas (backend listo para frontend)

---

## Arquitectura Actual (Microservicios)

```
┌──────────┐  HTTP :8200   ┌──────────────┐  HTTP :8081   ┌──────────────────┐
│  CLI     │──────────────→│ api-gateway  │──────────────→│ needle-service   │
│ (thin)   │←──────────────│ (FastAPI)    │←──────────────│ (26M params)     │
│          │               │              │               │ L1→L2→L3+enqueue │
│  voz WS  │               │ GET /inventory              └────────┬─────────┘
│  :8100   │               │ GET /sospechosos                     │ INSERT
└────┬─────┘               │ GET /status/{id}                     ▼
     │                     │ +10 endpoints nuevos  ┌──────────────────────────┐
     ▼ WebSocket           └────────┬──────────────│  pending_evaluations     │
┌──────────────┐                    │              │  + sesiones_conteo       │
│ voice-service│                    │              │  + registros_conteo      │
│ (Whisper)    │                    │              │  + bodegas               │
└──────────────┘                    │              │  + productos (1,407)     │
                                    │              └────────┬─────────────────┘
                                    │                       │ poll/NOTIFY
                                    │              ┌────────▼─────────────────┐
                                    │              │ kalman-worker (Go)       │
                                    │              │ 8 goroutines             │
                                    │              │ FOR UPDATE SKIP LOCKED   │
                                    │              │ +sync registros_conteo   │
                                    │              └──────────────────────────┘
                                    ▼
                              ┌──────────┐
                              │PostgreSQL│
                              │(funciones│
                              │Kalman)   │
                              └──────────┘
```

### Servicios Docker (8 containers)

| Servicio | Puerto | Perfil | Rol |
|---|---|---|---|
| `postgres` | 5432 | default | DB + cola + funciones Kalman puras |
| `kalman-worker` | 8300 | default | Go, consume `pending_evaluations`, 8 goroutines |
| `needle-service` | 8081 | default | Needle 26M, L1→L2→L3 pipeline, fuzzy search |
| `api-gateway` | 8200 | default | Router central, 19 endpoints, toggle cloud runtime |
| `kokoro-service` | 8205 | default | Kokoro 82M TTS, streaming PCM 24k por oracion |
| `openrouter-service` | 8082 | with-openrouter | Alternativa: DeepSeek V4 Flash via OpenRouter ($0.09/M in) |
| `voice-service` | 8100 | with-voice | Whisper local STT via WebSocket |
| `elevenlabs-service` | 8206 | with-elevenlabs | Eleven Labs STT + TTS cloud (scribe_v1 + eleven_multilingual_v2) |

---

## Cambios Realizados (12 archivos)

### 1. `db/init.sql` — Schema completo

**De:** 321 lineas, 3 tablas (productos, inventario_movimientos, auditoria_log), 5 productos semilla, funciones Kalman con INTEGER.
**A:** 363 lineas, 7 tablas, FLOAT en todo el pipeline.

**Cambios:**

| Linea | Cambio |
|---|---|
| 7-16 | Nueva tabla `bodegas` con seed `bodega_default` (id=1) |
| 18-33 | `productos`: agregados `bodega_id` (FK), `codigo_articulo`, `unidad`. `stock_actual` y `media_kalman` → FLOAT. UNIQUE(nombre, bodega_id) |
| 35-46 | `inventario_movimientos`: `cantidad_reportada` y `stock_resultante` → FLOAT |
| 61-79 | `pending_evaluations`: `cantidad` → FLOAT |
| 87-109 | Nuevas tablas `sesiones_conteo` y `registros_conteo` con FK a bodegas, productos, inventario_movimientos, pending_evaluations |
| 126-145 | `kalman_evaluar()`: parametro `p_cantidad FLOAT`, retorno `stock_proyectado FLOAT`, variable `nuevo_stock FLOAT` |
| 195-243 | `aplicar_movimiento_aceptado()`: parametro `p_cantidad FLOAT`, `nuevo_stock FLOAT` |
| 248-277 | `investigar_sospechosos()`: retorno `cantidad_reportada FLOAT` |
| 284-345 | `confirmar_movimiento()`: variable `ns FLOAT` |

### 2. `src/importer.py` — Importador Excel (nuevo, 190 lineas)

Lee `BODEGAS Y STOCK.xlsx` (9 hojas) con `openpyxl`:
- Hoja "BODEGAS DISPONIBLES" → 48 bodegas
- 8 hojas de stock → ~1,407 productos con `Nr.Articulo`, `Articulo`, `Unidad`, `SD`
- `SD` → `stock_actual` y `media_kalman` inicial
- `ON CONFLICT (nombre, bodega_id) DO UPDATE` para re-ejecucion segura
- Normaliza unidades: "kilos"→"Kilogram", "litros"→"Liter", etc.

Uso: `python -m src.importer [--excel PATH] [--dsn DSN]`

### 3. `services/llm_common/fuzzy_search.py` — Fuzzy matching (nuevo, 68 lineas)

- `fuzzy_match_product(query, candidates, threshold=75)` → mejor match via `rapidfuzz.token_sort_ratio`
- `fuzzy_search_candidates(query, candidates, limit=10)` → top-N matches
- Busqueda limitada a la bodega activa (~50-350 productos)

### 4. `services/llm_common/nlu.py` — NLU determinista (reescrito, 230 lineas)

**Antes:** 108 lineas, 5 productos hardcodeados, sin unidades.
**Ahora:** 230 lineas, catalogo dinamico, unidades, fast path regex.

**Nuevas funciones:**

| Funcion | Proposito |
|---|---|
| `parse_conteo_rapido(texto)` | Regex para "5 kilos de harina" → {cantidad, unidad, producto} en <1ms |
| `extract_unidad(texto)` | Extrae unidad del texto ("kilos"→"Kilogram") |
| `normalize_unidad(cantidad, unidad_usuario, unidad_catalogo)` | Convierte 500g→0.5kg, 2L→2Liter |
| `normalize_producto(val, producto_nombres)` | Match contra set dinamico de nombres |
| `extract_producto(query, producto_nombres)` | Busca nombres de producto en query |
| `get_producto_nombres_from_candidates(candidates)` | Extrae set de nombres de una lista de dicts |
| `get_unidad_from_candidates(candidates, nombre)` | Devuelve unidad del catalogo |
| `normalize_args(name, args, query, producto_nombres)` | Sanea argumentos del LLM, ahora extrae unidad |

**Funciones mantenidas:** `parse_confirmacion()`, `build_alert_context()`

### 5. `services/llm_common/db.py` — Capa de datos (reescrito, 280 lineas)

**Nuevas funciones:**

| Funcion | Proposito |
|---|---|
| `find_producto_fuzzy(query, bodega_id)` | Busca producto con fuzzy matching en una bodega |
| `get_catalogo_bodega(bodega_id, q, solo_pendientes, sesion_id)` | Catalogo con estado de conteo (pendiente/contado/alerta) |
| `get_producto_nombres_bodega(bodega_id)` | Lista de {id, nombre, unidad} para NLU |
| `enqueue_pending(session_id, tool_name, arguments, bodega_id)` | Encola a pending_evaluations con fuzzy search + normalizacion de unidad + crea registros_conteo |
| `enqueue_registro_manual(session_id, producto_id, cantidad, unidad)` | Encola directo sin LLM (modo Tablet) + crea registros_conteo |
| `get_pending_status(pending_id)` | Estado de un pending individual |

### 6. `services/llm_common/schemas.py` — Tool schemas (actualizado)

- `agregar_inventario` y `remover_inventario`: parametro `cantidad` → `number` (acepta decimales), nuevo parametro opcional `unidad`
- `TOOLS_OPENAI`: mismo cambio en formato OpenAI function calling

### 7. `services/needle_svc/main.py` — Needle service (actualizado)

- `InferRequest`: nuevo campo `bodega_id: int | None`
- `/infer` endpoint: si `bodega_id` existe, carga catalogo de la bodega y lo pasa al NLU para mejor extraccion de producto
- `_run_inference()`, `_pipeline()`, `_is_suspicious()`, `_confirmacion_fast_path()`: aceptan `producto_nombres` como parametro
- `normalize_args()` y `extract_producto()` reciben catalogo real
- `enqueue_pending()` recibe `bodega_id` para fuzzy search

### 8. `services/api-gateway/main.py` — API Gateway (177→522 lineas)

**Endpoints existentes mantenidos:** `/health`, `/query`, `/status/{pending_id}`, `/inventory`, `/sospechosos`

**Nuevos endpoints (10):**

| Method | Path | Proposito |
|---|---|---|
| POST | `/api/sesion/iniciar` | Crea sesion de conteo en una bodega |
| POST | `/api/sesion/finalizar` | Cierra sesion, retorna stats (contados/alertas/pendientes) |
| GET | `/api/sesion/{id}/estado` | Progreso en tiempo real |
| POST | `/api/sesion/registrar-manual` | Modo Tablet: producto_id + cantidad → cola Kalman |
| POST | `/api/sesion/registrar-voz` | Modo Voz: texto → regex fast path o LLM → cola Kalman |
| GET | `/api/catalogo/bodega/{id}` | Productos con stock_sistema y estado conteo |
| GET | `/api/reporte/diferencias/{id}` | Comparativo contado vs sistema + no contados |
| GET | `/api/reporte/sospechosos/{id}` | Alertas Kalman con residuales |
| GET | `/api/pending/{id}` | Estado individual de un pending |

`QueryRequest`: nuevo campo `bodega_id: int | None`.

### 9. `kalman-worker/main.go` — Worker Go (actualizado)

| Linea | Cambio |
|---|---|
| 75-83 | `Pending.Cantidad`: `int32` → `float64` |
| 89-97 | `KalmanResult.StockProyectado`: `int32` → `float64` |
| 366-370 | `evalMovimiento` caso PASA: sincroniza `registros_conteo.decision_kalman='ACEPTADA'` |
| 380-385 | `evalMovimiento` caso FALLA: sincroniza `registros_conteo.decision_kalman='SOSPECHOSA'` |
| 447-457 | `evalConfirmacion`: sincroniza `registros_conteo` (CONFIRMADA_MANUAL o RECHAZADA) segun resultado |

### 10-12. Dependencias

| Archivo | Cambio |
|---|---|
| `requirements.txt` | +openpyxl, +psycopg2-binary |
| `services/needle_svc/requirements.txt` | +rapidfuzz |
| `services/api-gateway/requirements.txt` | +rapidfuzz |
| `services/openrouter/requirements.txt` | +rapidfuzz |

---

## Flujo de Datos End-to-End

### Modo Tablet (registro manual)

```
PWA → POST /api/sesion/iniciar {bodega_id: 3, iniciada_por: "Carlos"}
    ← {sesion_id: 1, total_productos: 297}

PWA → GET /api/catalogo/bodega/3?sesion_id=1
    ← [{id: 42, nombre: "HARINA DE TRIGO", unidad: "Kilogram",
        stock_sistema: 125.5, estado_conteo: "pendiente"}, ...]

PWA → POST /api/sesion/registrar-manual
      {sesion_id: 1, producto_id: 42, cantidad: 5, unidad: "kg"}
    → enqueue_registro_manual()
    → INSERT pending_evaluations (session_id="1", producto_id=42, cantidad=5.0)
    → INSERT registros_conteo (sesion_id=1, stock_sistema=125.5, decision_kalman='PENDIENTE')
    ← {pending_id: 1}

Go Worker:
    → SELECT pending WHERE status='PENDING' FOR UPDATE SKIP LOCKED
    → kalman_evaluar(42, 'entrada', 5.0) → PASA (5 kilos vs 125.5 existentes)
    → aplicar_movimiento_aceptado(42, 'entrada', 5.0, ...)
    → UPDATE pending_evaluations SET status='ACEPTADA'
    → UPDATE registros_conteo SET decision_kalman='ACEPTADA'

PWA → GET /api/sesion/1/estado
    ← {contados: 1, aceptados: 1, alertas: 0, pendientes: 296}
```

### Modo Voz (fast path regex)

```
PWA → POST /api/sesion/registrar-voz
      {sesion_id: 1, texto: "5 kilos de harina de trigo"}

api-gateway:
    → nlu.parse_conteo_rapido("5 kilos de harina de trigo")
    → {cantidad: 5.0, unidad: "Kilogram", producto: "harina de trigo"}
    → fuzzy_match_product("harina de trigo", candidates) → id=42, score=92
    → normalize_unidad(5.0, "Kilogram", "Kilogram") → (5.0, "Kilogram")
    → enqueue_registro_manual(sesion_id=1, producto_id=42, cantidad=5.0)
    ← {via: "regex_fastpath", producto: "HARINA DE TRIGO", cantidad: 5.0, unidad: "Kilogram"}
```

### Modo Voz (fallback LLM — frases complejas)

```
PWA → POST /api/sesion/registrar-voz
      {sesion_id: 1, texto: "revisa si hay algo raro en el inventario"}

api-gateway:
    → nlu.parse_conteo_rapido("revisa si hay...") → None (no es conteo)
    → POST needle-service/infer {query, bodega_id: 3}
    → needle-service: pipeline L1→L2→L3 → investigar_sospechosos
    ← {tool_calls: [{name: "investigar_sospechosos", arguments: {}}]}
```

---

## Nuevo Esquema de Base de Datos

```
bodegas
  id SERIAL PK
  nombre VARCHAR(150) UNIQUE
  creado_en TIMESTAMP

productos
  id SERIAL PK
  nombre VARCHAR(150)
  bodega_id INTEGER FK → bodegas
  codigo_articulo VARCHAR(20)
  unidad VARCHAR(20) DEFAULT 'Unidad'
  stock_actual FLOAT
  media_kalman FLOAT
  varianza_kalman FLOAT
  q_proceso FLOAT DEFAULT 5.0
  r_medicion FLOAT DEFAULT 1.0
  umbral_sigma FLOAT DEFAULT 2.0
  creado_en / actualizado_en TIMESTAMP
  UNIQUE(nombre, bodega_id)

inventario_movimientos
  id SERIAL PK
  producto_id INTEGER FK → productos
  tipo VARCHAR(10) CHECK('entrada','salida')
  cantidad_reportada FLOAT
  residual_kalman FLOAT
  decision_kalman VARCHAR(20)
  umbral_usado FLOAT
  stock_resultante FLOAT
  creado_en TIMESTAMP

auditoria_log
  id SERIAL PK
  movimiento_id INTEGER FK → inventario_movimientos
  puntaje_riesgo FLOAT
  motivo TEXT
  creado_en TIMESTAMP

pending_evaluations          ← cola LLM→Worker
  id BIGSERIAL PK
  session_id TEXT
  tool_name VARCHAR(50)
  producto_id INTEGER FK → productos
  tipo VARCHAR(10)
  cantidad FLOAT
  payload JSONB
  status VARCHAR(20) DEFAULT 'PENDING'
  decision TEXT
  residual FLOAT
  umbral FLOAT
  movimiento_id INTEGER FK → inventario_movimientos
  locked_by TEXT
  locked_at TIMESTAMP
  created_at / resolved_at TIMESTAMP

sesiones_conteo              ← sesiones de la PWA
  id SERIAL PK
  bodega_id INTEGER FK → bodegas
  estado VARCHAR(20) DEFAULT 'activa'
  iniciada_por VARCHAR(100)
  creado_en / finalizado_en TIMESTAMP

registros_conteo             ← audit trail por sesion
  id SERIAL PK
  sesion_id INTEGER FK → sesiones_conteo
  producto_id INTEGER FK → productos
  cantidad_contada FLOAT
  unidad_usada VARCHAR(20)
  cantidad_normalizada FLOAT
  stock_sistema FLOAT
  decision_kalman VARCHAR(20)
  movimiento_id INTEGER FK → inventario_movimientos
  pending_id BIGINT FK → pending_evaluations
  creado_en TIMESTAMP
```

---

## API Reference

### Sesiones

| Method | Path | Body/Params | Response |
|---|---|---|---|
| POST | `/api/sesion/iniciar` | `{bodega_id, iniciada_por?}` | `{sesion_id, bodega_id, estado, total_productos}` |
| POST | `/api/sesion/finalizar` | `{sesion_id}` | `{sesion_id, estado, contados, aceptados, alertas}` |
| GET | `/api/sesion/{id}/estado` | — | `{sesion_id, contados, aceptados, alertas, pendientes, total_productos}` |

### Registro de Conteo

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/sesion/registrar-manual` | `{sesion_id, producto_id, cantidad, unidad}` | `{success, pending_id, message}` |
| POST | `/api/sesion/registrar-voz` | `{sesion_id, texto}` | `{success, via, pending_id?, producto?, cantidad?, unidad?}` |

### Catalogo

| Method | Path | Params | Response |
|---|---|---|---|
| GET | `/api/catalogo/bodega/{id}` | `?q=&solo_pendientes=&sesion_id=` | `[{id, nombre, codigo_articulo, unidad, stock_sistema, stock_contado?, estado_conteo}]` |

### Reportes

| Method | Path | Response |
|---|---|---|
| GET | `/api/reporte/diferencias/{sesion_id}` | `{contados: [...], no_contados: [...], total_contados, total_pendientes}` |
| GET | `/api/reporte/sospechosos/{sesion_id}` | `{sospechosos: [...], total}` |

### Utilidad

| Method | Path | Params | Response |
|---|---|---|---|
| GET | `/api/pending/{pending_id}` | — | `{id, status, decision, residual, umbral, payload, ...}` |
| GET | `/status/{pending_id}` | — | (alias, mismo que arriba) |
| GET | `/health` | — | `{status, backend, services: {...}, db}` |
| POST | `/query` | `{text, session_id?, pending_alert?, bodega_id?}` | `{backend, tool_calls, pending, raw_output}` |
| GET | `/inventory` | `?producto=` | Productos con stock |
| GET | `/sospechosos` | `?producto=` | Auditoria Kalman |

---

## Para Levantar el Sistema

```bash
# 1. Dependencias CLI
pip install -r requirements.txt

# 2. Levantar servicios (minimo: postgres + kalman-worker + needle + gateway)
docker compose up -d

# 3. Esperar a que Needle descargue el modelo
docker logs -f cactus_needle

# 4. Importar catalogo Excel
python -m src.importer

# 5. Verificar
curl http://localhost:8200/health

# 6. CLI (debug)
python -m src.cli
```

### Perfiles Docker

```bash
# Minimo (sin voz, con Needle local)
docker compose up -d

# Con voz (Whisper)
docker compose --profile with-voice up -d

# Con OpenRouter (Claude 3.5 Haiku) + voz
docker compose --profile with-openrouter --profile with-voice up -d

# Con Eleven Labs (STT + TTS cloud) — el toggle cloud arranca apagado
docker compose --profile with-elevenlabs up -d

# Todo cloud (OpenRouter + Eleven Labs) + voz local Whisper de respaldo
docker compose --profile with-openrouter --profile with-elevenlabs --profile with-voice up -d
```

Activacion cloud runtime (cambia sin reiniciar):

```bash
# En la CLI
> cloud on         # LLM/STT/TTS intentan cloud primero, fallback automatico a local
> cloud off        # todo local
> cloud status     # muestra config activa
> cloud stt=elevenlabs tts=kokoro llm=auto   # override por-backend

# Por HTTP
curl -X POST http://localhost:8200/api/config -H "Content-Type: application/json" \
  -d '{"cloud_enabled": true}'
```

---

## Lo que Falta (PWA — Daviño)

El backend expone todos los endpoints que la PWA necesita:

| Pantalla PWA | Endpoint |
|---|---|
| Seleccionar bodega | `GET /api/catalogo/bodega/{id}` (solo nombres) |
| Iniciar conteo | `POST /api/sesion/iniciar` |
| Lista de productos (Tablet) | `GET /api/catalogo/bodega/{id}?sesion_id=X` |
| Input manual (Tablet) | `POST /api/sesion/registrar-manual` |
| Grabar voz (texto) | `POST /api/sesion/registrar-voz` (enviar texto transcrito) |
| **Transcribir audio** | `POST /api/audio/transcribir` (multipart file) — **implementado**, backend-agnostico |
| **Sintetizar voz** | `POST /api/audio/speak` (json) — **implementado**, backend-agnostico |
| **Listar voces** | `GET /api/audio/voices` — para selector en PWA |
| **Toggle cloud** | `GET/POST /api/config` — `{cloud_enabled, llm, stt, tts}` |
| Barra de progreso | `GET /api/sesion/{id}/estado` (poll cada 2s) |
| Finalizar conteo | `POST /api/sesion/finalizar` |
| Reporte final | `GET /api/reporte/diferencias/{id}` |
| Alertas Kalman | `GET /api/reporte/sospechosos/{id}` |

Para el MVP del hackathon, el audio se puede grabar con `MediaRecorder` API en el navegador y enviar como `multipart/form-data` al endpoint `/api/audio/transcribir` (a implementar como proxy al voice-service).

---

## Fase 7 — Toggle Cloud + Eleven Labs (entrada/salida de voz)

**Fecha: implementacion actual.** El uso constante de la demo es el flujo
de voz (STT + TTS), asi que se agrega la opcion cloud Eleven Labs con
toggle runtime y fallback automatico.

### 7.1 Servicio `services/elevenlabs/` (nuevo, ~280 lineas)

Replica el patron de `openrouter-service` con dos endpoints (cloud STT y
cloud TTS). Sin torch ni torch-deps — solo fastapi + httpx.

| Endpoint | Consume | Devuelve |
|---|---|---|
| `POST /transcribe` | multipart `file`, `language_code`, `model_id` | `{text, language, backend, model}` |
| `POST /speak` | json `{text, voice_id?, model_id?, output_format?}` | stream `audio/pcm` (default `pcm_24000`) |
| `GET /voices` | — | `{voices: [...], default_voice_id}` (cache 1h server-side) |
| `GET /health` | — | `{status, has_key, stt_model, tts_model, default_voice, tts_output}` |

TTS a PCM via `output_format=pcm_24000` (int16 LE mono 24kHz) — encaja
1:1 con `tts_client.py:30` (SAMPLE_RATE=24000, DTYPE=int16). Cero
decode mp3, cero `pydub`.

### 7.2 Toggle runtime en api-gateway

Estado en memoria (no se persiste entre reinicios — el default vuelve al
`CLOUD_ENABLED` del `.env`):

```python
_cloud_toggle: bool = os.getenv("CLOUD_ENABLED", "false").lower() == "true"
_llm_override: str | None = None
_stt_override: str | None = None
_tts_override: str | None = None
```

Pickers:

```python
def _pick_llm() -> str:  # "needle" | "openrouter"
    if _llm_override: return _llm_override
    return "openrouter" if _cloud_toggle else LLM_BACKEND
# _pick_stt(), _pick_tts() identicos
```

Endpoints nuevos:

| Method | Path | Body / Response |
|---|---|---|
| `GET` | `/api/config` | `{cloud_enabled, llm, stt, tts, llm_override, stt_override, tts_override, defaults}` |
| `POST` | `/api/config` | `{cloud_enabled?, llm?, stt?, tts?}` (cada uno: `auto` o el valor canonico) |

### 7.3 Fallback automatico cloud → local

Helper `_post_with_fallback()` aplicado a:
- `/query` (LLM)
- `/api/sesion/registrar-voz` (LLM, via internal)
- `/api/audio/transcribir` (STT)
- `/api/audio/speak` (TTS)

Si el toggle esta en `true` y el backend cloud retorna error (HTTP no-200,
timeout, request error) → cae al local. La respuesta incluye:

```json
{
  "backend": "local",
  "backend_requested": "elevenlabs",
  "fallback_used": true,
  ...
}
```

### 7.4 Proxy STT con ffmpeg

El gateway ahora decodifica cualquier formato de audio (webm, mp3, wav,
m4a, ogg) a PCM 16k mono con ffmpeg, antes de mandarlo al voice-service
local. Si el backend activo es Eleven Labs, el archivo pasa tal cual
(Eleven Labs acepta todos los formatos). ffmpeg se instalo en el
Dockerfile del api-gateway (apt-get).

### 7.5 Archivos creados/modificados (resumen)

| Archivo | Cambio |
|---|---|
| `services/elevenlabs/main.py` | nuevo (~280 lineas): STT, TTS, voices, health |
| `services/elevenlabs/requirements.txt` | nuevo: fastapi, uvicorn, httpx, python-multipart |
| `services/elevenlabs/Dockerfile` | nuevo: python:3.11-slim, copia llm_common/ por consistencia |
| `docker-compose.yml` | +`elevenlabs-service` en perfil `with-elevenlabs`, +vars en api-gateway |
| `services/api-gateway/Dockerfile` | +ffmpeg (apt-get) para decode audio |
| `services/api-gateway/requirements.txt` | +websockets, +python-multipart |
| `services/api-gateway/main.py` | +`/api/config` (GET/POST), +`/api/audio/{transcribir,speak,voices}`, fallback automatico en `/query` y `/api/sesion/registrar-voz`, `+STT_BACKEND`/`+TTS_BACKEND` env, `+CLOUD_ENABLED` toggle, helper `_post_with_fallback()` y `_ffmpeg_to_pcm16k()` |
| `src/api_client.py` | +`get_config()`, +`set_config()`, +`transcribe_audio()`, +`speak_remote()`, +`list_voices()` |
| `src/voice_client.py` | reescrito: graba a .wav temp y POSTea al gateway (sin WebSocket directo) |
| `src/tts_client.py` | +`play_pcm()` para reproducir buffers PCM ya recibidos (usado por `tts` via gateway) |
| `src/cli.py` | comandos `cloud on/off/status` y `cloud llm=... stt=... tts=...`, comando `voices`, banner muestra los 3 backends activos + tag `[CLOUD]`, comando `tts` ahora va por el gateway con fallback |
| `.env.example` | nuevo: documenta todas las vars nuevas |

### 7.6 Comandos nuevos de la CLI

| Comando | Descripcion |
|---|---|
| `cloud on` | Activa todos los backends cloud (LLM, STT, TTS) con fallback automatico |
| `cloud off` | Vuelve a todo local |
| `cloud status` | Muestra `{cloud_enabled, llm, stt, tts, *_override, defaults}` |
| `cloud llm=openrouter stt=whisper tts=elevenlabs` | Override por-backend (cualquier combinacion; usar `auto` para volver al toggle) |
| `voices` | Lista las voces disponibles del backend TTS activo (proxy a Eleven Labs si esta activo) |

### 7.7 Variables de entorno nuevas

| Var | Default | Notas |
|---|---|---|
| `STT_BACKEND` | `whisper` | `whisper` o `elevenlabs` (default si no hay override ni toggle) |
| `TTS_BACKEND` | `kokoro` | `kokoro` o `elevenlabs` |
| `CLOUD_ENABLED` | `false` | Toggle global al arranque (luego `cloud on/off` en runtime) |
| `ELEVENLABS_API_KEY` | vacio | Requerida para que elevenlabs-service funcione |
| `ELEVENLABS_VOICE_ID` | `21m00Tcm4TlvDq8ikWAM` (Rachel) | Default; el endpoint `/api/audio/speak` acepta `voice_id` en el body |
| `ELEVENLABS_STT_MODEL` | `scribe_v1` | |
| `ELEVENLABS_TTS_MODEL` | `eleven_multilingual_v2` | |
| `ELEVENLABS_TTS_OUTPUT` | `pcm_24000` | PCM int16 LE mono 24k para match con tts_client.py |

### 7.8 Para usar la PWA con cloud

1. Levantar con perfil cloud:
   ```bash
   ELEVENLABS_API_KEY=sk_xxx OPENROUTER_API_KEY=sk-xxx \
     docker compose --profile with-elevenlabs --profile with-openrouter up -d
   ```
2. Activar runtime (sin reiniciar):
   ```bash
   curl -X POST http://localhost:8200/api/config -d '{"cloud_enabled": true}' -H "Content-Type: application/json"
   ```
3. La PWA consume `/api/audio/transcribir` para STT y `/api/audio/speak`
   para TTS sin saber que backend esta activo. `GET /api/audio/voices`
   alimenta el selector de voces.

### 7.9 Narrador (textos naturales para TTS)

**Problema**: los mensajes TTS originales sonaban roboticos
("Se agregaron 5 kilos de harina. Stock actual: 125.5, en Bodega 1.").
Para el uso constante de la demo (voz como canal principal) hace falta
algo mas natural y conversacional.

**Solucion**: modulo `services/llm_common/narrator.py` + endpoint
`POST /api/narrate`. Dos modos seleccionables runtime:

| Modo | Backend | Velocidad | Costo | Calidad |
|---|---|---|---|---|
| `default` | templates hardcodeados con variaciones aleatorias | < 1ms | 0 | Buena (es-AR natural) |
| `llm` | OpenRouter con modelo configurable (default `google/gemma-4-31b-it:free`) | 1-3s | 0 (free tier) | Excelente |

Eventos soportados (9):
- `aceptada`, `sospechosa`, `confirmada`, `rechazada`, `consulta`,
  `sospechosos`, `invalid`, `no_action`, `registrar_manual`

Ejemplos de salida (modo default):
- `aceptada` con `{producto: "papa", cantidad: 5, unidad: "kg", stock_actual: 130}`:
  - "Listo, sumamos 5 kilos de papa. Te quedan 130 kilos en Bodega 1."
  - "Buenísimo, sumamos 5 kilos de papa. Te quedan 130 kilos en Bodega 1."
  - "Anotado, sumamos 5 kilos de papa. Te quedan 130 kilos en Bodega 1."
  (elige una variación al azar)
- `sospechosa` con `puntaje_riesgo: 4.2`: "Pará, ingreso de 50 kilos de
  harina se va de mambo, 4.2 sigmas. Te lo confirmo o lo descarto?"
- `consulta` con `{stock_actual: 130, unidad: "kg", bodega: "Bodega 1"}`:
  "Encontre esto: en Bodega 1 hay 130 kilos de papa."

Con `narrator=llm` + `OPENROUTER_API_KEY` configurado, el gateway envia el
template al LLM con un system prompt en espanol rioplatense que pide
reformular el mensaje de forma conversacional. Cache en memoria (256
entradas, LRU simple) para no martillar la API en eventos repetidos.

**Endpoints**:
- `POST /api/narrate` body `{event, data}` -> `{text, event, backend, model}`
- `GET /api/config` ahora incluye `narrator`, `narrator_model`, `narrator_override`, `narrator_model_override`
- `POST /api/config` acepta campos `narrator` y `narrator_model`

**Runtime**:
```bash
# Activar narrador LLM (Gemma 4 31B free por default)
curl -X POST http://localhost:8200/api/config \
  -H "Content-Type: application/json" \
  -d '{"narrator": "llm"}'

# Cambiar el modelo del narrador
curl -X POST http://localhost:8200/api/config \
  -H "Content-Type: application/json" \
  -d '{"narrator_model": "deepseek/deepseek-v4-flash"}'

# Volver a templates
curl -X POST http://localhost:8200/api/config \
  -H "Content-Type: application/json" \
  -d '{"narrator": "default"}'
```

**CLI**: el `cloud` y el `cloud status` ahora muestran el narrador activo.
Comando nuevo: `narrate <evento> k=v...` para probar (e.g.,
`narrate aceptada producto=papa cantidad=5 unidad=kg stock_actual=130`).
El CLI ya no construye frases localmente — siempre llama al gateway.

### 7.10 Selector de modelos (`/api/models`)

**Problema**: cambiar el modelo de OpenRouter requeria reiniciar el
servicio (variable de entorno) y el usuario no tenia visibilidad de que
modelos estaban disponibles.

**Solucion**: endpoint dedicado `/api/models` con GET (lista) y POST
(selecciona).

**`GET /api/models`**:
- Sin params: lista curada de 5 modelos recomendados (DeepSeek V4 Flash/Pro,
  Gemma 4 31B/26B free, Claude 3.5 Haiku) con `cost_in`, `cost_out`, `free`,
  `tagline` para cada uno.
- `?all=true`: si hay `OPENROUTER_API_KEY`, lista los 350+ modelos
  disponibles en OpenRouter con sus precios.
- `?category=narrator|llm`: filtra por categoria.
- Siempre incluye `current: {narrator_backend, narrator_model, llm}` y
  `defaults: {...}`.

**`POST /api/models/select`**:
- Body: `{category: "narrator", model: "<slug>"}` (o `model: null`/`auto`
  para volver al default).
- Equivale a `POST /api/config {narrator_model: ...}` pero con un endpoint
  dedicado y self-documenting.

**CLI**:
```bash
models                  # lista curada
models all              # lista completa de OpenRouter
models select <slug>    # cambia el modelo del narrador
models select auto      # vuelve al default
```

### 7.11 Archivos modificados en sub-fase 7.9-7.10

| Archivo | Cambio |
|---|---|
| `services/llm_common/narrator.py` | nuevo (~220 lineas): templates con variaciones + LLM rewriter |
| `services/api-gateway/main.py` | +`/api/narrate` (POST), +`/api/models` (GET), +`/api/models/select` (POST), +narrator state runtime, +NARRATOR_BACKEND/NARRATOR_MODEL env |
| `docker-compose.yml` | +NARRATOR_BACKEND, NARRATOR_MODEL, OPENROUTER_API_KEY, OPENROUTER_BASE en api-gateway |
| `.env.example` | +seccion Narrador |
| `src/api_client.py` | +`narrate()`, +`list_models()`, +`select_model()` |
| `src/cli.py` | refactor `_narrate_*` para llamar al gateway, +comandos `models` y `narrate` demo, banner muestra narrador activo, `cloud status` incluye narrador |
| `PLAN.md` | +secciones 7.9-7.11 |

---

## Fase 8 — Manager CLI + Constraints de DB

### 8.1 Trigger: enteros solo para unidad "Unidad"

**Problema**: la DB aceptaba decimales en `cantidad_*` para cualquier producto,
incluso los que tienen `unidad = "Unidad"` (que son cantidades discretas: 5
unidades, no 5.5). Esto permitia inconsistencia: stock_resultante podia
ser 5.7 tornillos.

**Solucion**: trigger `check_cantidad_unidad()` en plpgsql que:
- Para cada INSERT/UPDATE en las tablas relevantes, busca la unidad del
  producto via JOIN con `unidades`.
- Si la unidad es "Unidad", valida que los campos de cantidad sean
  enteros exactos (`valor = ROUND(valor)`).
- Si la unidad es "Kilogram", "Liter" u otra, deja pasar decimales.

**Tablas y campos cubiertos**:
- `inventario_movimientos.cantidad_reportada`
- `registros_conteo.cantidad_contada` y `cantidad_normalizada`
- `pending_evaluations.cantidad`
- `stock.stock_actual`

**Excepcion**: `IntegrityConstraintViolation` con mensaje claro:
```
inventario_movimientos.cantidad_reportada debe ser entero para productos
con unidad "Unidad" (producto_id=42, recibido: 5.5)
```

**Idempotencia**: `CREATE OR REPLACE FUNCTION` + `DROP TRIGGER IF EXISTS`,
asi corre tanto en init.sql (fresh deploy) como en migraciones.

### 8.2 Migraciones SQL

Directorio `db/migrations/` con archivos numerados:
- `001_normalize_3fn.sql` — la 3FN que ya existia
- `002_cantidad_unidad_integer.sql` — el trigger nuevo

Tabla `_migrations` (creada on demand por el manager) trackea que ya
estan aplicadas para no re-ejecutar.

### 8.3 Manager CLI (`src/manager.py`)

CLI admin para correr **en el servidor**. Separado del CLI de inventario
(`src/cli.py`) que usa el usuario final. Diseñado para sysadmins que
gestionan el deploy.

**Comandos** (12):

| Comando | Proposito |
|---|---|
| `status` | docker ps + gateway health + config activa |
| `config show` | muestra .env con secretos enmascarados |
| `config set KEY=VALUE ...` | setea variables (con confirmacion o `--yes`) |
| `config unset KEY ...` | remueve variables |
| `keys` | lista solo API keys/secrets (filtrado) |
| `rebuild [services...]` | `docker compose build + up -d` |
| `restart [services...]` | `docker compose restart` |
| `up` | arranca todo con profiles activos |
| `down` | para todo (con doble confirmacion) |
| `logs <service> [-n N] [-f]` | tail de logs |
| `migrate` | aplica migraciones SQL pendientes |
| `models` / `models select <slug>` | lista / cambia modelos OpenRouter via gateway |

**Seguridad**:
- Secretos enmascarados en outputs (mask `sk-...` como `sk-t****7890`).
- Confirmacion interactiva en acciones destructivas (`config set`,
  `rebuild`, `down`). `--yes` la skipea.
- `down` pide escribir "yes" literal (no "y"), por destructivo.
- `.env` se preserva con la estructura de `.env.example` (secciones con
  comentarios); keys nuevas se appendan al final con un header
  "Agregadas por manager".

**Output**: usa `rich` (Table, Console) si esta disponible, si no
fallback a `print` plano.

**Conexion DB**: lee `DATABASE_URL` del ambiente (default
`postgres://cactus:cactus@127.0.0.1:5432/inventario`).

### 8.4 Archivos modificados/creados en Fase 8

| Archivo | Cambio |
|---|---|
| `db/init.sql` | +`check_cantidad_unidad()` function + 4 triggers (movimientos, registros, pending, stock) |
| `db/migrations/002_cantidad_unidad_integer.sql` | nuevo: misma logica, para DBs existentes |
| `db/migrations/001_normalize_3fn.sql` | (pre-existente, ahora trackeado por el manager) |
| `src/manager.py` | nuevo (~430 lineas): CLI admin completo |
| `PLAN.md` | +secciones 8.1-8.4 |

---

## Fase 9 — Backend listo para frontend web (CORS + Auth + Documentacion)

**Proposito**: el nucleo (gateway + servicios + DB) esta completo. La CLI
local es solo para dev/test. El frontend web real se conecta via HTTP.
Esta fase deja al gateway production-ready para esa conexion:
- CORS configurable (que origins pueden llamar)
- Auth opcional por API key (header `X-API-Key`)
- Health endpoint que reporta la config del API
- Documentacion completa de todos los endpoints con ejemplos
  `fetch` + `curl` para que el equipo de frontend tenga todo
  lo que necesita sin leer el codigo

### 9.1 CORS

`ALLOWED_ORIGINS` env (default `*`). Lista separada por comas:
```bash
ALLOWED_ORIGINS=https://b-link.tu-dominio.com,https://www.b-link.tu-dominio.com
```

`CORSMiddleware` de FastAPI; permite credenciales, todos los metodos
y headers. Expone los headers custom (`X-Backend`, `X-Fallback-Used`,
`X-Sample-Rate`, `X-Voice-Id`, etc) para que el frontend pueda leerlos.

### 9.2 Auth opcional

`API_KEY` env (default vacia = sin auth). Si esta seteada, todos los
endpoints (salvo `/health`, `/docs`, `/openapi.json`, `/redoc`) requieren
el header `X-API-Key: <valor>`. Implementado como middleware global
HTTP (no como `Depends` por ruta) para no repetir en cada endpoint.

Respuesta cuando falta o es incorrecta: `401` con
`{"detail": "API key invalida o ausente. Header requerido: X-API-Key"}`.

### 9.3 Health endpoint enriquecido

Ademas de status y config, ahora reporta:
```json
{
  "api": {
    "version": "3.0.0",
    "auth_required": true,
    "cors_origins": ["*"],
    "docs_url": "/docs",
    "openapi_url": "/openapi.json"
  }
}
```

El frontend lo usa al arrancar para verificar conectividad, version
y config de CORS/auth.

### 9.4 FRONTEND.md (~700 lineas)

Documentacion completa para el equipo de frontend. Cubre:
- Configuracion base (URL, API key, CORS)
- **21 endpoints** documentados con request/response JSON, ejemplos
  `fetch` y `curl`, y casos de uso
- Wiring de UI: que componente/boton llama a que endpoint
- Flujos end-to-end (modo voz, modo manual, polling de pending, etc)
- Manejo de errores (401, 502, 500, etc)
- Como generar cliente tipado desde OpenAPI
- Checklist pre-merge para el equipo de frontend
- Diagrama de secuencia end-to-end

Endpoints documentados:
- `GET /health` (publico)
- `GET/POST /api/config` (toggle cloud, overrides)
- `GET /api/models`, `POST /api/models/select`
- `POST /api/narrate` (B-Link, eventos -> texto)
- `GET /api/audio/voices`
- `POST /api/audio/transcribir` (STT)
- `POST /api/audio/speak` (TTS, devuelve stream audio)
- `GET /api/bodegas`
- `GET /api/catalogo/bodega/{id}`
- `POST /api/sesion/iniciar`, `finalizar`
- `GET /api/sesion/{id}/estado` (polling cada 2s)
- `POST /api/sesion/registrar-manual`
- `POST /api/sesion/registrar-voz` (flujo voz completo)
- `GET /api/reporte/diferencias/{id}`
- `GET /api/reporte/sospechosos/{id}`
- `GET /api/pending/{id}` (polling cada 200ms)
- `POST /query` (chat libre)
- `GET /inventory`, `/sospechosos`, `/catalog`, `/status/{id}`

### 9.5 Archivos modificados/creados en Fase 9

| Archivo | Cambio |
|---|---|
| `services/api-gateway/main.py` | +`CORSMiddleware` (ALLOWED_ORIGINS), +middleware de auth (API_KEY), +`api` info en /health, +env `ALLOWED_ORIGINS` y `API_KEY` |
| `docker-compose.yml` | +env `ALLOWED_ORIGINS` y `API_KEY` en api-gateway |
| `.env.example` | +seccion Frontend (CORS + Auth) |
| `FRONTEND.md` | nuevo: guia completa para el equipo de frontend |
| `PLAN.md` | +secciones 9.1-9.5 |