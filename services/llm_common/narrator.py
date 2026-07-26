"""narrator: convierte eventos estructurados del inventario en frases
naturales en espanol (rioplatense), para que el TTS las pronuncie.

Dos modos:
  - "default": usa templates hardcodeados, variaciones para no sonar repetitivo.
    Cero latencia, cero costo, cero dependencia externa.
  - "llm":    usa OpenRouter (default google/gemma-4-31b-it:free) para reformular
    el template con mas onda conversacional. Mas lento, depende de internet.

API:
  from llm_common.narrator import Narrator, NarrateEvent

  narrator = Narrator(backend="default")
  text = await narrator.narrate(NarrateEvent.ACEPTADA, {
      "producto": "papa", "cantidad": 5, "unidad": "kg",
      "stock_actual": 130, "bodega": "Bodega 1",
  })
  # -> "Listo, sumamos 5 kilos de papa. Te quedan 130 kilos en Bodega 1."
"""
from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger("narrator")


class NarrateEvent(str, Enum):
    """Tipos de evento que el narrador puede reformular."""
    ACEPTADA = "aceptada"            # Movimiento aceptado por Kalman
    SOSPECHOSA = "sospechosa"        # Movimiento marcado como sospechoso
    CONFIRMADA = "confirmada"        # Sospechoso confirmado por el usuario
    RECHAZADA = "rechazada"          # Sospechoso rechazado por el usuario
    CONSULTA = "consulta"            # Resultado de consultar_inventario
    SOSPECHOSOS = "sospechosos"      # Resultado de investigar_sospechosos
    INVALID = "invalid"              # Tool call que no se encolo
    NO_ACTION = "no_action"          # LLM no decidio nada util
    REGISTRAR_MANUAL = "registrar_manual"  # Registro manual de tablet


# ---------------------------------------------------------------------------
#  Templates por evento (variaciones para no sonar repetitivo)
# ---------------------------------------------------------------------------

_OPENINGS_ACK = [
    "Listo,",
    "Hecho,",
    "Ok,",
    "Dale,",
    "Anotado,",
    "Buenisimo,",
]

_OPENINGS_ALERT = [
    "Atencion,",
    "Ojo,",
    "Pará,",
    "Uh,",
    "Mmm, este movimiento se ve raro:",
    "Esto me hace ruido:",
]

_OPENINGS_CONFIRM = [
    "Listo, confirmado.",
    "Anotado, confirmado.",
    "Hecho, confirmado.",
    "Ok, dale.",
]

_OPENINGS_REJECT = [
    "Ok, lo descarte.",
    "Anotado, no se toco nada.",
    "Listo, no se modifico el stock.",
    "Vale, lo dejamos como estaba.",
]

_OPENINGS_CONSULTA = [
    "Mirá,",
    "Fijate,",
    "Te cuento,",
    "Encontre esto:",
]

_OPENINGS_NOACTION = [
    "Mmm, no te entendi.",
    "Uh, no me cerro eso.",
    "No cacho que quisiste decir.",
    "Pasame el pedido de otra forma.",
]


def _cantidad_texto(cantidad: float, unidad: str) -> str:
    """'5.0 Kilogram' -> '5 kilos', '0.25 Kilogram' -> '250 gramos', '0.5 Liter' -> 'medio litro'.

    Reglas:
      - Peso: si < 1 kilo, lo nombra en gramos (mas natural que '0.25 kilos').
              Si >= 1 kilo, en kilos.
      - Volumen: igual, < 1 litro -> mililitros.
      - Unidades: si == 1 'una unidad', sino N 'unidades'.
      - Unidad desconocida: la cantidad con la unidad tal cual.
    """
    if unidad in ("Kilogram", "kg", "kilo", "kilos"):
        if cantidad < 1:
            #  Pasamos a gramos: 0.25 kg = 250 g. Mas natural.
            gramos = round(cantidad * 1000)
            if gramos == 1:
                return "un gramo"
            if gramos == 500:
                return "medio kilo"  # 500 g = "medio kilo" es mas natural
            if gramos == 250:
                return "cuarto kilo"
            return f"{gramos} gramos"
        if cantidad == 1:
            return "un kilo"
        if cantidad == 0.5:
            return "medio kilo"
        if cantidad.is_integer():
            return f"{int(cantidad)} kilos"
        return f"{cantidad:g} kilos"
    if unidad in ("Liter", "litro", "litros", "lts"):
        if cantidad < 1:
            ml = round(cantidad * 1000)
            if ml == 1:
                return "un mililitro"
            if ml == 500:
                return "medio litro"
            return f"{ml} mililitros"
        if cantidad == 1:
            return "un litro"
        if cantidad == 0.5:
            return "medio litro"
        if cantidad.is_integer():
            return f"{int(cantidad)} litros"
        return f"{cantidad:g} litros"
    if unidad in ("Unidad", "unidad", "unidades"):
        if cantidad == 1:
            return "una unidad"
        if cantidad.is_integer():
            return f"{int(cantidad)} unidades"
        return f"{cantidad:g} unidades"
    #  Sub-unidades que llegan crudas (caso raro: el caller paso "g" sin
    #  normalizar). Las nombramos en plural standard.
    if unidad in ("g", "gr", "gramo", "gramos"):
        if cantidad == 1:
            return "un gramo"
        if cantidad.is_integer():
            return f"{int(cantidad)} gramos"
        return f"{cantidad:g} gramos"
    if unidad in ("ml", "mililitro", "mililitros"):
        if cantidad == 1:
            return "un mililitro"
        if cantidad.is_integer():
            return f"{int(cantidad)} mililitros"
        return f"{cantidad:g} mililitros"
    #  Unidad desconocida: cantidad + unidad tal cual
    if cantidad.is_integer():
        return f"{int(cantidad)} {unidad}"
    return f"{cantidad:g} {unidad}"


def _stock_texto(stock: float | None, unidad: str) -> str:
    if stock is None:
        return ""
    #  float(): stock_actual puede llegar como int desde JSON (el caller no
    #  siempre lo castea), y .is_integer() no existe en int para py<3.12.
    s = _cantidad_texto(float(stock), unidad)
    return f"te quedan {s}"


def _nivel_riesgo(puntaje: float) -> str:
    """Traduce un puntaje de sigmas a una frase natural."""
    if puntaje >= 30:
        return "es gravísimo"
    if puntaje >= 10:
        return "es altisimo"
    if puntaje >= 3:
        return "se va de mambo"
    if puntaje >= 1.5:
        return "se ve raro"
    return "es un poquito fuera de lo normal"


def _build_default(event: NarrateEvent, data: dict[str, Any]) -> str:
    """Genera una frase natural a partir de data estructurada.
    Elige variaciones al azar para no sonar repetitivo.
    """
    producto = (data.get("producto") or "el producto").lower()
    cantidad = data.get("cantidad") or 0
    unidad = data.get("unidad") or ""
    stock = data.get("stock_actual")
    bodega = data.get("bodega") or "la bodega"
    #  Sin default a "entrada": un registrar_conteo ("hay N") no tiene
    #  tipo (no es una entrada ni una salida, es una medicion absoluta).
    #  Si viene None/vacio, usamos una frase neutra ("contamos") en vez de
    #  asumir que fue una entrada — asumir eso fue el bug original que
    #  arranco todo este cambio (decia "sumamos 47" para un conteo de "hay 3").
    tipo = data.get("tipo") or None
    puntaje = float(data.get("puntaje_riesgo") or 0)

    cant_txt = _cantidad_texto(float(cantidad), unidad)
    stock_txt = _stock_texto(stock if stock is not None else None, unidad)

    if event == NarrateEvent.ACEPTADA:
        op = random.choice(_OPENINGS_ACK)
        if tipo == "salida":
            return f"{op} sacamos {cant_txt} de {producto}. {stock_txt.capitalize() if stock_txt else 'Listo'} en {bodega}."
        if tipo == "entrada":
            return f"{op} sumamos {cant_txt} de {producto}. {stock_txt.capitalize() if stock_txt else 'Listo'} en {bodega}."
        #  tipo=None -> registrar_conteo (conteo absoluto, no delta)
        return f"{op} contamos {cant_txt} de {producto}. {stock_txt.capitalize() if stock_txt else 'Listo'} en {bodega}."

    if event == NarrateEvent.REGISTRAR_MANUAL:
        op = random.choice(_OPENINGS_ACK)
        return f"{op} anotado: {cant_txt} de {producto}. {stock_txt.capitalize() if stock_txt else 'Listo'} en {bodega}."

    if event == NarrateEvent.SOSPECHOSA:
        op = random.choice(_OPENINGS_ALERT)
        if tipo == "entrada":
            accion = "ingreso"
        elif tipo == "salida":
            accion = "salida"
        else:
            #  tipo=None -> registrar_conteo: no es un movimiento, es un
            #  conteo que no coincide con lo que el sistema esperaba.
            accion = "conteo"
        riesgo_txt = _nivel_riesgo(puntaje)
        return (
            f"{op} {accion} de {cant_txt} de {producto} {riesgo_txt}, "
            f"{puntaje:.1f} sigmas. Te lo confirmo o lo descarto?"
        )

    if event == NarrateEvent.CONFIRMADA:
        return random.choice(_OPENINGS_CONFIRM)

    if event == NarrateEvent.RECHAZADA:
        return random.choice(_OPENINGS_REJECT)

    if event == NarrateEvent.CONSULTA:
        op = random.choice(_OPENINGS_CONSULTA)
        if stock is None:
            return f"{op} no encontre {producto} en {bodega}."
        return f"{op} en {bodega} hay {_cantidad_texto(float(stock), unidad)} de {producto}."

    if event == NarrateEvent.SOSPECHOSOS:
        total = data.get("total", 0)
        top_producto = (data.get("top_producto") or "").lower()
        top_cantidad = data.get("top_cantidad") or 0
        top_puntaje = float(data.get("top_puntaje") or 0)
        top_tipo = data.get("top_tipo") or "entrada"
        if total == 0:
            return "No hay movimientos raros en la auditoria, todo tranqui."
        top_unidad = data.get("top_unidad") or unidad or "Unidad"
        op = random.choice(_OPENINGS_CONSULTA)
        accion = "ingreso" if top_tipo == "entrada" else "salida"
        return (
            f"{op} hay {total} movimientos sospechosos. "
            f"El mas grave: {accion} de {_cantidad_texto(float(top_cantidad), top_unidad)} de {top_producto}, "
            f"{top_puntaje:.1f} sigmas."
        )

    if event == NarrateEvent.INVALID:
        tool = data.get("tool_name") or ""
        args = data.get("args") or {}
        prod = (args.get("producto") or "").lower()
        cant = args.get("cantidad")
        if tool == "confirmar_movimiento":
            return "No entendi la confirmacion. Decime si o no."
        if tool == "registrar_conteo":
            if not prod:
                return (
                    "No se de que producto me estas contando. "
                    "Probá con algo como 'hay cinco kilos de papa'."
                )
            if cant is None or float(cant) < 0:
                return f"No entendi cuanto contaste de {prod}."
            return f"No pude registrar el conteo de {prod}."
        if not prod:
            accion_txt = "agregar" if tool == "agregar_inventario" else "sacar"
            return (
                f"No se que producto queres {accion_txt}. "
                f"Probá con algo como '{accion_txt} cinco kilos de papa' o 'cuanto hay de tomate'."
            )
        if cant is None or float(cant) <= 0:
            accion = "agregar" if tool == "agregar_inventario" else "sacar"
            return f"La cantidad no me cerro. Cuanto queres {accion} de {prod}?"
        return f"No pude procesar la operacion sobre {prod}."

    if event == NarrateEvent.NO_ACTION:
        op = random.choice(_OPENINGS_NOACTION)
        return f"{op} Probá con 'agregar cinco kilos de papa', 'hay tres kilos de papa', 'cuanto hay de tomate' o 'hay algo raro'."

    return f"{producto} {cant_txt} en {bodega}."


# ---------------------------------------------------------------------------
#  Reescritura con LLM via OpenRouter
# ---------------------------------------------------------------------------

_NARRATOR_PROMPT = """Sos un asistente de inventario en un almacen. Hablas en espanol rioplatense, corto y natural, como un companiero de trabajo.

Reformula el siguiente mensaje de forma conversacional. Reglas:
- Mismos datos exactos (producto, cantidad, unidad, stock, bodega, riesgo). NO inventes numeros.
- Maximo 1-2 oraciones, idealmente 1.
- Sin emojis, sin markdown, sin comillas.
- Si el mensaje original es una alerta, sonar preocupado pero calmo. Si es confirmacion, sonar satisfecho.
- NO uses "Atencion" o "Se detecto" ni lenguaje formal. Habla como persona.

Mensaje original: "{original}"

Reformula:"""


@dataclass
class NarratorConfig:
    backend: str = "default"   # "default" | "llm"
    model: str = "google/gemma-4-31b-it:free"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = ""


class Narrator:
    """Wrapper async con cache opcional para no martillar la API."""

    def __init__(self, config: NarratorConfig | None = None):
        self.config = config or NarratorConfig()
        #  Cache de event+key -> texto reescrito. Key = hash del texto default
        #  + data importante. Asi si el mismo evento se narra 2 veces seguidas
        #  no gastamos tokens.
        self._cache: dict[str, str] = {}
        self._cache_max = 256

    def _cache_key(self, event: NarrateEvent, data: dict[str, Any]) -> str:
        #  Solo los campos que afectan el texto
        relevant = {k: data.get(k) for k in (
            "producto", "cantidad", "unidad", "stock_actual",
            "bodega", "tipo", "puntaje_riesgo", "tool_name", "args",
            "total", "top_producto", "top_cantidad", "top_puntaje", "top_tipo",
        )}
        return f"{event.value}|{repr(sorted(relevant.items()))}"

    async def narrate(self, event: NarrateEvent, data: dict[str, Any]) -> str:
        """Devuelve la frase natural. Si backend=llm y falla, cae a default."""
        original = _build_default(event, data)
        if self.config.backend != "llm":
            return original

        key = self._cache_key(event, data)
        if key in self._cache:
            return self._cache[key]

        rewritten = await self._rewrite_with_llm(original)
        if rewritten is None:
            #  Fallback al default (sin cache, asi reintenta proxima vez)
            return original

        #  Cache (LRU simple: si se llena, limpiamos la mitad)
        if len(self._cache) >= self._cache_max:
            #  Corta la mitad menos usada (FIFO)
            for k in list(self._cache.keys())[: self._cache_max // 2]:
                self._cache.pop(k, None)
        self._cache[key] = rewritten
        return rewritten

    async def _rewrite_with_llm(self, original: str) -> str | None:
        if not self.config.api_key:
            logger.warning("narrator LLM sin API key, fallback a default")
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.config.model,
                        "messages": [
                            {"role": "system", "content": _NARRATOR_PROMPT.format(original=original)},
                            {"role": "user", "content": "Reformula."},
                        ],
                        "temperature": 0.7,
                        "max_tokens": 100,
                    },
                )
            if r.status_code != 200:
                logger.warning("narrator LLM http %d: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
            #  Limpieza: quitar comillas, markdown
            text = text.strip('"\'`').strip()
            if not text:
                return None
            return text
        except Exception as e:
            logger.warning("narrator LLM error: %s", e)
            return None
