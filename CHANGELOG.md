# CHANGELOG — Sprint Final Hackathon

## 2026-07-25 — Fast-paths NLU, Stats Manager, ElevenLabs TTS

---

### 1. NLU: Regex Fast-Paths Completos

**Archivo:** `services/llm_common/nlu.py` (+80 lineas)

Se agregaron 16 patrones regex para cubrir todos los comandos de inventario sin pasar por el LLM. El 90%+ de las interacciones ahora se resuelven en <1ms.

#### 1a. Fast-path de escritura (`parse_escritura_rapida`) — ya existia, sin cambios

Distingue direccion agregar vs remover:
- `"agrega 5 kilos de harina de trigo"` → `{tool: "agregar_inventario", cantidad: 5.0, unidad: "Kilogram"}`
- `"saca 3 litros de leche"` → `{tool: "remover_inventario", cantidad: 3.0, unidad: "Liter"}`

Verbos cubiertos:
- Agregar: `agregar`, `añadir`, `ingresar`, `meter`, `poner`, `cargar`, `subir`
- Remover: `sacar`, `remover`, `retirar`, `quitar`, `vender`, `descontar`, `restar`, `bajar`

#### 1b. Fast-path de lectura (`parse_lectura_rapida`) — NUEVO

8 patrones que cubren consultas de inventario:

| Patron | Ejemplo |
|---|---|
| `cu[áa]nt[oa]s? (hay\|tenemos\|quedan) (de\|en)? <producto>` | "cuanto hay de aceite" |
| `cu[áa]nt[oa]s? <producto> (hay\|tenemos\|quedan)` | "cuanta harina tenemos" |
| `consulta[r]? (stock\|inventario) (de)? <producto>` | "consulta inventario de pan" |
| `ver (stock\|inventario) (de)? <producto>?` | "ver stock", "ver inventario de aceite" |
| `qu[ée] (hay\|tenemos\|queda) (en inventario)?` | "que hay en inventario" |
| `qu[ée] (hay\|tenemos) de <producto>` | "que hay de tomate" |
| `(mostrar\|dame) (el )?(inventario\|stock) (de)? <producto>?` | "dame stock", "mostrame inventario" |
| `inventario\|stock\|cat[aá]logo` (solo) | "inventario" |

Retorna: `{tool: "consultar_inventario", producto: "aceite" | null}`

#### 1c. Fast-path de investigacion (`parse_investigacion_rapida`) — NUEVO

8 patrones que cubren comandos de auditoria:

| Patron | Ejemplo |
|---|---|
| `(hay )?algo raro` | "algo raro", "hay algo raro en el inventario" |
| `investig(ar?\|a)` | "investigar", "investiga" |
| `audit(ar?\|a)` | "auditar", "audita" |
| `sospechos[oa]s?` | "sospechosos", "sospechosa" |
| `discrepancias?` | "discrepancias" |
| `anomal[ií]as?` | "anomalias" |
| `(revisa\|mira\|checa) (si hay )?(sospechosos?\|errores?\|anomalias?)` | "revisa si hay errores" |
| `errores? (de\|del) inventario` | "errores del inventario" |

Retorna: `{tool: "investigar_sospechosos", producto: null}`

---

### 2. Endpoint `registrar-voz` Refactorizado

**Archivo:** `services/api-gateway/main.py`

El endpoint `POST /api/sesion/registrar-voz` ahora ejecuta 3 fast-paths secuenciales antes de caer al LLM:

```
texto → parse_escritura_rapida()  ──match──→ fuzzy + enqueue → respuesta
      → parse_lectura_rapida()    ──match──→ query DB directa → respuesta
      → parse_investigacion_rapida()─match─→ query DB directa → respuesta
      → LLM fallback (needle/openrouter)
```

**Respuesta de escritura (via regex):**
```json
{
  "success": true,
  "pending_id": 42,
  "via": "regex_escritura",
  "tool": "agregar_inventario",
  "producto": "HARINA DE TRIGO",
  "cantidad": 5.0,
  "unidad": "Kilogram"
}
```

**Respuesta de lectura (via regex):**
```json
{
  "success": true,
  "via": "regex_lectura",
  "tool": "consultar_inventario",
  "producto": "ACEITE",
  "stock_actual": 333.0,
  "unidad": "Liter"
}
```

**Respuesta de investigacion (via regex):**
```json
{
  "success": true,
  "via": "regex_investigacion",
  "tool": "investigar_sospechosos",
  "sospechosos": [...],
  "total": 3
}
```

---

### 3. Manager de Estadisticas

**Archivo:** `services/api-gateway/main.py` (+3 endpoints)

#### `GET /api/stats/general` — Dashboard global

```json
{
  "bodegas": 48,
  "productos_catalogo": 1407,
  "productos_con_stock": 1380,
  "sesiones_activas": 2,
  "sospechosos_pendientes": 5,
  "movimientos_hoy": 127,
  "ultimas_sesiones": [...]
}
```

#### `GET /api/stats/bodega/{id}` — Estadisticas por bodega

```json
{
  "bodega": "almacen general",
  "total_productos": 297,
  "con_stock_positivo": 290,
  "con_stock_negativo": 3,
  "total_sesiones": 12,
  "ultima_sesion": {...}
}
```

Incluye conteo de stock negativo (descuadres reales del Excel que el Kalman debe detectar).

#### `GET /api/stats/sesiones?limit=10` — Resumen de sesiones

```json
{
  "sesiones": [
    {
      "id": 12,
      "bodega": "almacen general",
      "estado": "finalizada",
      "iniciada_por": "Carlos",
      "productos_contados": 47,
      "alertas_kalman": 3,
      "aceptados": 44
    }
  ],
  "total": 10
}
```

---

### 4. ElevenLabs TTS Service

**Archivos nuevos (3):**

| Archivo | Proposito |
|---|---|
| `services/elevenlabs_svc/main.py` | FastAPI proxy a ElevenLabs API v1 |
| `services/elevenlabs_svc/requirements.txt` | fastapi, uvicorn, httpx, numpy, soundfile |
| `services/elevenlabs_svc/Dockerfile` | Python 3.11-slim, expone :8206 |

**Endpoint:** `POST /speak`
```json
{
  "text": "Movimiento aceptado. Stock actualizado a 55 kilogramos.",
  "voice": "9BWtsMINqrJLrRakOkie",
  "model": "eleven_multilingual_v2",
  "speed": 1.0
}
```

**Respuesta:** Streaming `audio/raw` PCM int16 mono 24kHz, chunks de 100ms.

**Pipeline interno:**
```
texto → ElevenLabs API (MP3) → decode MP3 → resample 24kHz → chunk PCM → stream
```

**Perfil Docker:** `--profile with-elevenlabs`
```bash
ELEVENLABS_API_KEY=sk_... docker compose --profile with-elevenlabs up -d
```

**Interfaz compatible con Kokoro:** Mismo endpoint `POST /speak`, mismo formato de audio (PCM int16 24kHz). El `tts_client.py` puede switchear entre ambos con solo cambiar la URL.

**Archivos modificados:**
- `docker-compose.yml`: +elevenlabs-service (perfil with-elevenlabs), +ELEVENLABS_URL en api-gateway
- `services/api-gateway/main.py`: +ELEVENLABS_URL, +elevenlabs en /health check

---

### 5. Health Check Expandido

`GET /health` en api-gateway ahora incluye 6 servicios:

```json
{
  "status": "ok",
  "backend": "needle",
  "services": {
    "needle": {"status": "ok", "model_loaded": true},
    "openrouter": {"status": "down", "error": "..."},
    "voice": {"status": "ok"},
    "kalman": {"status": "ok"},
    "kokoro": {"status": "ok"},
    "elevenlabs": {"status": "ok"}
  },
  "db": "ok"
}
```

---

## Resumen de Capacidades Actuales

### Comandos de voz (Whisper → regex fast-path)

| Comando | Fast-path | LLM fallback |
|---|---|---|
| "agrega 5 kilos de harina" | parse_escritura_rapida | openrouter |
| "saca 3 litros de leche" | parse_escritura_rapida | openrouter |
| "cuanto hay de aceite" | parse_lectura_rapida | openrouter |
| "que tenemos en inventario" | parse_lectura_rapida | openrouter |
| "dame el stock de pan" | parse_lectura_rapida | openrouter |
| "algo raro, investiga" | parse_investigacion_rapida | openrouter |
| "audita sospechosos" | parse_investigacion_rapida | openrouter |
| Frases complejas/ambiguas | — | openrouter / needle |

### Endpoints totales (api-gateway)

| Categoria | Endpoints |
|---|---|
| Query LLM | `/query` |
| Sesiones | `/api/sesion/iniciar`, `/finalizar`, `/{id}/estado` |
| Registro | `/api/sesion/registrar-manual`, `/registrar-voz` |
| Catalogo | `/api/catalogo/bodega/{id}`, `/api/bodegas` |
| Reportes | `/api/reporte/diferencias/{id}`, `/api/reporte/sospechosos/{id}` |
| Stats | `/api/stats/general`, `/api/stats/bodega/{id}`, `/api/stats/sesiones` |
| Utilidad | `/health`, `/status/{pending_id}`, `/api/pending/{id}`, `/inventory`, `/sospechosos` |
| **Total** | **18 endpoints** |

### Servicios Docker

| Servicio | Puerto | Perfil | TTS? |
|---|---|---|---|
| postgres | 5432 | default | — |
| kalman-worker | 8300 | default | — |
| needle-service | 8081 | default | — |
| api-gateway | 8200 | default | — |
| openrouter-service | 8082 | with-openrouter | — |
| voice-service | 8100 | with-voice | — |
| kokoro-service | 8205 | (always) | Kokoro 82M |
| elevenlabs-service | 8206 | with-elevenlabs | ElevenLabs API |