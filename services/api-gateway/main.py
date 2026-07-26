"""api-gateway: punto de entrada unico para la CLI y la PWA.

Responsabilidades:
- Enrutar a los backends de LLM (needle/openrouter), STT (whisper/elevenlabs)
  y TTS (kokoro/elevenlabs) segun un toggle runtime + overrides por-backend.
- Si el toggle cloud esta activo, intentar primero el backend cloud y caer
  al local en caso de error (HTTP 502/503/504, timeout, request error).
- Exponer proxies HTTP para audio (transcribir/speak) backend-agnosticos:
  la CLI/PWA nunca habla directo con voice-service ni kokoro-service ni
  elevenlabs-service — siempre pasa por aca.
- Exponer /api/config para que la UI consulte y modifique el toggle runtime.
- Exponer /api/narrate para que la CLI/PWA obtenga frases naturales
  (templates por default, LLM reescritor con Gemma 4 31B free si esta
  activado).
- Exponer /api/models para listar y seleccionar modelos en runtime.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, File, HTTPException, UploadFile, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

#  Carga .env / .env.example ANTES de leer cualquier os.getenv().
#  Prioridad: shell env > .env > .env.example.
from llm_common.env_loader import load_env
load_env()

from llm_common.db import (
    fetch,
    fetchrow,
    close_pool,
    enqueue_pending,
    enqueue_registro_manual,
    get_catalogo_bodega,
    get_pending_status,
)
from llm_common import nlu
from llm_common.narrator import Narrator, NarrateEvent, NarratorConfig

import re as _re

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api-gateway")

# =====================================================================
#  B-Link: el asistente conversacional (OpenRouter free model)
# =====================================================================
#  Cuando la query NO parece ser de inventario, el gateway la manda al
#  free model de OpenRouter (default: Gemma 4 31B free) que sabe que es
#  "B-Link, el asistente de inventario en un parpadeo". Asi el main LLM
#  (DeepSeek V4 Flash, de pago) solo se gasta en tool calls reales.
# =====================================================================

CONVERSATION_MODEL = os.getenv("CONVERSATION_MODEL", "google/gemma-4-31b-it:free")

# =====================================================================
#  Configuracion HTTP para el frontend
# =====================================================================
#  ALLOWED_ORIGINS: lista separada por comas de origins que pueden
#  llamar al gateway desde el browser (CORS). Default "*" para dev;
#  en produccion conviene poner el dominio real del frontend.
#  Ej: ALLOWED_ORIGINS=https://b-link.app,https://www.b-link.app
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

#  API_KEY: si esta seteada, todos los endpoints requieren el header
#  X-API-Key: <valor>. Si esta vacia, no hay auth (util para dev local).
#  En produccion SIEMPRE setearla.
API_KEY = os.getenv("API_KEY", "").strip()

B_LINK_PROMPT = """Sos B-Link, el asistente de inventario en un parpadeo. Hablas en espanol rioplatense, breve, calido y con onda.

Tu rol:
- Sos la cara conversacional del sistema de inventario. Saludas al usuario, charlas, respondes preguntas generales.
- NO ejecutas acciones de inventario vos mismo: el sistema principal (con herramientas) se encarga de agregar, sacar, consultar stock, etc.
- Si el usuario te pide algo de inventario, no inventes respuestas: derivá al sistema principal con un ejemplo ("decime 'agregar 5 kilos de papa' y yo me encargo").

Reglas:
- 1-2 oraciones maximo. Si la pregunta amerita mas, expandi un poco pero sin volar.
- Sin emojis (o muy de vez en cuando).
- Si te preguntan tu nombre, soy B-Link, asistente de inventario.
- Si no sabes algo, decilo y sugerí como averiguarlo.
- NUNCA inventes numeros de stock, productos o bodegas.
"""

#  Palabras clave para detectar queries de inventario (regex rapido, sin
#  gastar un LLM call). Si matchea, va al main LLM con tool_choice=required.
#  Si NO matchea, va al modelo conversacional (B-Link).
_INVENTORY_KEYWORDS = _re.compile(
    r"\b("
    #  verbos de escritura
    r"agreg[aeo]?r?|met[aeo]?|sac[aeo]?|quit[aeo]?|remov[aeo]?|rest[aeo]?|"
    r"vend[aeo]?|compr[aeo]?|ingres[aeo]?|anot[aeo]?|carg[aeo]?|"
    #  consultas / lecturas
    r"cu[aá]nto[^\s]* hay|cu[aá]nto[^\s]* queda|cu[aá]nto[^\s]* ten[eé]s|"
    r"hay (algo|stock|producto|movimiento)|"
    r"stock|inventario|producto[^\s]*s?|"
    #  alertas / kalman
    r"sospechos[oa]s?|movimiento[^\s]*s? raro|alerta[^\s]*s?|confirm[aeo]?r?|rechaz[aeo]?|"
    #  unidades
    r"kilo[^\s]*s?|gramo[^\s]*s?|litro[^\s]*s?|unidad(?:es)?|caja[^\s]*s?|"
    r"sobre[^\s]*s?|pieza[^\s]*s?|frasco[^\s]*s?|paquete[^\s]*s?|rollo[^\s]*s?"
    r")\b",
    _re.IGNORECASE,
)


def _is_inventory_query(text: str) -> bool:
    """True si la query parece ser de inventario (debe ir al main LLM con tools)."""
    return bool(_INVENTORY_KEYWORDS.search(text or ""))


async def _call_conversation_model(query: str) -> tuple[str | None, str | None]:
    """Llama al free model de OpenRouter para charlar. Devuelve (texto, error).

    Si OpenRouter no esta disponible (sin key, error de red, rate limit, etc),
    devuelve (None, mensaje_de_error) y el caller decide que hacer.
    Reintenta 1 vez con backoff si recibe 429 (rate limit comun en free tier).
    """
    if not OPENROUTER_API_KEY:
        return None, "OPENROUTER_API_KEY not configured"
    import asyncio as _asyncio
    last_err: str | None = None
    for attempt in range(2):  # 1 retry
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": CONVERSATION_MODEL,
                        "messages": [
                            {"role": "system", "content": B_LINK_PROMPT},
                            {"role": "user", "content": query},
                        ],
                        "temperature": 0.7,
                        "max_tokens": 200,
                    },
                )
            if r.status_code == 429:
                last_err = f"http 429 (rate limited, intento {attempt + 1}/2)"
                logger.warning("B-Link rate-limited, intento %d/2", attempt + 1)
                if attempt < 1:
                    await _asyncio.sleep(2.0)  # backoff
                    continue
                return None, last_err
            if r.status_code != 200:
                err = r.text[:200]
                logger.warning("conversation model http %d: %s", r.status_code, err)
                return None, f"http {r.status_code}"
            data = r.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
            return text, None
        except Exception as e:
            logger.warning("conversation model error: %s", e)
            return None, str(e)
    return None, last_err


#  Fallback hardcoded para cuando B-Link no puede responder (rate limit,
#  sin internet, etc). Asi el usuario SIEMPRE recibe una respuesta de B-Link.
_B_LINK_FALLBACK_RESPONSES = [
    "Hola, soy B-Link, el asistente de inventario en un parpadeo. "
    "Decime que necesitas (ej: 'agregar 5 kilos de papa') y me pongo a trabajar.",
    "Buenas. Soy B-Link. Estoy aca para ayudarte con el inventario: "
    "agregar, sacar, consultar stock, investigar movimientos. Que necesitás?",
    "Acá ando. Soy B-Link. El sistema principal se encarga de las acciones de "
    "inventario, yo soy la cara charlatana mientras el modulo conversacional "
    "vuelve del rate limit. Que decis?",
]


def _b_link_hardcoded(query: str) -> str:
    """Fallback cuando el free model no responde. Respuesta corta en el estilo B-Link."""
    q = (query or "").lower().strip()
    if any(w in q for w in ("hola", "buenas", "buen dia", "buen dia", "que tal", "hello", "hi")):
        return _B_LINK_FALLBACK_RESPONSES[0]
    if any(w in q for w in ("como te llamas", "quien sos", "que sos", "your name")):
        return "Soy B-Link, el asistente de inventario en un parpadeo."
    if any(w in q for w in ("gracias", "thanks", "muchas gracias")):
        return "De nada. Cuando necesites, aca estoy."
    #  Default: redirigir al sistema principal
    return ("Estoy con el modulo conversacional caido (rate limit). "
            "Pero el sistema principal de inventario funciona: "
            "proba con 'agregar 5 kilos de papa' o 'cuanto hay de tomate'.")

# =====================================================================
#  Configuracion por env (defaults al arranque)
# =====================================================================

LLM_BACKEND = os.getenv("LLM_BACKEND", "needle").lower()  # needle | openrouter
STT_BACKEND = os.getenv("STT_BACKEND", "whisper").lower()  # whisper | elevenlabs
TTS_BACKEND = os.getenv("TTS_BACKEND", "kokoro").lower()   # kokoro  | elevenlabs
NARRATOR_BACKEND = os.getenv("NARRATOR_BACKEND", "default").lower()  # default | llm
NARRATOR_MODEL = os.getenv("NARRATOR_MODEL", "google/gemma-4-31b-it:free")
OPENROUTER_BASE = os.getenv("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

NEEDLE_URL = os.getenv("NEEDLE_URL", "http://needle-service:8081")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "http://openrouter-service:8082")
VOICE_URL = os.getenv("VOICE_URL", "http://voice-service:8100")  # host:port (sin /ws)
KOKORO_URL = os.getenv("KOKORO_URL", "http://kokoro-service:8205")
ELEVENLABS_URL = os.getenv("ELEVENLABS_URL", "http://elevenlabs-service:8206")
KALMAN_URL = os.getenv("KALMAN_URL", "http://kalman-worker:8300")

VOICE_WS_URL = VOICE_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/transcribe"

# =====================================================================
#  Estado runtime (toggle cloud + overrides por-backend, en memoria)
# =====================================================================

_cloud_toggle: bool = os.getenv("CLOUD_ENABLED", "false").lower() == "true"
_llm_override: str | None = None   # None = seguir el toggle
_stt_override: str | None = None
_tts_override: str | None = None
_narrator_override: str | None = None   # None = seguir el toggle
_narrator_model_override: str | None = None

_lock = asyncio.Lock()

#  Singleton del narrador (se reusa entre requests para aprovechar la cache)
_narrator: Narrator = Narrator(NarratorConfig(
    backend=NARRATOR_BACKEND,
    model=NARRATOR_MODEL,
    base_url=OPENROUTER_BASE,
    api_key=OPENROUTER_API_KEY,
))


def _pick_llm() -> str:
    if _llm_override in ("needle", "openrouter"):
        return _llm_override
    return "openrouter" if _cloud_toggle else LLM_BACKEND


def _pick_stt() -> str:
    if _stt_override in ("whisper", "elevenlabs"):
        return _stt_override
    return "elevenlabs" if _cloud_toggle else STT_BACKEND


def _pick_tts() -> str:
    if _tts_override in ("kokoro", "elevenlabs"):
        return _tts_override
    return "elevenlabs" if _cloud_toggle else TTS_BACKEND


def _pick_narrator() -> str:
    if _narrator_override in ("default", "llm"):
        return _narrator_override
    return "llm" if _cloud_toggle else NARRATOR_BACKEND


def _active_narrator_model() -> str:
    return _narrator_model_override or NARRATOR_MODEL


def _refresh_narrator() -> None:
    """Reconfigura el singleton del narrador con el estado actual."""
    _narrator.config.backend = _pick_narrator()
    _narrator.config.model = _active_narrator_model()
    _narrator.config.api_key = OPENROUTER_API_KEY
    _narrator.config.base_url = OPENROUTER_BASE


def _llm_url() -> str:
    return OPENROUTER_URL if _pick_llm() == "openrouter" else NEEDLE_URL


def _current_config() -> dict[str, Any]:
    return {
        "cloud_enabled": _cloud_toggle,
        "llm": _pick_llm(),
        "stt": _pick_stt(),
        "tts": _pick_tts(),
        "narrator": _pick_narrator(),
        "narrator_model": _active_narrator_model(),
        "conversation_model": CONVERSATION_MODEL,
        "llm_override": _llm_override,
        "stt_override": _stt_override,
        "tts_override": _tts_override,
        "narrator_override": _narrator_override,
        "narrator_model_override": _narrator_model_override,
        "defaults": {
            "llm": LLM_BACKEND, "stt": STT_BACKEND, "tts": TTS_BACKEND,
            "narrator": NARRATOR_BACKEND, "narrator_model": NARRATOR_MODEL,
            "conversation_model": CONVERSATION_MODEL,
        },
    }


# =====================================================================
#  Helpers de routing y fallback
# =====================================================================

async def _post_with_fallback(
    client: httpx.AsyncClient,
    *,
    cloud_url: str | None,
    local_url: str,
    payload: dict,
    want_cloud: bool,
    label: str,
    timeout: float = 60.0,
) -> tuple[httpx.Response, str, bool, str | None]:
    """POST con fallback cloud->local.

    Retorna (response, backend_usado, fallback_used, fallback_reason).
    Si want_cloud=True y cloud_url dado, intenta cloud primero; si falla,
    cae a local con fallback_used=True. Si want_cloud=False va directo a local.

    fallback_reason es un string corto explicando por que cayo a local
    (ej: "service_down:openrouter-service", "http_429:rate_limited",
    "dns_error", "timeout"). None si no hubo fallback o no se sabe.
    """
    if want_cloud and cloud_url:
        try:
            r = await client.post(cloud_url, json=payload, timeout=timeout)
            if r.status_code == 200:
                return r, "cloud", False, None
            #  HTTP no-200: log + fallback con motivo
            reason = _classify_http_error(r.status_code, cloud_url, r)
            logger.warning("%s cloud http %d, falling back (%s)", label, r.status_code, reason)
        except httpx.ConnectError as e:
            #  DNS error o "connection refused" -> el servicio cloud no esta corriendo
            host = cloud_url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
            reason = f"service_down:{host} (no esta corriendo; arrancalo con docker compose --profile with-{_profile_for_host(host)} up -d)"
            logger.warning("%s cloud service down: %s, falling back", label, e)
        except httpx.TimeoutException as e:
            reason = "timeout"
            logger.warning("%s cloud timeout, falling back: %s", label, e)
        except httpx.RequestError as e:
            reason = f"transport_error:{type(e).__name__}"
            logger.warning("%s cloud transport error: %s, falling back", label, e)
        r = await client.post(local_url, json=payload, timeout=timeout)
        return r, "local", True, reason
    r = await client.post(local_url, json=payload, timeout=timeout)
    return r, "local", False, None


def _profile_for_host(host: str) -> str:
    """Mapea hostname de un servicio cloud al nombre de perfil de docker compose."""
    return {
        "openrouter-service": "openrouter",
        "elevenlabs-service": "elevenlabs",
    }.get(host, "voice")


def _classify_http_error(status: int, url: str, response: httpx.Response) -> str:
    """Clasifica un HTTP error en un reason corto para mostrar al usuario."""
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    if status == 401 or status == 403:
        return f"http_{status}:api_key_invalida_o_sin_permiso ({host})"
    if status == 404:
        return f"http_404:endpoint_no_encontrado_en_{host}"
    if status == 429:
        #  Rate limit - intentar extraer el mensaje de OpenRouter si esta
        msg = ""
        try:
            err = response.json()
            raw = err.get("error", {}).get("metadata", {}).get("raw", "")
            if raw:
                msg = f" — {raw[:120]}"
        except Exception:
            pass
        return f"http_429:rate_limited{msg}"
    if status == 502 or status == 503 or status == 504:
        return f"http_{status}:upstream_no_disponible ({host})"
    return f"http_{status}"


# =====================================================================
#  App
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "api-gateway up. cloud=%s llm=%s stt=%s tts=%s",
        _cloud_toggle, _pick_llm(), _pick_stt(), _pick_tts(),
    )
    yield
    await close_pool()


app = FastAPI(title="api-gateway", version="3.0.0", lifespan=lifespan)

#  CORS para que el frontend (browser) pueda llamar al gateway.
#  En dev "*" esta bien; en produccion especificar los origins reales.
_cors_origins = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Backend", "X-Backend-Requested", "X-Fallback-Used",
        "X-Sample-Rate", "X-Channels", "X-Sample-Width", "X-Endian",
        "X-Voice-Id", "X-Model-Id", "Content-Length",
    ],
)


#  Auth: si API_KEY esta seteada, todos los endpoints (salvo /health)
#  requieren el header X-API-Key. Si esta vacia, no se chequea nada.
async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not API_KEY:
        return  # auth deshabilitada
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida o ausente")


#  Rutas publicas (sin auth): health, docs, openapi, redoc
#  Todo lo demas requiere API_KEY si esta seteada.
_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"}


@app.middleware("http")
async def auth_middleware(request, call_next):
    """Chequea API key para todas las rutas salvo las publicas."""
    if API_KEY and request.url.path not in _PUBLIC_PATHS:
        x_api_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
        if x_api_key != API_KEY:
            from fastapi.responses import JSONResponse as _J
            return _J(
                status_code=401,
                content={"detail": "API key invalida o ausente. Header requerido: X-API-Key"},
            )
    return await call_next(request)


# =====================================================================
#  Schemas
# =====================================================================

class QueryRequest(BaseModel):
    text: str
    session_id: str | None = None
    pending_alert: dict | None = None
    bodega_id: int | None = None


class QueryResponse(BaseModel):
    backend: str
    backend_requested: str
    fallback_used: bool
    fallback_reason: str | None = None
    tool_calls: list[dict] = []
    pending: list[dict] = []
    raw_output: str = ""


class ConfigUpdate(BaseModel):
    cloud_enabled: bool | None = None
    llm: str | None = None      # "needle" | "openrouter" | "auto"
    stt: str | None = None      # "whisper" | "elevenlabs" | "auto"
    tts: str | None = None      # "kokoro" | "elevenlabs" | "auto"
    narrator: str | None = None  # "default" | "llm" | "auto"
    narrator_model: str | None = None  # slug de OpenRouter, o "auto" para volver al default


# =====================================================================
#  /api/config  (toggle runtime)
# =====================================================================

_VALID_OVERRIDES = {
    "llm": ("needle", "openrouter"),
    "stt": ("whisper", "elevenlabs"),
    "tts": ("kokoro", "elevenlabs"),
    "narrator": ("default", "llm"),
}


@app.get("/api/config")
async def get_config():
    return _current_config()


@app.post("/api/config")
async def update_config(req: ConfigUpdate):
    global _cloud_toggle, _llm_override, _stt_override, _tts_override
    global _narrator_override, _narrator_model_override
    async with _lock:
        if req.cloud_enabled is not None:
            _cloud_toggle = bool(req.cloud_enabled)
        for field in ("llm", "stt", "tts", "narrator"):
            v = getattr(req, field)
            if v is None:
                continue
            v = v.lower()
            attr = f"_{field}_override"
            if v == "auto":
                globals()[attr] = None
            elif v in _VALID_OVERRIDES[field]:
                globals()[attr] = v
            else:
                raise HTTPException(400, f"{field} invalido: {v}")
        #  narrator_model es libre: cualquier slug de OpenRouter vale.
        #  Si viene vacio o "auto", vuelve al default.
        if req.narrator_model is not None:
            v = req.narrator_model.strip()
            if not v or v.lower() == "auto":
                _narrator_model_override = None
            else:
                _narrator_model_override = v
        _refresh_narrator()
        logger.info(
            "config update: cloud=%s llm=%s stt=%s tts=%s narrator=%s model=%s",
            _cloud_toggle, _pick_llm(), _pick_stt(), _pick_tts(),
            _pick_narrator(), _active_narrator_model(),
        )
    return _current_config()


# =====================================================================
#  Health
# =====================================================================

@app.get("/health")
async def health():
    out: dict[str, Any] = {
        "status": "ok",
        "config": _current_config(),
        "services": {},
        "api": {
            "version": "3.0.0",
            "auth_required": bool(API_KEY),
            "cors_origins": _cors_origins,
            "docs_url": "/docs",
            "openapi_url": "/openapi.json",
        },
    }
    async with httpx.AsyncClient(timeout=3) as client:
        targets = [
            ("needle", NEEDLE_URL),
            ("openrouter", OPENROUTER_URL),
            ("kokoro", KOKORO_URL),
            ("elevenlabs", ELEVENLABS_URL),
        ]
        if _pick_stt() == "whisper":
            targets.append(("voice", VOICE_URL))
        for name, url in targets:
            try:
                r = await client.get(f"{url}/health")
                out["services"][name] = r.json() if r.status_code == 200 else {"status": "down"}
            except Exception as e:
                out["services"][name] = {"status": "down", "error": str(e)}

    try:
        await fetchrow("SELECT 1 AS ok")
        out["db"] = "ok"
    except Exception as e:
        out["db"] = f"down: {e}"
    return out


# =====================================================================
#  /query  (LLM con fallback cloud->local)
# =====================================================================

@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    text = (req.text or "").strip()

    #  1) Si la query NO parece de inventario, va al modelo conversacional
    #     (B-Link via OpenRouter free). Asi el main LLM (pago) no se gasta
    #     en saludos y preguntas generales.
    #
    #  Excepcion: si viene pending_alert, es la resolucion de una alerta
    #  SOSPECHOSA (la CLI/frontend responde "si"/"no"/"dale"/"cancela").
    #  Esas palabras sueltas no matchean _INVENTORY_KEYWORDS y sin este
    #  bypass la confirmacion caia siempre al chat conversacional y la
    #  alerta jamas se resolvia.
    if not req.pending_alert and not _is_inventory_query(text):
        bl_text, bl_err = await _call_conversation_model(text)
        if bl_text is not None:
            return QueryResponse(
                backend="b-link",
                backend_requested="b-link",
                fallback_used=False,
                tool_calls=[],
                pending=[],
                raw_output=bl_text,
            )
        #  B-Link fallo (rate limit, sin internet, etc). Usamos la respuesta
        #  hardcoded como fallback final, asi el usuario SIEMPRE recibe una
        #  respuesta coherente de B-Link y no algo raro del LLM principal.
        logger.info("b-link no disponible (%s), usando fallback hardcoded", bl_err)
        return QueryResponse(
            backend="b-link-fallback",
            backend_requested="b-link",
            fallback_used=True,
            fallback_reason=bl_err,
            tool_calls=[],
            pending=[],
            raw_output=_b_link_hardcoded(text),
        )

        #  (El codigo de abajo solo corre si la query PARECE inventario.)
    pick = _pick_llm()
    payload: dict[str, Any] = {
        "query": text,
        "tools": "[]",
        "session_id": req.session_id or "default",
        "mode": "full",
    }
    if req.pending_alert:
        payload["pending_alert"] = req.pending_alert
    if req.bodega_id:
        payload["bodega_id"] = req.bodega_id

    async with httpx.AsyncClient(timeout=60) as client:
        if pick == "openrouter":
            r, used, fallback, reason = await _post_with_fallback(
                client,
                cloud_url=f"{OPENROUTER_URL}/infer",
                local_url=f"{NEEDLE_URL}/infer",
                payload=payload,
                want_cloud=True,
                label="LLM",
            )
        else:
            r, used, fallback, reason = await _post_with_fallback(
                client,
                cloud_url=None,
                local_url=f"{NEEDLE_URL}/infer",
                payload=payload,
                want_cloud=False,
                label="LLM",
            )

    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)

    data = r.json()
    return QueryResponse(
        backend=used,
        backend_requested=pick,
        fallback_used=fallback,
        fallback_reason=reason,
        tool_calls=data.get("tool_calls", []),
        pending=data.get("pending", []),
        raw_output=data.get("raw_output", ""),
    )


# =====================================================================
#  /api/audio/transcribir  (STT con fallback cloud->local)
# =====================================================================

@app.post("/api/audio/transcribir")
async def audio_transcribir(file: UploadFile = File(...)):
    pick = _pick_stt()
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "audio file vacio")
    fname = file.filename or "audio.webm"
    ctype = file.content_type or "application/octet-stream"

    async with httpx.AsyncClient(timeout=60) as client:
        if pick == "elevenlabs":
            files = {"file": (fname, raw, ctype)}
            data = {"language_code": "es"}
            try:
                r = await client.post(
                    f"{ELEVENLABS_URL}/transcribe",
                    files=files, data=data, timeout=60,
                )
                if r.status_code == 200:
                    out = r.json()
                    out["backend_requested"] = pick
                    out["fallback_used"] = False
                    return out
                logger.warning("STT elevenlabs http %d, falling back to whisper: %s",
                               r.status_code, r.text[:200])
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.warning("STT elevenlabs transport error: %s, falling back", e)
            # Fallback: Whisper local (necesita PCM 16k)
            pcm = await _ffmpeg_to_pcm16k(raw, ctype)
            if pcm is None:
                raise HTTPException(500, "no se pudo decodificar el audio para fallback local")
            text = await _whisper_ws_transcribe(pcm)
            return {
                "text": text,
                "language": "es",
                "backend": "whisper",
                "backend_requested": pick,
                "fallback_used": True,
            }
        # pick == "whisper"
        pcm = await _ffmpeg_to_pcm16k(raw, ctype)
        if pcm is None:
            raise HTTPException(400, "no se pudo decodificar el audio (formato no soportado)")
        text = await _whisper_ws_transcribe(pcm)
        return {
            "text": text,
            "language": "es",
            "backend": "whisper",
            "backend_requested": pick,
            "fallback_used": False,
        }


async def _ffmpeg_to_pcm16k(raw: bytes, content_type: str) -> bytes | None:
    """Convierte cualquier formato a PCM int16 LE mono 16kHz via ffmpeg.

    Devuelve None si ffmpeg falla o no esta disponible.
    """
    #  Si ya parece PCM crudo, devolver tal cual.
    if content_type.startswith("audio/L16") or content_type == "audio/pcm":
        return raw
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", "16000",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error("ffmpeg no instalado en el contenedor")
        return None
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=raw), timeout=30,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        logger.warning("ffmpeg rc=%d: %s", proc.returncode, err.decode("utf-8", "replace")[:300])
        return None
    return out


async def _whisper_ws_transcribe(pcm_bytes: bytes) -> str:
    """Envia PCM int16 16k mono al voice-service por WS y devuelve la transcripcion final."""
    url = VOICE_WS_URL
    try:
        async with websockets.connect(url, open_timeout=30, max_size=2**23) as ws:
            #  Enviar en chunks de ~100ms (3200 bytes) para imitar el flujo del CLI
            chunk = 3200
            for i in range(0, len(pcm_bytes), chunk):
                await ws.send(pcm_bytes[i:i + chunk])
            await ws.send(json.dumps({"type": "stop"}))
            while True:
                msg = await ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                t = data.get("type")
                if t == "final":
                    return (data.get("text") or "").strip()
                if t == "error":
                    raise HTTPException(502, f"voice-service: {data.get('message')}")
    except websockets.exceptions.WebSocketException as e:
        raise HTTPException(502, f"voice-service WS error: {e}")
    return ""


# =====================================================================
#  /api/audio/speak  (TTS con fallback cloud->local)
# =====================================================================

class SpeakRequest(BaseModel):
    text: str
    voice_id: str | None = None   # Eleven Labs voice (si backend=elevenlabs)
    speed: float | None = None    # Kokoro


@app.post("/api/audio/speak")
async def audio_speak(req: SpeakRequest):
    pick = _pick_tts()
    if pick == "elevenlabs":
        payload = {"text": req.text}
        if req.voice_id:
            payload["voice_id"] = req.voice_id
        #  Leemos el audio completo en memoria antes de streamearlo al cliente.
        #  Esto evita el bug donde el `async with client.stream(...)` cierra la
        #  conexion al backend al salir de la funcion, pero el generator que
        #  pasamos a StreamingResponse todavia esta leyendo de esa conexion.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10)) as client:
                r = await client.post(f"{ELEVENLABS_URL}/speak", json=payload)
                if r.status_code == 200:
                    return _build_tts_response(r, "elevenlabs", fallback=False)
                logger.warning("TTS elevenlabs http %d, falling back to kokoro",
                               r.status_code)
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logger.warning("TTS elevenlabs transport error: %s, falling back", e)
        # Fallback: Kokoro
        return await _kokoro_speak(req.text, req.speed, fallback=True)
    # pick == "kokoro"
    return await _kokoro_speak(req.text, req.speed, fallback=False)


async def _kokoro_speak(text: str, speed: float | None, fallback: bool) -> StreamingResponse:
    payload: dict[str, Any] = {"text": text}
    if speed is not None:
        payload["speed"] = speed
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10)) as client:
            r = await client.post(f"{KOKORO_URL}/speak", json=payload)
            if r.status_code != 200:
                raise HTTPException(r.status_code, f"kokoro error: {r.text[:200]}")
            return _build_tts_response(r, "kokoro", fallback=fallback)
    except httpx.RequestError as e:
        raise HTTPException(503, f"kokoro unreachable: {e}")


def _build_tts_response(upstream: httpx.Response, backend: str, fallback: bool) -> StreamingResponse:
    """Empaqueta el audio del backend en una StreamingResponse.

    Leemos el body completo en memoria (los TTS producen < 1MB por oracion)
    para evitar problemas con el ciclo de vida del cliente httpx hacia el
    backend cuando el cliente HTTP del gateway se desconecta mid-stream.
    """
    audio_bytes = upstream.content
    extra = {
        "X-Backend": backend,
        "X-Backend-Requested": "elevenlabs" if _pick_tts() == "elevenlabs" else "kokoro",
        "X-Fallback-Used": "true" if fallback else "false",
        "Content-Length": str(len(audio_bytes)),
    }
    for h in ("X-Sample-Rate", "X-Channels", "X-Sample-Width", "X-Endian", "X-Voice-Id", "X-Model-Id"):
        if h in upstream.headers:
            extra[h] = upstream.headers[h]
    ctype = upstream.headers.get("content-type", "audio/pcm")
    return Response(content=audio_bytes, media_type=ctype, headers=extra)


# =====================================================================
#  /api/audio/voices  (proxy a elevenlabs-service con cache server-side)
# =====================================================================

@app.get("/api/audio/voices")
async def audio_voices():
    pick = _pick_tts()
    if pick != "elevenlabs":
        #  Sin elevenlabs activo, devolvemos la voz default que Kokoro usaria
        return {
            "voices": [
                {
                    "voice_id": "kokoro_default",
                    "name": "Kokoro (local)",
                    "category": "local",
                    "labels": {"lang": "es"},
                    "preview_url": None,
                }
            ],
            "default_voice_id": "kokoro_default",
            "backend": "kokoro",
        }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ELEVENLABS_URL}/voices")
            if r.status_code != 200:
                raise HTTPException(r.status_code, f"elevenlabs voices: {r.text[:200]}")
            out = r.json()
            out["backend"] = "elevenlabs"
            return out
    except httpx.RequestError as e:
        raise HTTPException(503, f"elevenlabs unreachable: {e}")


# =====================================================================
#  /api/narrate  (texto natural para TTS)
# =====================================================================

class NarrateRequest(BaseModel):
    event: str                                    # NarrateEvent value
    data: dict = {}                               # campos del evento


@app.post("/api/narrate")
async def narrate(req: NarrateRequest):
    """Convierte un evento estructurado en una frase natural en espanol.

    Si el backend activo es "llm" y hay API key configurada, reescribe con
    el modelo OpenRouter seleccionado (default: gemma-4-31b-it:free).
    Si falla, cae al template hardcodeado (variaciones, no repetitivo).
    """
    try:
        event = NarrateEvent(req.event)
    except ValueError:
        valid = ", ".join(e.value for e in NarrateEvent)
        raise HTTPException(400, f"event invalido: {req.event!r}. validos: {valid}")

    _refresh_narrator()
    text = await _narrator.narrate(event, req.data)
    return {
        "text": text,
        "event": event.value,
        "backend": _pick_narrator(),
        "model": _active_narrator_model(),
    }


# =====================================================================
#  /api/models  (selector de modelos en runtime)
# =====================================================================

#  Modelos recomendados (curados). OpenRouter tiene muchos mas; mostramos
#  estos para no abrumar al usuario. Para ver todos: GET /api/models?all=true
_RECOMMENDED_MODELS = [
    {
        "slug": "deepseek/deepseek-v4-flash",
        "name": "DeepSeek V4 Flash",
        "category": "llm",
        "cost_in": 0.094, "cost_out": 0.188,
        "smart": True, "free": False, "tagline": "Smart, tool calling solido, ~$0.09/M in",
    },
    {
        "slug": "deepseek/deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "category": "llm",
        "cost_in": 0.435, "cost_out": 0.870,
        "smart": True, "free": False, "tagline": "V4 full, mas caro pero mas capaz",
    },
    {
        "slug": "google/gemma-4-31b-it:free",
        "name": "Gemma 4 31B (free)",
        "category": "llm",
        "cost_in": 0, "cost_out": 0,
        "smart": True, "free": True, "tagline": "Free tier de Google, ideal para narrador",
    },
    {
        "slug": "google/gemma-4-26b-a4b-it:free",
        "name": "Gemma 4 26B (free)",
        "category": "llm",
        "cost_in": 0, "cost_out": 0,
        "smart": True, "free": True, "tagline": "Free tier mas chico, mas rapido",
    },
    {
        "slug": "anthropic/claude-3.5-haiku",
        "name": "Claude 3.5 Haiku",
        "category": "llm",
        "cost_in": 0.80, "cost_out": 4.0,
        "smart": True, "free": False, "tagline": "Si preferis Anthropic",
    },
]


@app.get("/api/models")
async def list_models(all: bool = False, category: str | None = None):
    """Lista los modelos disponibles por categoria.

    Por default devuelve solo los recomendados (curados). Con ?all=true
    intenta listar TODOS los modelos de OpenRouter (hace falta API key).

    Categorias: llm, narrator (el narrador usa el mismo pool que llm).
    """
    if category and category not in ("llm", "narrator"):
        raise HTTPException(400, f"category invalida: {category}")

    items = list(_RECOMMENDED_MODELS)
    if all and OPENROUTER_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{OPENROUTER_BASE}/models",
                    headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                )
                if r.status_code == 200:
                    raw = r.json().get("data", [])
                    for m in raw:
                        pricing = m.get("pricing", {})
                        items.append({
                            "slug": m.get("id"),
                            "name": m.get("name"),
                            "category": "llm",
                            "cost_in": float(pricing.get("prompt", "0") or 0) * 1_000_000,
                            "cost_out": float(pricing.get("completion", "0") or 0) * 1_000_000,
                            "smart": True,
                            "free": float(pricing.get("prompt", "0") or 0) == 0,
                            "tagline": m.get("description", "")[:80],
                        })
        except Exception as e:
            logger.warning("error listando modelos de OpenRouter: %s", e)

    if category:
        items = [m for m in items if m["category"] == category]

    return {
        "models": items,
        "current": {
            "narrator_backend": _pick_narrator(),
            "narrator_model": _active_narrator_model(),
            "llm": _pick_llm(),
        },
        "defaults": {
            "narrator": NARRATOR_BACKEND,
            "narrator_model": NARRATOR_MODEL,
        },
    }


class ModelSelect(BaseModel):
    category: str               # "narrator" (extensible a "llm" en el futuro)
    model: str | None = None    # slug OpenRouter, o null/auto para reset


@app.post("/api/models/select")
async def select_model(req: ModelSelect):
    """Cambia el modelo activo de una categoria en runtime.

    Equivale a POST /api/config con el campo especifico, pero con un
    endpoint dedicado que es mas descubrible y self-documenting.
    """
    global _narrator_model_override
    if req.category == "narrator":
        if req.model and req.model.lower() != "auto":
            _narrator_model_override = req.model.strip()
        else:
            _narrator_model_override = None
        _refresh_narrator()
        return _current_config()
    raise HTTPException(400, f"category no soportada: {req.category!r}. usar 'narrator'")


# =====================================================================
#  Endpoints existentes (sin cambios funcionales)
# =====================================================================

@app.get("/status/{pending_id}")
async def status(pending_id: int):
    row = await fetchrow(
        """
        SELECT id, session_id, tool_name, status, decision,
               residual, umbral, movimiento_id, payload, created_at, resolved_at
        FROM pending_evaluations
        WHERE id = $1
        """,
        pending_id,
    )
    if not row:
        raise HTTPException(404, "pending not found")
    return row


@app.get("/catalog")
async def catalog(bodega_id: int | None = None):
    if bodega_id is not None:
        return await fetch(
            """
            SELECT DISTINCT
                pc.id, pc.nombre, pc.codigo_articulo, pc.unidad,
                pc.q_proceso, pc.r_medicion, pc.umbral_sigma
            FROM productos_catalogo pc
            JOIN stock s ON s.producto_id = pc.id
            WHERE s.bodega_id = $1
            ORDER BY pc.nombre
            """,
            bodega_id,
        )
    return await fetch(
        """SELECT id, nombre, codigo_articulo, unidad,
                  q_proceso, r_medicion, umbral_sigma
           FROM productos_catalogo
           ORDER BY nombre"""
    )


@app.get("/inventory")
async def inventory(
    producto: str | None = None,
    bodega_id: int | None = None,
):
    if producto and bodega_id is not None:
        row = await fetchrow(
            """SELECT peb.nombre, peb.bodega_id, b.nombre AS bodega,
                      peb.unidad, peb.stock_actual, peb.media_kalman, peb.varianza_kalman
               FROM productos_en_bodega peb
               JOIN bodegas b ON b.id = peb.bodega_id
               WHERE peb.nombre = $1 AND peb.bodega_id = $2""",
            producto, bodega_id,
        )
        if not row:
            raise HTTPException(404, f"producto '{producto}' no encontrado en bodega {bodega_id}")
        return row
    if producto:
        rows = await fetch(
            """SELECT peb.nombre, peb.bodega_id, b.nombre AS bodega,
                      peb.unidad, peb.stock_actual, peb.media_kalman, peb.varianza_kalman
               FROM productos_en_bodega peb
               JOIN bodegas b ON b.id = peb.bodega_id
               WHERE peb.nombre = $1
               ORDER BY b.nombre""",
            producto,
        )
        if not rows:
            raise HTTPException(404, f"producto '{producto}' no encontrado")
        return rows
    if bodega_id is not None:
        return await fetch(
            """SELECT peb.nombre, peb.codigo_articulo, peb.unidad,
                      peb.stock_actual, peb.media_kalman, peb.varianza_kalman
               FROM productos_en_bodega peb
               WHERE peb.bodega_id = $1
               ORDER BY peb.nombre""",
            bodega_id,
        )
    return await fetch(
        """SELECT id, nombre, codigo_articulo, unidad
           FROM productos_catalogo
           ORDER BY nombre"""
    )


@app.get("/sospechosos")
async def sospechosos(producto: str | None = None):
    return await fetch("SELECT * FROM investigar_sospechosos($1)", producto)


@app.get("/api/bodegas")
async def list_bodegas(q: str | None = None):
    if q:
        return await fetch(
            "SELECT id, nombre FROM bodegas WHERE nombre ILIKE $1 ORDER BY nombre",
            f"%{q}%",
        )
    return await fetch("SELECT id, nombre FROM bodegas ORDER BY nombre")


# =====================================================================
#  Sesiones de conteo
# =====================================================================

class IniciarSesionRequest(BaseModel):
    bodega_id: int
    iniciada_por: str = "anonimo"


class FinalizarSesionRequest(BaseModel):
    sesion_id: int


class RegistroManualRequest(BaseModel):
    sesion_id: int
    producto_id: int
    cantidad: float
    unidad: str


class RegistroVozRequest(BaseModel):
    sesion_id: int
    texto: str


@app.get("/api/sesiones")
async def list_sesiones():
    """Lista todas las sesiones de conteo, ordenadas por fecha (para /sesiones)."""
    rows = await fetch(
        """
        SELECT
            s.id, s.bodega_id, b.nombre as bodega_nombre,
            s.estado, s.iniciada_por, s.creado_en, s.finalizado_en,
            COUNT(r.id)::int as total_productos,
            SUM(CASE WHEN r.decision_kalman IN ('ACEPTADA', 'CONFIRMADA_MANUAL') THEN 1 ELSE 0 END)::int as contados,
            SUM(CASE WHEN r.decision_kalman = 'SOSPECHOSA' THEN 1 ELSE 0 END)::int as alertas
        FROM sesiones_conteo s
        LEFT JOIN bodegas b ON s.bodega_id = b.id
        LEFT JOIN registros_conteo r ON s.id = r.sesion_id
        GROUP BY s.id, b.nombre, s.estado, s.iniciada_por, s.creado_en, s.finalizado_en
        ORDER BY s.creado_en DESC
        """
    )
    return [
        {
            **dict(row),
            "creado_en": row["creado_en"].isoformat() if row.get("creado_en") else None,
            "finalizado_en": row["finalizado_en"].isoformat() if row.get("finalizado_en") else None,
        }
        for row in rows
    ]


@app.post("/api/sesion/iniciar")
async def iniciar_sesion(req: IniciarSesionRequest):
    row = await fetchrow(
        "INSERT INTO sesiones_conteo (bodega_id, iniciada_por) VALUES ($1, $2) RETURNING id, bodega_id, estado, creado_en",
        req.bodega_id, req.iniciada_por,
    )
    if not row:
        raise HTTPException(500, "No se pudo crear la sesion")
    total_productos = await fetchrow(
        "SELECT COUNT(*) AS total FROM productos_en_bodega WHERE bodega_id = $1",
        req.bodega_id,
    )
    return {
        "sesion_id": row["id"],
        "bodega_id": row["bodega_id"],
        "estado": row["estado"],
        "creado_en": row["creado_en"].isoformat() if row.get("creado_en") else None,
        "total_productos": total_productos["total"] if total_productos else 0,
    }


@app.post("/api/sesion/finalizar")
async def finalizar_sesion(req: FinalizarSesionRequest):
    sesion = await fetchrow(
        "SELECT id, bodega_id, estado FROM sesiones_conteo WHERE id = $1",
        req.sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    if sesion["estado"] != "activa":
        raise HTTPException(400, f"Sesion ya esta {sesion['estado']}")
    await fetch(
        "UPDATE sesiones_conteo SET estado = 'finalizada', finalizado_en = NOW() WHERE id = $1",
        req.sesion_id,
    )
    stats = await fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'ACEPTADA') AS aceptados,
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'SOSPECHOSA') AS alertas,
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'PENDIENTE') AS pendientes,
            COUNT(*) AS total_contados
        FROM registros_conteo rc
        WHERE rc.sesion_id = $1
        """,
        req.sesion_id,
    )
    total_prods = await fetchrow(
        "SELECT COUNT(*) AS total FROM productos_en_bodega WHERE bodega_id = $1",
        sesion["bodega_id"],
    )
    return {
        "sesion_id": req.sesion_id,
        "estado": "finalizada",
        "total_productos": total_prods["total"] if total_prods else 0,
        "contados": stats["total_contados"] if stats else 0,
        "aceptados": stats["aceptados"] if stats else 0,
        "alertas": stats["alertas"] if stats else 0,
        "pendientes_kalman": stats["pendientes"] if stats else 0,
    }


@app.get("/api/sesion/{sesion_id}/estado")
async def estado_sesion(sesion_id: int):
    sesion = await fetchrow(
        "SELECT id, bodega_id, estado, iniciada_por, creado_en, finalizado_en FROM sesiones_conteo WHERE id = $1",
        sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    stats = await fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'ACEPTADA') AS aceptados,
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'SOSPECHOSA') AS alertas,
            COUNT(*) FILTER (WHERE rc.decision_kalman = 'PENDIENTE') AS pendientes,
            COUNT(*) AS total_contados
        FROM registros_conteo rc
        WHERE rc.sesion_id = $1
        """,
        sesion_id,
    )
    total_prods = await fetchrow(
        "SELECT COUNT(*) AS total FROM productos_en_bodega WHERE bodega_id = $1",
        sesion["bodega_id"],
    )
    return {
        "sesion_id": sesion["id"],
        "bodega_id": sesion["bodega_id"],
        "estado": sesion["estado"],
        "iniciada_por": sesion["iniciada_por"],
        "creado_en": sesion["creado_en"].isoformat() if sesion.get("creado_en") else None,
        "total_productos": total_prods["total"] if total_prods else 0,
        "contados": stats["total_contados"] if stats else 0,
        "aceptados": stats["aceptados"] if stats else 0,
        "alertas": stats["alertas"] if stats else 0,
        "pendientes": (total_prods["total"] if total_prods else 0) - (stats["total_contados"] if stats else 0),
    }


# =====================================================================
#  Registro de conteo
# =====================================================================

@app.post("/api/sesion/registrar-manual")
async def registrar_manual(req: RegistroManualRequest):
    sesion = await fetchrow(
        "SELECT id, bodega_id, estado FROM sesiones_conteo WHERE id = $1",
        req.sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    if sesion["estado"] != "activa":
        raise HTTPException(400, f"Sesion {sesion['estado']}")
    try:
        pending_id = await enqueue_registro_manual(
            session_id=str(req.sesion_id),
            producto_id=req.producto_id,
            cantidad=req.cantidad,
            unidad=req.unidad,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "success": True,
        "pending_id": pending_id,
        "message": "Registro encolado. El worker Kalman lo evaluara.",
    }


@app.post("/api/sesion/registrar-voz")
async def registrar_voz(req: RegistroVozRequest):
    sesion = await fetchrow(
        "SELECT id, bodega_id, estado FROM sesiones_conteo WHERE id = $1",
        req.sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    if sesion["estado"] != "activa":
        raise HTTPException(400, f"Sesion {sesion['estado']}")

    conteo = nlu.parse_conteo_rapido(req.texto)
    if conteo:
        from llm_common.fuzzy_search import fuzzy_match_product
        candidatos = await fetch(
            """SELECT id, nombre, unidad
               FROM productos_en_bodega
               WHERE bodega_id = $1""",
            sesion["bodega_id"],
        )
        match = fuzzy_match_product(conteo["producto"], candidatos)
        if match:
            cantidad_normalizada, unidad_final = nlu.normalize_unidad(
                conteo["cantidad"],
                conteo.get("unidad"),
                match["unidad"],
            )
            try:
                pending_id = await enqueue_registro_manual(
                    session_id=str(req.sesion_id),
                    producto_id=match["id"],
                    cantidad=cantidad_normalizada,
                    unidad=unidad_final,
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
            return {
                "success": True,
                "pending_id": pending_id,
                "via": "regex_fastpath",
                "producto": match["nombre"],
                "cantidad": cantidad_normalizada,
                "unidad": unidad_final,
                "message": "Registro encolado via fast path.",
            }

    #  Fallback: LLM (con cloud->local)
    async with httpx.AsyncClient(timeout=60) as client:
        if _pick_llm() == "openrouter":
            r, used, fallback, reason = await _post_with_fallback(
                client,
                cloud_url=f"{OPENROUTER_URL}/infer",
                local_url=f"{NEEDLE_URL}/infer",
                payload={
                    "query": req.texto,
                    "tools": "[]",
                    "session_id": str(req.sesion_id),
                    "mode": "full",
                    "bodega_id": sesion["bodega_id"],
                },
                want_cloud=True,
                label="LLM(voz)",
            )
        else:
            r, used, fallback, reason = await _post_with_fallback(
                client,
                cloud_url=None,
                local_url=f"{NEEDLE_URL}/infer",
                payload={
                    "query": req.texto,
                    "tools": "[]",
                    "session_id": str(req.sesion_id),
                    "mode": "full",
                    "bodega_id": sesion["bodega_id"],
                },
                want_cloud=False,
                label="LLM(voz)",
            )

    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    data = r.json()
    return {
        "success": True,
        "via": "llm",
        "backend": used,
        "fallback_used": fallback,
        "fallback_reason": reason,
        "tool_calls": data.get("tool_calls", []),
        "pending": data.get("pending", []),
        "raw_output": data.get("raw_output", ""),
    }


# =====================================================================
#  Catalogo / Reportes / Pending (sin cambios)
# =====================================================================

@app.get("/api/catalogo/bodega/{bodega_id}")
async def catalogo_bodega(
    bodega_id: int,
    q: str | None = None,
    solo_pendientes: bool = False,
    sesion_id: int | None = None,
):
    return await get_catalogo_bodega(bodega_id, q=q, solo_pendientes=solo_pendientes, sesion_id=sesion_id)


@app.get("/api/reporte/diferencias/{sesion_id}")
async def reporte_diferencias(sesion_id: int):
    sesion = await fetchrow(
        "SELECT id, bodega_id FROM sesiones_conteo WHERE id = $1",
        sesion_id,
    )
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    rows = await fetch(
        """
        SELECT
            p.nombre,
            peb.unidad,
            p.codigo_articulo,
            rc.stock_sistema,
            rc.cantidad_normalizada AS stock_contado,
            (rc.cantidad_normalizada - rc.stock_sistema) AS diferencia,
            rc.decision_kalman
        FROM registros_conteo rc
        JOIN productos p            ON p.id = rc.producto_id
        JOIN productos_en_bodega peb ON peb.id = p.id AND peb.bodega_id = rc.bodega_id
        WHERE rc.sesion_id = $1
        ORDER BY ABS(rc.cantidad_normalizada - rc.stock_sistema) DESC
        """,
        sesion_id,
    )
    pendientes = await fetch(
        """
        SELECT
            p.nombre,
            peb.unidad,
            p.codigo_articulo,
            peb.stock_actual AS stock_sistema,
            NULL::FLOAT AS stock_contado,
            NULL::FLOAT AS diferencia,
            'no_contado' AS decision_kalman
        FROM productos_en_bodega peb
        JOIN productos p ON p.id = peb.id
        WHERE peb.bodega_id = $1
        AND NOT EXISTS (
            SELECT 1 FROM registros_conteo rc
            WHERE rc.producto_id = peb.id
              AND rc.bodega_id   = peb.bodega_id
              AND rc.sesion_id   = $2
        )
        ORDER BY p.nombre
        """,
        sesion["bodega_id"],
        sesion_id,
    )
    return {
        "sesion_id": sesion_id,
        "contados": rows,
        "no_contados": pendientes,
        "total_contados": len(rows),
        "total_pendientes": len(pendientes),
    }


@app.get("/api/reporte/sospechosos/{sesion_id}")
async def reporte_sospechosos(sesion_id: int):
    rows = await fetch(
        """
        SELECT
            pe.id AS pending_id,
            p.nombre,
            peb.unidad,
            rc.cantidad_normalizada AS cantidad_contada,
            rc.stock_sistema,
            (rc.cantidad_normalizada - rc.stock_sistema) AS diferencia,
            pe.residual,
            pe.umbral,
            pe.decision,
            pe.created_at
        FROM registros_conteo rc
        JOIN productos p            ON p.id = rc.producto_id
        JOIN productos_en_bodega peb ON peb.id = p.id AND peb.bodega_id = rc.bodega_id
        JOIN pending_evaluations pe  ON pe.id = rc.pending_id
        WHERE rc.sesion_id = $1 AND rc.decision_kalman = 'SOSPECHOSA'
        ORDER BY ABS(pe.residual) DESC
        """,
        sesion_id,
    )
    return {"sesion_id": sesion_id, "sospechosos": rows, "total": len(rows)}


@app.get("/api/pending/{pending_id}")
async def pending_status(pending_id: int):
    row = await get_pending_status(pending_id)
    if not row:
        raise HTTPException(404, "pending not found")
    return row


# =====================================================================
#  Entrypoint
# =====================================================================

def main():
    import uvicorn
    port = int(os.getenv("PORT", "8200"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
