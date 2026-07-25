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
    "kilogramo": "Kilogram", "kilogramos": "Kilogram", "kilogram": "Kilogram",
    #  Sub-unidades se mapean a si mismas: normalize_unidad aplica el
    #  factor de _CONVERSION (g->Kilogram = 0.001). Mapearlas directo a la
    #  canonica dejaba la conversion muerta (500 gramos = 500 kg!).
    "g": "g", "gr": "g", "gramo": "g", "gramos": "g",
    "l": "Liter", "lt": "Liter", "lts": "Liter",
    "litro": "Liter", "litros": "Liter", "liter": "Liter",
    "ml": "ml", "mililitro": "ml", "mililitros": "ml",
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


# ---------------------------------------------------------------------------
#  Fast path: escrituras con direccion (agregar vs remover)
# ---------------------------------------------------------------------------

_VERBOS_AGREGAR = (
    r"(?:agreg(?:ar?|a|ue)|añad(?:ir?|e)|ingresa[r]?|met(?:er?|a|eme)"
    r"|pon(?:er?|e|ga?)|carga[r]?|sube[r]?)"
)
_VERBOS_REMOVER = (
    r"(?:sac(?:ar?|a)|remov(?:er?|e)|retira[r]?|quita[r]?|vend(?:er?|e)"
    r"|descontar|descuenta|resta[r]?|baja[r]?)"
)
_NUM = r"(?P<cantidad>\d+(?:[\.,]\d+)?)"
_UNI = (
    r"(?:(?P<unidad>kilos?|kilogramos?|kg|gramos?|gr?|litros?|lts?|ml"
    r"|unidades?|un\.?|unds?|piezas?|pzs?|cajas?|paquetes?|sobres?|frascos?|rollos?)\s+)?"
)
_TAIL = _NUM + r"\s*" + _UNI + r"(?:de\s+)?(?P<producto>.+?)\s*[.,;!?]?$"

_ESCRITURA_AGREGAR_RE = re.compile(_VERBOS_AGREGAR + r"\s+" + _TAIL, re.IGNORECASE)
_ESCRITURA_REMOVER_RE = re.compile(_VERBOS_REMOVER + r"\s+" + _TAIL, re.IGNORECASE)


def parse_escritura_rapida(texto: str) -> dict | None:
    """Fast path determinista para escrituras: <verbo> <num> [unidad] [de] <producto>.

    A diferencia de parse_conteo_rapido, distingue direccion.
    Retorna {"tool": "agregar_inventario"|"remover_inventario",
             "cantidad": float, "unidad": str|None, "producto": str}
    o None si no matchea (a si lo intenta el modelo).
    """
    t = texto.strip()
    for pat, tool in ((_ESCRITURA_AGREGAR_RE, "agregar_inventario"),
                      (_ESCRITURA_REMOVER_RE, "remover_inventario")):
        m = pat.search(t)
        if m:
            return {
                "tool": tool,
                "cantidad": float(m.group("cantidad").replace(",", ".")),
                "unidad": _normalizar_unidad_raw((m.group("unidad") or "").lower()) or None,
                "producto": m.group("producto").strip(),
            }
    return None


# ---------------------------------------------------------------------------
#  Fast path: lecturas (consultas de inventario, sin escritura)
# ---------------------------------------------------------------------------

_READ_PATTERNS: list[tuple[re.Pattern, bool]] = [
    # "cuanto hay de aceite", "cuantas cebollas tenemos"
    (re.compile(r"cu[áa]nt[oa]s?\s+(?:hay|tenemos|quedan)(?:\s+(?:de|en)\s+)?(?P<producto>.+?)\s*$", re.IGNORECASE), True),
    # "cuanto aceite hay"
    (re.compile(r"cu[áa]nt[oa]s?\s+(?P<producto>.+?)\s+(?:hay|tenemos|quedan)\s*$", re.IGNORECASE), True),
    # "consulta aceite", "consultar inventario de pan"
    (re.compile(r"consulta[r]?\s+(?:stock|inventario|producto\s+)?(?:de\s+)?(?P<producto>.+?)\s*$", re.IGNORECASE), True),
    # "ver stock de aceite", "ver inventario"
    (re.compile(r"ver\s+(?:stock|inventario)(?:\s+(?:de\s+)?(?P<producto>.+?))?\s*$", re.IGNORECASE), True),
    # "que hay", "que tenemos"
    (re.compile(r"qu[ée]\s+(?:hay|tenemos|queda)(?:\s+en\s+(?:el\s+)?inventario)?\s*$", re.IGNORECASE), False),
    (re.compile(r"qu[ée]\s+(?:hay|tenemos)\s+(?:de\s+)?(?P<producto>.+?)\s*$", re.IGNORECASE), True),
    # "mostrame inventario", "dame stock de X"
    (re.compile(r"(?:mostr(?:ar?|a)|dame)\s+(?:el\s+)?(?:inventario|stock)(?:\s+(?:de\s+)?(?P<producto>.+?))?\s*$", re.IGNORECASE), True),
    # "inventario" solo
    (re.compile(r"^(?:inventario|stock|cat[aá]logo)\s*$", re.IGNORECASE), False),
]

# ---------------------------------------------------------------------------
#  Fast path: investigacion / auditoria
# ---------------------------------------------------------------------------

_INVESTIGATE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:hay\s+)?algo\s+raro\b", re.IGNORECASE),
    re.compile(r"\binvestig(?:ar?|a)\b", re.IGNORECASE),
    re.compile(r"\baudit(?:ar?|a)\b", re.IGNORECASE),
    re.compile(r"\bsospechos[oa]s?\b", re.IGNORECASE),
    re.compile(r"\bdiscrepancias?\b", re.IGNORECASE),
    re.compile(r"\banomal[íi]as?\b", re.IGNORECASE),
    re.compile(r"(?:revisa[r]?|mira[r]?|checa[r]?)\s+(?:si\s+hay\s+)?(?:sospechosos?|errores?|anomal[íi]as?)", re.IGNORECASE),
    re.compile(r"\berrores?\s+(?:de|del)\s+inventario\b", re.IGNORECASE),
]


def parse_lectura_rapida(texto: str) -> dict | None:
    """Fast path determinista para lecturas: consultas de stock/inventario.

    Retorna {"tool": "consultar_inventario", "producto": str|None}
    o None si no matchea.
    """
    t = texto.strip()
    for pat, has_prod in _READ_PATTERNS:
        m = pat.search(t)
        if m:
            prod = None
            if has_prod:
                try:
                    prod = m.group("producto").strip().rstrip(".,;!?")
                except IndexError:
                    prod = None
            return {"tool": "consultar_inventario", "producto": prod or None}
    return None


def parse_investigacion_rapida(texto: str) -> dict | None:
    """Fast path determinista para investigacion/auditoria.

    Retorna {"tool": "investigar_sospechosos", "producto": None}
    o None si no matchea.
    """
    t = texto.strip()
    for pat in _INVESTIGATE_PATTERNS:
        if pat.search(t):
            return {"tool": "investigar_sospechosos", "producto": None}
    return None
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

def _match_producto_en_texto(texto: str, producto_nombres: set[str]) -> str | None:
    """Primer nombre del catalogo que aparece en el texto, con limites de
    palabra y plural opcional. Sin \b, "ajo" matcheaba dentro de "trabajo".
    Se prueba primero el nombre mas largo (gana "aceite de ajonjoli" a
    "ajonjoli").
    """
    t = texto.lower()
    for nombre in sorted(producto_nombres, key=len, reverse=True):
        if re.search(r"\b" + re.escape(nombre) + r"s?\b", t):
            return nombre
    return None


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

    return _match_producto_en_texto(v, producto_nombres) or ""


def extract_producto(query: str, producto_nombres: set[str]) -> str | None:
    """Busca el primer nombre de producto que aparece en el query."""
    return _match_producto_en_texto(query.lower(), producto_nombres)


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