# Phylloinventory — Documentacion de Implementacion

## Estado: Fases 1-6 completadas

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

### Servicios Docker (6 containers)

| Servicio | Puerto | Perfil | Rol |
|---|---|---|---|
| `postgres` | 5432 | default | DB + cola + funciones Kalman puras |
| `kalman-worker` | 8300 | default | Go, consume `pending_evaluations`, 8 goroutines |
| `needle-service` | 8081 | default | Needle 26M, L1→L2→L3 pipeline, fuzzy search |
| `api-gateway` | 8200 | default | Router central, 15 endpoints |
| `openrouter-service` | 8082 | with-openrouter | Alternativa: Claude 3.5 Haiku via OpenRouter |
| `voice-service` | 8100 | with-voice | Whisper via WebSocket |

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
| Grabar voz | `POST /api/sesion/registrar-voz` (enviar texto transcrito) |
| Transcribir audio | `POST /api/audio/transcribir` (pendiente de implementar — proxy a voice-service) |
| Barra de progreso | `GET /api/sesion/{id}/estado` (poll cada 2s) |
| Finalizar conteo | `POST /api/sesion/finalizar` |
| Reporte final | `GET /api/reporte/diferencias/{id}` |
| Alertas Kalman | `GET /api/reporte/sospechosos/{id}` |

Para el MVP del hackathon, el audio se puede grabar con `MediaRecorder` API en el navegador y enviar como `multipart/form-data` al endpoint `/api/audio/transcribir` (a implementar como proxy al voice-service).