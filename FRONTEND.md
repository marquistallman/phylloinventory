# B-Link Frontend Integration Guide

Esta guia es para el equipo que va a construir el **frontend web** de
B-Link. Cubre:

- La URL base del backend
- Autenticacion (API key)
- CORS (que origins pueden llamar)
- **TODOS los endpoints** que el frontend va a consumir, con request/response
  ejemplo y ejemplos en `fetch` y `curl`
- **Wiring de UI**: que boton/componente llama a que endpoint, y como
  mostrar la respuesta
- Errores comunes y como manejarlos
- Como correr el frontend en dev y en produccion

El nucleo (la CLI en `src/cli.py`) es **solo para pruebas locales** y NO
consume la API directamente. El frontend SI consume esta API.

---

## 1. Configuracion base

| Parametro | Valor en dev | Valor en produccion |
|---|---|---|
| URL del gateway | `http://localhost:8200` | `https://b-link.tu-dominio.com` |
| Header `X-API-Key` | (vacio, no auth) | `sk_prod_xxxxx...` (rotar cada 90 dias) |
| `ALLOWED_ORIGINS` | `*` (en `.env`) | El dominio real del frontend |
| WebSocket | no usado (por ahora) | igual |

Donde se configura:
- `API_GATEWAY_URL` en el `.env` del frontend
- `API_KEY` en el `.env` del frontend (que el build process inyecta en runtime,
  NO en el bundle del cliente si es un SPA publico — en ese caso el frontend
  habla a un BFF que tiene la key server-side)
- `ALLOWED_ORIGINS` y `API_KEY` en el `.env` del **servidor** (`.env.example`)

El navegador hace CORS preflight automaticamente para POST/PUT con
`Content-Type: application/json`, asi que `ALLOWED_ORIGINS` debe incluir
el origen del frontend (`https://b-link.tu-dominio.com` o `http://localhost:3000`
en dev).

---

## 2. Endpoints del backend

### 2.0 Indice rapido

| Endpoint | Metodo | Auth | Proposito |
|---|---|---|---|
| `/health` | GET | NO | Liveness + info del API (version, CORS, auth) |
| `/api/config` | GET | SI | Ver config runtime activa (LLM/STT/TTS/narrator) |
| `/api/config` | POST | SI | Cambiar config runtime (toggle cloud, overrides) |
| `/api/models` | GET | SI | Listar modelos OpenRouter disponibles |
| `/api/models/select` | POST | SI | Cambiar el modelo del narrador en runtime |
| `/api/narrate` | POST | SI | Convertir evento estructurado en texto natural |
| `/api/audio/voices` | GET | SI | Listar voces TTS disponibles |
| `/api/audio/transcribir` | POST | SI | Audio -> texto (STT) |
| `/api/audio/speak` | POST | SI | Texto -> audio (TTS, devuelve stream PCM/mpeg) |
| `/api/bodegas` | GET | SI | Listar bodegas |
| `/api/catalogo/bodega/{id}` | GET | SI | Productos en una bodega con estado de conteo |
| `/api/sesion/iniciar` | POST | SI | Crear sesion de conteo en una bodega |
| `/api/sesion/finalizar` | POST | SI | Cerrar sesion, devuelve stats finales |
| `/api/sesion/{id}/estado` | GET | SI | Progreso en tiempo real de la sesion |
| `/api/sesion/registrar-manual` | POST | SI | Registro manual (modo Tablet) |
| `/api/sesion/registrar-voz` | POST | SI | Registro por voz (texto) |
| `/api/reporte/diferencias/{id}` | GET | SI | Comparativo contado vs sistema |
| `/api/reporte/sospechosos/{id}` | GET | SI | Alertas Kalman con residuales |
| `/api/pending/{id}` | GET | SI | Estado de un pending individual |
| `/query` | POST | SI | Query libre al LLM (modo chat) |
| `/inventory` | GET | SI | Stock de productos |
| `/sospechosos` | GET | SI | Auditoria Kalman |
| `/catalog` | GET | SI | Catalogo abstracto de productos |
| `/status/{id}` | GET | SI | Estado de un pending (alias) |
| `/docs` | GET | NO | Swagger UI (auto-generado por FastAPI) |
| `/openapi.json` | GET | NO | OpenAPI schema |

**Todos los `/api/*`, `/query`, `/inventory`, `/sospechosos`, `/catalog`,
`/status/{id}`, `/api/pending/{id}`** requieren `X-API-Key` si `API_KEY`
esta seteada en el servidor (recomendado en produccion).

### 2.1 Helper JS recomendado

```js
// lib/api.js (en el frontend)
const BASE = process.env.NEXT_PUBLIC_API_GATEWAY_URL || "http://localhost:8200";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";

export async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const r = await fetch(`${BASE}${path}`, { ...opts, headers });
  if (!r.ok) {
    const body = await r.text();
    throw new ApiError(r.status, body, path);
  }
  //  Si es audio stream, devolvemos el Response tal cual
  const ct = r.headers.get("content-type") || "";
  if (ct.startsWith("audio/")) return r;
  return r.json();
}

export class ApiError extends Error {
  constructor(status, body, path) {
    super(`API ${status} en ${path}: ${body.slice(0, 200)}`);
    this.status = status;
    this.body = body;
  }
}
```

---

### 2.2 `GET /health` (publico, sin auth)

Liveness probe + info del API.

```bash
curl http://localhost:8200/health
```

```json
{
  "status": "ok",
  "config": { "cloud_enabled": false, "llm": "needle", "stt": "whisper", "tts": "kokoro" },
  "services": {
    "needle": { "status": "ok", "model_loaded": true },
    "kokoro": { "status": "ok", "model_loaded": true }
  },
  "api": {
    "version": "3.0.0",
    "auth_required": false,
    "cors_origins": ["*"],
    "docs_url": "/docs",
    "openapi_url": "/openapi.json"
  }
}
```

**UI wiring**: este endpoint lo usa el frontend al arrancar para
verificar que el backend esta vivo y que la config coincide. Tambien es
el que usas para un healthcheck de Kubernetes / Docker.

```js
const h = await api("/health");
if (h.status !== "ok") showError("Backend no disponible");
// Mostrar en la UI: "Backend v3.0.0, cloud OFF, LLM: needle"
```

---

### 2.3 `GET/POST /api/config`

Ver o cambiar la config runtime (cloud toggle, overrides por backend,
narrator).

**GET**:
```bash
curl http://localhost:8200/api/config -H "X-API-Key: $API_KEY"
```

```json
{
  "cloud_enabled": false,
  "llm": "needle", "stt": "whisper", "tts": "kokoro",
  "narrator": "default",
  "narrator_model": "google/gemma-4-31b-it:free",
  "conversation_model": "google/gemma-4-31b-it:free",
  "llm_override": null, "stt_override": null, "tts_override": null,
  "narrator_override": null, "narrator_model_override": null,
  "defaults": { "llm": "needle", "stt": "whisper", "tts": "kokoro" }
}
```

**POST** (cambia):
```bash
curl -X POST http://localhost:8200/api/config \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"cloud_enabled": true}'
```

Body (todos los campos opcionales):
```json
{
  "cloud_enabled": true,            // toggle global
  "llm": "openrouter",              // "needle" | "openrouter" | "auto"
  "stt": "elevenlabs",              // "whisper" | "elevenlabs" | "auto"
  "tts": "elevenlabs",              // "kokoro" | "elevenlabs" | "auto"
  "narrator": "llm",                // "default" | "llm" | "auto"
  "narrator_model": "google/gemma-4-31b-it:free"  // cualquier slug
}
```

**UI wiring**: el panel de "Configuracion" del admin.
- Toggle "Usar cloud" -> `POST {cloud_enabled: bool}`
- Selector "LLM" -> `POST {llm: "openrouter"}`
- Selector "Voz del narrador" -> `POST {narrator_model: "..."}`

El `cloud toggle` global activa/desactiva TODOS los backends cloud a la
vez. Los overrides individuales (`llm=openrouter`) tienen prioridad sobre
el toggle.

---

### 2.4 `GET /api/models`

Lista modelos de OpenRouter disponibles.

```bash
curl "http://localhost:8200/api/models" -H "X-API-Key: $API_KEY"
```

Query params:
- `all=true` para listar todos los 350+ modelos (requiere OPENROUTER_API_KEY)
- `category=llm` para filtrar

```json
{
  "models": [
    { "slug": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash",
      "cost_in": 0.094, "cost_out": 0.188, "free": false,
      "tagline": "Smart, tool calling solido" },
    { "slug": "google/gemma-4-31b-it:free", "name": "Gemma 4 31B (free)",
      "cost_in": 0, "cost_out": 0, "free": true, "tagline": "Free tier" }
  ],
  "current": { "narrator_backend": "llm", "narrator_model": "google/gemma-4-31b-it:free" },
  "defaults": { "narrator": "default", "narrator_model": "google/gemma-4-31b-it:free" }
}
```

**UI wiring**: el selector de modelo del narrador.
```jsx
<select onChange={e => apiSelectModel(e.target.value)}>
  {models.map(m => (
    <option key={m.slug} value={m.slug}>
      {m.name} {m.free ? "(FREE)" : `$${m.cost_in}/M`}
    </option>
  ))}
</select>
```

---

### 2.5 `POST /api/models/select`

Cambia el modelo del narrador en runtime.

```bash
curl -X POST http://localhost:8200/api/models/select \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"category": "narrator", "model": "deepseek/deepseek-v4-flash"}'
```

Body:
```json
{ "category": "narrator", "model": "google/gemma-4-31b-it:free" }
// o {"category": "narrator", "model": null}  para volver al default
```

Devuelve la config actualizada.

**UI wiring**: boton "Aplicar" en el selector de modelo del narrador.

---

### 2.6 `POST /api/narrate`

Convierte un evento estructurado en texto natural en espanol (para TTS).

```bash
curl -X POST http://localhost:8200/api/narrate \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "event": "aceptada",
    "data": {
      "producto": "papa", "cantidad": 5, "unidad": "Kilogram",
      "stock_actual": 130, "bodega": "Bodega 1"
    }
  }'
```

Eventos validos: `aceptada`, `sospechosa`, `confirmada`, `rechazada`,
`consulta`, `sospechosos`, `invalid`, `no_action`, `registrar_manual`.

```json
{
  "text": "Buenísimo, sumamos 5 kilos de papa. Te quedan 130 kilos en Bodega 1.",
  "event": "aceptada",
  "backend": "llm",
  "model": "google/gemma-4-31b-it:free"
}
```

**UI wiring**: el frontend NO deberia llamar a `/api/narrate` directamente.
En su lugar, hace el flujo completo:
1. POST al endpoint de accion (`/query` o `/api/sesion/registrar-manual`)
2. Cuando el pending se resuelve con exito, llama a `/api/narrate` con el
   evento para obtener el texto a reproducir
3. Reproduce el texto via `/api/audio/speak`

---

### 2.7 `GET /api/audio/voices`

Lista las voces TTS disponibles del backend activo.

```bash
curl http://localhost:8200/api/audio/voices -H "X-API-Key: $API_KEY"
```

```json
{
  "voices": [
    { "voice_id": "kokoro_default", "name": "Kokoro (local)",
      "category": "local", "labels": { "lang": "es" },
      "preview_url": null }
  ],
  "default_voice_id": "kokoro_default",
  "backend": "kokoro"
}
```

Si elevenlabs-service esta activo, devuelve las 50+ voces de Eleven Labs
(ingles, espanol, etc) con su `preview_url` para escucharlas.

**UI wiring**: el selector de voz para el TTS. Mostrar las voces en una
lista con un boton "Play" que reproduce el `preview_url` (si lo tiene).

```jsx
<select>
  {voices.map(v => (
    <option key={v.voice_id} value={v.voice_id}>
      {v.name} ({v.labels?.lang || "?"})
    </option>
  ))}
</select>
```

---

### 2.8 `POST /api/audio/transcribir`

Audio -> texto (STT). Acepta cualquier formato que soporte el backend
(webm, mp3, wav, m4a, etc). Devuelve `{text, backend, language, ...}`.

```bash
curl -X POST http://localhost:8200/api/audio/transcribir \
  -H "X-API-Key: $API_KEY" \
  -F "file=@grabacion.webm" \
  -F "language_code=es"
```

```json
{
  "text": "metele cinco kilos de papa",
  "language": "es",
  "backend": "whisper",
  "backend_requested": "whisper",
  "fallback_used": false
}
```

**UI wiring**: el boton del microfono en la UI.
```js
async function transcribir(blob) {
  const fd = new FormData();
  fd.append("file", blob, "grabacion.webm");
  fd.append("language_code", "es");
  const r = await fetch(`${BASE}/api/audio/transcribir`, {
    method: "POST",
    body: fd,
    headers: { "X-API-Key": API_KEY },  // NO Content-Type, fetch lo setea
  });
  return r.json();
}

// Uso desde el navegador: MediaRecorder
const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
const recorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
// ... al detener:
const blob = new Blob([...], { type: "audio/webm" });
const { text } = await transcribir(blob);
// Despues del texto transcrito, enviar a /query o /api/sesion/registrar-voz
```

**IMPORTANTE**: NO setear `Content-Type` manualmente al hacer upload.
`fetch` lo setea automaticamente con el `boundary` correcto para multipart.

---

### 2.9 `POST /api/audio/speak`

Texto -> audio (TTS). Devuelve un stream de audio (PCM o MP3 segun
backend). Headers de respuesta: `X-Sample-Rate`, `X-Channels`, `X-Backend`,
`X-Backend-Requested`, `X-Fallback-Used`, `X-Voice-Id` (si elevenlabs).

```bash
curl -X POST http://localhost:8200/api/audio/speak \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hola, soy B-Link", "voice_id": "21m00Tcm4TlvDq8ikWAM"}' \
  --output audio.pcm
```

Body:
```json
{
  "text": "Hola, soy B-Link",
  "voice_id": "21m00Tcm4TlvDq8ikWAM",  // opcional, default del backend
  "speed": 1.0                            // opcional, solo Kokoro
}
```

**UI wiring**: reproducir el audio con un `<audio>` element o Web Audio API.

```js
async function speak(text, voiceId) {
  const r = await api("/api/audio/speak", {
    method: "POST",
    body: JSON.stringify({ text, voice_id: voiceId }),
  });
  //  r es un Response con el body de audio
  const sampleRate = parseInt(r.headers.get("X-Sample-Rate") || "24000");
  const backend = r.headers.get("X-Backend");
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.play();
  return { audio, backend, sampleRate };
}
```

Para mostrar en la UI de que backend se uso (utile para debug):
```jsx
{audio && <p>TTS: {audio.backend} @ {audio.sampleRate}Hz</p>}
```

---

### 2.10 `GET /api/bodegas`

Lista todas las bodegas.

```bash
curl http://localhost:8200/api/bodegas -H "X-API-Key: $API_KEY"
```

```json
[
  { "id": 1, "nombre": "bodega_default" },
  { "id": 2, "nombre": "Bodega Cocina" }
]
```

**UI wiring**: el dropdown "Seleccionar bodega" en la pantalla principal.

---

### 2.11 `GET /api/catalogo/bodega/{id}`

Productos en una bodega con su estado de conteo (si se pasa `sesion_id`).

```bash
curl "http://localhost:8200/api/catalogo/bodega/1?sesion_id=5" \
  -H "X-API-Key: $API_KEY"
```

```json
[
  { "id": 42, "nombre": "HARINA DE TRIGO", "codigo_articulo": "HR-001",
    "unidad": "Kilogram", "stock_sistema": 125.5, "stock_contado": 130,
    "estado_conteo": "alerta" },
  { "id": 43, "nombre": "ACEITE", "unidad": "Liter",
    "stock_sistema": 50, "stock_contado": null, "estado_conteo": "pendiente" }
]
```

Query params:
- `q=` filtro por nombre (ILIKE)
- `solo_pendientes=true` solo productos no contados
- `sesion_id=N` muestra el stock contado de esa sesion

**UI wiring**: la tabla de productos en la pantalla de conteo. Las
columnas son: nombre, stock sistema, stock contado, estado (badge verde /
amarillo / rojo). Filtrable por texto y por estado.

---

### 2.12 `POST /api/sesion/iniciar`

Crea una sesion de conteo en una bodega.

```bash
curl -X POST http://localhost:8200/api/sesion/iniciar \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"bodega_id": 1, "iniciada_por": "Carlos"}'
```

```json
{
  "sesion_id": 5,
  "bodega_id": 1,
  "estado": "activa",
  "creado_en": "2026-07-25T18:00:00Z",
  "total_productos": 287
}
```

**UI wiring**: boton "Iniciar conteo" en la pantalla de seleccion de
bodega. Despues de iniciar, navega a `/contar/{sesion_id}`.

---

### 2.13 `POST /api/sesion/finalizar`

Cierra la sesion, devuelve stats finales.

```bash
curl -X POST http://localhost:8200/api/sesion/finalizar \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sesion_id": 5}'
```

```json
{
  "sesion_id": 5,
  "estado": "finalizada",
  "total_productos": 287,
  "contados": 285,
  "aceptados": 280,
  "alertas": 5,
  "pendientes_kalman": 0
}
```

**UI wiring**: boton "Finalizar conteo" en la barra superior. Muestra
un modal con los stats y navega al reporte (`/reporte/diferencias/5`).

---

### 2.14 `GET /api/sesion/{id}/estado`

Progreso en tiempo real. El frontend lo pollea cada 2-3 segundos.

```bash
curl http://localhost:8200/api/sesion/5/estado -H "X-API-Key: $API_KEY"
```

```json
{
  "sesion_id": 5,
  "bodega_id": 1,
  "estado": "activa",
  "iniciada_por": "Carlos",
  "creado_en": "2026-07-25T18:00:00Z",
  "total_productos": 287,
  "contados": 145,
  "aceptados": 140,
  "alertas": 5,
  "pendientes": 142
}
```

**UI wiring**: barra de progreso en la pantalla de conteo. Usar
`setInterval` cada 2 segundos para actualizar.

```js
useEffect(() => {
  const t = setInterval(async () => {
    const e = await api(`/api/sesion/${sesionId}/estado`);
    setProgress(e);
  }, 2000);
  return () => clearInterval(t);
}, [sesionId]);
```

---

### 2.15 `POST /api/sesion/registrar-manual`

Modo Tablet: el usuario tipea producto + cantidad directamente.

```bash
curl -X POST http://localhost:8200/api/sesion/registrar-manual \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sesion_id": 5, "producto_id": 42, "cantidad": 5, "unidad": "kg"}'
```

```json
{
  "success": true,
  "pending_id": 123,
  "message": "Registro encolado. El worker Kalman lo evaluara."
}
```

**UI wiring**: el form de "Modo Tablet" (input para buscar producto +
input numerico para cantidad + selector de unidad). Despues de recibir
`pending_id`, el frontend pollea `/api/pending/{id}` hasta que se
resuelva (status != PENDING) y muestra el resultado.

---

### 2.16 `POST /api/sesion/registrar-voz`

Modo Voz: el usuario HABLO (ya transcripado) y se envia el texto.

```bash
curl -X POST http://localhost:8200/api/sesion/registrar-voz \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sesion_id": 5, "texto": "metele 5 kilos de papa"}'
```

Respuesta cuando matchea el regex fast-path:
```json
{
  "success": true,
  "pending_id": 124,
  "via": "regex_fastpath",
  "producto": "HARINA DE TRIGO",
  "cantidad": 5,
  "unidad": "Kilogram",
  "message": "Registro encolado via fast path."
}
```

Cuando NO matchea (va al LLM):
```json
{
  "success": true,
  "via": "llm",
  "backend": "cloud",
  "fallback_used": false,
  "tool_calls": [...],
  "pending": [...]
}
```

**UI wiring**: el flujo completo del modo voz:
1. Grabar audio (MediaRecorder) -> blob
2. POST a `/api/audio/transcribir` -> texto
3. Mostrar texto transcrito al usuario (para que confirme)
4. POST a `/api/sesion/registrar-voz` con el texto -> pending_id
5. Pollear `/api/pending/{id}` hasta resolver
6. Si `via=regex_fastpath`, ya esta confirmado -> mostrar toast verde
7. Si `via=llm`, mostrar el tool_call y pedir confirmacion visual
8. Si `decision=ACEPTADA`, reproducir narrate + speak
9. Si `decision=SOSPECHOSA`, mostrar dialog de confirmacion

---

### 2.17 `GET /api/reporte/diferencias/{sesion_id}`

Comparativo contado vs sistema + lista de no contados.

```bash
curl http://localhost:8200/api/reporte/diferencias/5 -H "X-API-Key: $API_KEY"
```

```json
{
  "sesion_id": 5,
  "contados": [
    { "nombre": "HARINA", "unidad": "kg", "stock_sistema": 125.5,
      "stock_contado": 130, "diferencia": 4.5, "decision_kalman": "ACEPTADA" }
  ],
  "no_contados": [
    { "nombre": "ACEITE", "unidad": "L", "stock_sistema": 50,
      "stock_contado": null, "diferencia": null, "decision_kalman": "no_contado" }
  ],
  "total_contados": 285,
  "total_pendientes": 2
}
```

**UI wiring**: la pantalla de "Reporte final" despues de finalizar la
sesion. Dos tablas: contados (con diff en rojo si hay) y no contados.

---

### 2.18 `GET /api/reporte/sospechosos/{sesion_id}`

Alertas Kalman con residuales.

```bash
curl http://localhost:8200/api/reporte/sospechosos/5 -H "X-API-Key: $API_KEY"
```

```json
{
  "sesion_id": 5,
  "sospechosos": [
    { "nombre": "HARINA", "unidad": "kg", "cantidad_contada": 200,
      "stock_sistema": 125, "diferencia": 75, "residual": 8.5,
      "umbral": 2.0, "decision": "SOSPECHOSA", "created_at": "..." }
  ],
  "total": 1
}
```

**UI wiring**: seccion de "Alertas" en el reporte final. Las alertas
requieren confirmacion manual (boton "Confirmar" / "Rechazar").

---

### 2.19 `GET /api/pending/{id}`

Estado de un pending individual.

```bash
curl http://localhost:8200/api/pending/123 -H "X-API-Key: $API_KEY"
```

```json
{
  "id": 123,
  "session_id": "5",
  "tool_name": "agregar_inventario",
  "status": "ACEPTADA",
  "decision": "...",
  "residual": 0.5,
  "umbral": 2.0,
  "movimiento_id": 456,
  "payload": {...},
  "created_at": "...",
  "resolved_at": "..."
}
```

Status posibles: `PENDING` (esperando Kalman), `ACEPTADA`, `SOSPECHOSA`,
`CONFIRMADA_MANUAL`, `RECHAZADA`.

**UI wiring**: polling. Loop con `setInterval` cada 200ms hasta que
`status != "PENDING"`. Si el polling tarda > 15s, mostrar "Procesando...".

```js
async function pollPending(id) {
  const start = Date.now();
  while (true) {
    const p = await api(`/api/pending/${id}`);
    if (p.status !== "PENDING") return p;
    if (Date.now() - start > 15000) throw new Error("Timeout");
    await new Promise(r => setTimeout(r, 200));
  }
}
```

---

### 2.20 `POST /query`

Query libre al LLM (modo chat sin bodega). El gateway decide si va a
B-Link (conversacional) o al main LLM (inventario).

```bash
curl -X POST http://localhost:8200/query \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "metele 5 kilos de papa",
    "session_id": "cli-abc123",
    "pending_alert": null,
    "bodega_id": 1
  }'
```

```json
{
  "backend": "cloud",         // "cloud" | "local" | "b-link" | "b-link-fallback"
  "backend_requested": "openrouter",
  "fallback_used": false,
  "fallback_reason": null,
  "tool_calls": [
    { "name": "agregar_inventario", "arguments": {"producto": "papa", "cantidad": 5, "unidad": "kg"} }
  ],
  "pending": [
    { "pending_id": 125, "tool_name": "agregar_inventario", "arguments": {...} }
  ],
  "raw_output": "Anotado, sumamos 5 kilos de papa..."
}
```

`backend` values:
- `cloud` / `local` → inventario procesado (ver `tool_calls` + `pending`)
- `b-link` → respuesta conversacional de B-Link (ver `raw_output`)
- `b-link-fallback` → B-Link caido (rate limit), uso respuesta hardcoded

**UI wiring**: el "input libre" del chat. Mostrar `raw_output` siempre.
Si hay `tool_calls`, mostrar lo que se hizo (boton "Ver detalle" abre
los argumentos). Si hay `pending`, pollear `/api/pending/{id}`.

---

### 2.21 `GET /inventory`, `/sospechosos`, `/catalog`, `/status/{id}`

Endpoints simples de lectura.

```bash
# Stock (con o sin filtros)
curl "http://localhost:8200/inventory?bodega_id=1" -H "X-API-Key: $API_KEY"
curl "http://localhost:8200/inventory?producto=HARINA" -H "X-API-Key: $API_KEY"

# Auditoria Kalman (sospechosos historicos)
curl "http://localhost:8200/sospechosos?producto=HARINA" -H "X-API-Key: $API_KEY"

# Catalogo abstracto (1 fila por producto)
curl http://localhost:8200/catalog -H "X-API-Key: $API_KEY"

# Alias de /api/pending/{id}
curl http://localhost:8200/status/123 -H "X-API-Key: $API_KEY"
```

---

## 3. Errores y como manejarlos

| Status | Cuando | Accion del frontend |
|---|---|---|
| 200 | OK | Mostrar respuesta |
| 401 | API key invalida o faltante | Re-login / refresh key |
| 404 | Endpoint o sesion no existe | Mostrar error generico |
| 422 | Body invalido (Pydantic) | Mostrar el detalle de Pydantic |
| 500 | Error del servidor | Retry una vez, despues mostrar error |
| 502 | Backend caido (cloud->local fallback) | Mostrar warning + raw_output |
| 503 | Servicio requerido no esta corriendo | Mostrar error claro |

**Estructura de error** (FastAPI default):
```json
{ "detail": "mensaje del error" }
```

**Patron recomendado**:
```js
try {
  const r = await api(path);
  // exito
} catch (e) {
  if (e instanceof ApiError) {
    if (e.status === 401) router.push("/login");
    else if (e.status === 502) showWarning("Cloud caido, usando local");
    else showError(e.message);
  } else {
    showError("Error de red: " + e.message);
  }
}
```

---

## 4. Flujo end-to-end recomendado (UX completa)

### 4.1 Pantalla principal (seleccion de bodega + iniciar conteo)

```
[Bodega: dropdown]  [Empezar conteo]
                ↓ onClick
              POST /api/sesion/iniciar
                ↓
              router.push("/contar/{sesion_id}")
```

### 4.2 Pantalla de conteo (la mas importante)

```
+-----------------------------------+
|  Sesion #5 - Bodega Cocina        |
|  Progreso: 145/287  [#######  ]   |  ← poll /api/sesion/{id}/estado cada 2s
+-----------------------------------+
|  [🎤 Voz]  [⌨️ Manual]  [🔍 Buscar]  |  ← tabs
+-----------------------------------+
|  Productos:                        |
|   □ HARINA         125kg            |
|     → contado 130kg ✅              |
|   □ ACEITE          50L             |
|   □ TOMATE          30u             |
+-----------------------------------+
|  [📊 Reporte]  [⏹ Finalizar]      |
+-----------------------------------+
```

Para el **modo voz** (boton 🎤):
```js
async function modoVoz() {
  const blob = await grabarAudio();  // MediaRecorder
  const { text } = await api("/api/audio/transcribir", {
    method: "POST", body: formDataCon(blob),
  });
  mostrarTextoReconocido(text);  // "metele 5 kilos de papa"
  const r = await api(`/api/sesion/${sesionId}/registrar-voz`, {
    method: "POST", body: JSON.stringify({ sesion_id: sesionId, texto: text }),
  });
  if (r.via === "regex_fastpath") {
    showToast(`✅ ${r.producto}: ${r.cantidad} ${r.unidad}`);
  } else {
    const p = await pollPending(r.pending[0].pending_id);
    if (p.status === "ACEPTADA") showToast("✅ Listo");
    else if (p.status === "SOSPECHOSA") mostrarDialogoConfirmacion(p);
  }
  // Reproducir narracion con TTS
  const { text: narr } = await api("/api/narrate", {
    method: "POST", body: JSON.stringify({ event: "aceptada", data: { ...r, ...p } }),
  });
  await speak(narr);  // reproduce el audio
}
```

Para el **modo manual** (boton ⌨️):
```jsx
<form onSubmit={handleSubmit}>
  <input list="productos" onChange={e => setProductoId(...)} />
  <input type="number" value={cantidad} onChange={e => setCantidad(...)} />
  <select value={unidad}>{/* opciones segun producto */}</select>
  <button type="submit">Registrar</button>
</form>
```

Para la **busqueda** (boton 🔍):
```js
// Input con debounce
const r = await api(`/api/catalogo/bodega/${bodegaId}?q=${query}&sesion_id=${sesionId}`);
```

### 4.3 Pantalla de reporte

```
[POST /api/reporte/diferencias/{sesion_id}]
[POST /api/reporte/sospechosos/{sesion_id}]
```

Dos tablas lado a lado: contados (con diff) + no contados. Los
sospechosos van arriba con un boton "Confirmar / Rechazar" cada uno.

---

## 5. CORS y auth en produccion

### Servidor (`.env` del gateway):
```bash
ALLOWED_ORIGINS=https://b-link.tu-dominio.com,https://www.b-link.tu-dominio.com
API_KEY=sk_prod_cambiar_esta_key_larga_y_random_para_produccion_2026
```

### Frontend (.env del frontend, ej Next.js):
```bash
NEXT_PUBLIC_API_GATEWAY_URL=https://api.b-link.tu-dominio.com
NEXT_PUBLIC_API_KEY=sk_prod_...
```

**IMPORTANTE**: si el frontend es un SPA publico, NO expongas la
`API_KEY` en el bundle. En su lugar:
- Pon el frontend detras de un BFF (Next.js API routes, Nuxt server
  routes, etc) que tenga la key server-side
- O usa un origin verificado (subdomain del mismo dominio) y
  autenticacion por cookie + CSRF token

Para una demo / hackathon esta bien hardcodear la key en el .env del
frontend. Para produccion real, BFF o cookie-based auth.

---

## 6. OpenAPI / Swagger

FastAPI genera la documentacion interactiva automaticamente. Disponible en:

- `http://localhost:8200/docs` — Swagger UI
- `http://localhost:8200/openapi.json` — OpenAPI 3.0 schema

El schema OpenAPI se puede usar para generar clientes tipados
automaticamente:
```bash
npx openapi-typescript http://localhost:8200/openapi.json -o ./lib/api-types.ts
```

Esto te da tipos TypeScript para todos los endpoints sin escribir nada
a mano.

---

## 7. Checklist para el equipo de frontend

Antes de mergear el PR:

- [ ] Maneja el caso `backend="b-link-fallback"` con un toast "B-Link
      tuvo un problema, usando respuesta local" (asi el usuario sabe
      que la respuesta no vino del LLM).
- [ ] Si el usuario navega a `/contar/{sesion_id}` sin bodega
      seleccionada, redirigir a `/`.
- [ ] Polling de `/api/sesion/{id}/estado` cada 2s mientras la sesion
      este activa. Limpiar el interval al desmontar.
- [ ] Polling de `/api/pending/{id}` cada 200ms hasta resolver. Si
      > 15s, mostrar "Procesando..." y seguir esperando.
- [ ] Si la respuesta de `/api/narrate` tiene `backend=llm`, mostrar
      un indicator sutil "B-Link formulo esta respuesta" (para
      transparencia).
- [ ] Cache local del resultado de `/api/models` (cambian poco).
- [ ] Al cambiar voz del TTS, validar que `voice_id` exista con
      `/api/audio/voices` antes de mandar a `/api/audio/speak`.
- [ ] Si el audio del TTS falla al reproducirse, mostrar el `raw_output`
      como texto (asi el usuario igual ve la respuesta).
- [ ] Manejar el caso de `decision="SOSPECHOSA"` con un dialog
      visual: "Esto se ve raro. Confirmas?" con botones Si / No.

---

## 8. Comandos utiles para el equipo de frontend

```bash
# Ver todos los endpoints (Swagger)
open http://localhost:8200/docs

# Probar un endpoint rapido
curl -X POST http://localhost:8200/query \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "hola"}'

# Probar transcribir
curl -X POST http://localhost:8200/api/audio/transcribir \
  -H "X-API-Key: $API_KEY" \
  -F "file=@grabacion.webm"

# Probar TTS
curl -X POST http://localhost:8200/api/audio/speak \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "hola mundo"}' --output /tmp/audio.pcm

# Generar cliente tipado
npx openapi-typescript http://localhost:8200/openapi.json -o ./lib/api-types.ts

# Ver el estado del server
curl http://localhost:8200/health | jq .
```

---

## 9. Diagrama de flujo end-to-end

```
[Browser]                                                    [Server]
   |                                                              |
   |--- GET /health (al cargar) ------------------------------->|
   |<-- {status: ok, config: {...}, api: {...}} --------------|
   |                                                              |
   |--- GET /api/bodegas ------------------------------------->|
   |<-- [{id:1, nombre:"..."}, ...] ----------------------------|
   |                                                              |
   |--- POST /api/sesion/iniciar ----------------------------->|
   |<-- {sesion_id: 5, ...} -----------------------------------|
   |                                                              |
   |--- GET /api/catalogo/bodega/1?sesion_id=5 ------------->|
   |<-- [{producto, stock_sistema, estado_conteo}, ...] -------|
   |                                                              |
   |  [Usuario hace click en "Voz" y habla]                      |
   |--- POST /api/audio/transcribir (multipart) ------------>|
   |<-- {text: "metele 5 kilos de papa", ...} ------------------|
   |                                                              |
   |--- POST /api/sesion/registrar-voz ------------------------>|
   |<-- {success: true, via: "regex_fastpath", pending_id: 124}-|
   |                                                              |
   |--- GET /api/pending/124 (cada 200ms) -------------------->|
   |<-- {status: "ACEPTADA", ...} -----------------------------|
   |                                                              |
   |--- POST /api/narrate {event: "aceptada", data: {...} ---->|
   |<-- {text: "Listo, sumamos 5 kilos...", backend: "llm"} ---|
   |                                                              |
   |--- POST /api/audio/speak {text: "..."} ------------------>|
   |<-- stream audio/pcm ---------------------------------------|
   |  [reproduce]                                                  |
```
