"""NLU determinista compartida por needle-service y openrouter-service.

Regla de oro del proyecto: lo que se puede resolver sin modelo (un si/no,
plurales, numeros, unidades) se resuelve sin modelo. El LLM solo elige
intenciones.

El catalogo de productos es dinamico (viene de DB), no hardcodeado.
Las funciones aceptan un conjunto de nombres de producto como parametro.
"""
from __future__ import annotations

import re
from typing import Any

_CONFIRMA_RE = re.compile(
    r"\b(si|sí|dale|confirma\w*|ok|okay|yes|acepta\w*|va|hazlo|metele|adelante)\b",
    re.IGNORECASE,
)
_RECHAZA_RE = re.compile(
    r"\b(no\b(?!\s+(s[ée]|estoy|creo)\b)|nope|negativo|rechaza\w*|cancela\w*|descarta\w*|anula\w*)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
#  Unidades — deteccion y normalizacion
# ---------------------------------------------------------------------------

_UNIDAD_MAP: dict[str, str] = {
    "kg": "Kilogram", "kilo": "Kilogram", "kilos": "Kilogram",
    "kilogramo": "Kilogram", "kilogramos": "Kilogram",
    "g": "Kilogram", "gr": "Kilogram", "gramo": "Kilogram", "gramos": "Kilogram",
    "l": "Liter", "lt": "Liter", "lts": "Liter",
    "litro": "Liter", "litros": "Liter",
    "ml": "Liter", "mililitro": "Liter", "mililitros": "Liter",
    "un": "Unidad", "und": "Unidad", "unds": "Unidad",
    "unidad": "Unidad", "unidades": "Unidad",
    "pza": "Unidad", "pzas": "Unidad", "pieza": "Unidad", "piezas": "Unidad",
    "caja": "Unidad", "cajas": "Unidad",
    "paquete": "Unidad", "paquetes": "Unidad",
    "sobre": "Unidad", "sobres": "Unidad",
    "frasco": "Unidad", "frascos": "Unidad",
    "rollo": "Unidad", "rollos": "Unidad",
}

_UNIDAD_REGEX = re.compile(
    r'\b(kilos?|kilogramos?|kg|gramos?|gr|litros?|lts?|L|mililitros?|ml'
    r'|unidades?|un\.?|unds?|piezas?|pzs?'
    r'|cajas?|paquetes?|sobres?|frascos?|rollos?)\b',
    re.IGNORECASE,
)

# Conversion: (from_unit_normalizada, to_unit_catalogo) -> factor
_CONVERSION: dict[tuple[str, str], float] = {
    ("Kilogram", "Kilogram"): 1.0,
    ("Liter", "Liter"): 1.0,
    ("Unidad", "Unidad"): 1.0,
    # Sub-unidad -> unidad canonica
    ("g", "Kilogram"): 0.001,
    ("ml", "Liter"): 0.001,
    # Canonica -> sub-unidad
    ("Kilogram", "g"): 1000.0,
    ("Liter", "ml"): 1000.0,
}

# ---------------------------------------------------------------------------
#  Fast path: regex para comandos de conteo (<numero> <unidad> de <producto>)
# ---------------------------------------------------------------------------

_CONTEO_RE = re.compile(
    r'(?:agreg(?:ar?|a|ue)|añad(?:ir?|e)|ingresa[r]?|met(?:er?|a|eme)|pon(?:er?|e|ga?)|'
    r'carga[r]?|sac(?:ar?|a)|remover|retirar|quita[r]?)\s+'
    r'(?P<cantidad>\d+(?:[\.,]\d+)?)\s*'
    r'(?:(?P<unidad>kilos?|kilogramos?|kg|gramos?|gr?|litros?|lts?|ml|'
    r'unidades?|un\.?|unds?|piezas?|pzs?|cajas?|paquetes?|sobres?|frascos?|rollos?)\s+)?'
    r'(?:de\s+)?(?P<producto>.+?)$',
    re.IGNORECASE,
)

_CONTEO_SIMPLE_RE = re.compile(
    r'^(?P<cantidad>\d+(?:[\.,]\d+)?)\s*'
    r'(?:(?P<unidad>kilos?|kilogramos?|kg|gramos?|gr?|litros?|lts?|ml|'
    r'unidades?|un\.?|unds?|piezas?|pzs?|cajas?|paquetes?|sobres?|frascos?|rollos?)\s+)?'
    r'(?:de\s+)?(?P<producto>.+?)$',
    re.IGNORECASE,
)


def parse_conteo_rapido(texto: str) -> dict | None:
    """Fast path regex para comandos de conteo. <1ms, sin LLM.

    Retorna {"cantidad": float, "unidad": str|None, "producto": str}
    o None si el texto no matchea el patron de conteo.
    """
    for pat in (_CONTEO_RE, _CONTEO_SIMPLE_RE):
        m = pat.search(texto.strip())
        if m:
            cantidad_str = m.group("cantidad").replace(",", ".")
            return {
                "cantidad": float(cantidad_str),
                "unidad": _normalizar_unidad_raw((m.group("unidad") or "").lower()) or None,
                "producto": m.group("producto").strip(),
            }
    return None


def extract_unidad(texto: str) -> str | None:
    """Extrae la primera mencion de unidad en el texto."""
    m = _UNIDAD_REGEX.search(texto)
    if m:
        return _normalizar_unidad_raw(m.group(1).lower())
    return None


def _normalizar_unidad_raw(raw: str) -> str | None:
    """'kilos' -> 'Kilogram', 'litros' -> 'Liter', etc. None si no se reconoce."""
    if not raw:
        return None
    return _UNIDAD_MAP.get(raw.lower())


def normalize_unidad(
    cantidad: float, unidad_usuario: str | None, unidad_catalogo: str
) -> tuple[float, str]:
    """Convierte la cantidad a la unidad del catalogo.

    Ej: (500, 'g', 'Kilogram') -> (0.5, 'Kilogram')
        (5, 'kilos', 'Kilogram') -> (5.0, 'Kilogram')
        (3, None, 'Unidad') -> (3.0, 'Unidad')
    """
    if unidad_usuario is None:
        return cantidad, unidad_catalogo

    uu = _normalizar_unidad_raw(unidad_usuario)
    if uu is None:
        return cantidad, unidad_catalogo

    if uu == unidad_catalogo:
        return cantidad, unidad_catalogo

    factor = _CONVERSION.get((uu, unidad_catalogo))
    if factor is not None:
        return cantidad * factor, unidad_catalogo

    return cantidad, unidad_catalogo


# ---------------------------------------------------------------------------
#  Confirmacion / rechazo
# ---------------------------------------------------------------------------

def parse_confirmacion(text: str) -> bool | None:
    """True = confirma, False = rechaza, None = no hay intencion clara.

    Gana la palabra que aparece primero. Muletillas como "no se",
    "no estoy seguro" o "no creo" NO cuentan como rechazo.
    """
    matches = [(m.start(), True) for m in _CONFIRMA_RE.finditer(text)]
    matches += [(m.start(), False) for m in _RECHAZA_RE.finditer(text)]
    if not matches:
        return None
    matches.sort()
    return matches[0][1]


# ---------------------------------------------------------------------------
#  Normalizacion de producto (dinamica, desde catalogo)
# ---------------------------------------------------------------------------

def normalize_producto(val: str, producto_nombres: set[str]) -> str:
    """Busca si `val` contiene algun nombre de producto conocido.

    producto_nombres: conjunto de nombres en lowercase del catalogo.
    Retorna el nombre canonico o "" si no hay match.
    """
    v = (val or "").strip().lower()
    if not v:
        return ""

    if v in producto_nombres:
        return v

    for nombre in sorted(producto_nombres, key=len, reverse=True):
        if nombre in v:
            return nombre

    return ""


def extract_producto(query: str, producto_nombres: set[str]) -> str | None:
    """Busca el primer nombre de producto que aparece en el query."""
    q = query.lower()
    for nombre in sorted(producto_nombres, key=len, reverse=True):
        if nombre in q:
            return nombre
    return None


# ---------------------------------------------------------------------------
#  Saneamiento de argumentos del modelo
# ---------------------------------------------------------------------------

def normalize_args(name: str, args: dict, query: str, producto_nombres: set[str] | None = None) -> dict:
    """Sanea los argumentos que escupe el modelo contra la query original."""
    if producto_nombres is None:
        producto_nombres = set()

    if name in ("agregar_inventario", "remover_inventario"):
        prod = normalize_producto(str(args.get("producto", "")), producto_nombres)
        cant = args.get("cantidad")
        if isinstance(cant, str):
            m = re.search(r"(\d+(?:[\.,]\d+)?)", cant.replace(",", "."))
            cant = float(m.group(1)) if m else 0.0
        elif isinstance(cant, (int, float)) and not isinstance(cant, bool):
            cant = float(cant)
        else:
            cant = 0.0

        if not prod:
            prod = extract_producto(query, producto_nombres) or ""
        if not cant:
            nums = re.findall(r"(\d+(?:[\.,]\d+)?)", query)
            cant = float(nums[-1].replace(",", ".")) if nums else 0.0

        unidad = args.get("unidad")
        if not unidad:
            unidad = extract_unidad(query)

        return {
            "producto": prod,
            "cantidad": cant,
            "unidad": unidad or "",
        }

    if name in ("consultar_inventario", "investigar_sospechosos"):
        prod = normalize_producto(str(args.get("producto", "") or ""), producto_nombres) or None
        if prod is None:
            prod = extract_producto(query, producto_nombres)
        return {"producto": prod}

    if name == "confirmar_movimiento":
        pid = args.get("pending_id", args.get("movimiento_id", 0))
        if isinstance(pid, str):
            m = re.search(r"(\d+)", pid)
            pid = int(m.group(1)) if m else 0
        conf = args.get("confirmar", True)
        if isinstance(conf, str):
            conf = conf.lower() in ("true", "si", "sí", "yes", "1")
        return {"pending_id": int(pid or 0), "confirmar": bool(conf)}

    return dict(args)


def build_alert_context(query: str, alert: dict) -> str:
    """Contexto textual de la alerta para cuando si hace falta el modelo."""
    pid = alert.get("pending_id") or alert.get("movimiento_id") or 0
    return (
        f"Hay una alerta de inventario pendiente (pending_id={pid}, "
        f"producto {alert.get('producto')}, {alert.get('cantidad')} unidades, "
        f"tipo {alert.get('tipo')}). El usuario dice: {query}"
    )


def get_producto_nombres_from_candidates(candidates: list[dict]) -> set[str]:
    """Extrae un set de nombres lowercase de una lista de candidatos."""
    return {c["nombre"].lower() for c in candidates}


def get_unidad_from_candidates(candidates: list[dict], nombre: str) -> str:
    """Devuelve la unidad del catalogo para un nombre de producto dado."""
    nl = nombre.lower()
    for c in candidates:
        if c["nombre"].lower() == nl:
            return c.get("unidad", "Unidad")
    return "Unidad"