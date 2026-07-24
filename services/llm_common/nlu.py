"""NLU determinista compartida por needle-service y openrouter-service.

Regla de oro del proyecto: lo que se puede resolver sin modelo (un si/no,
plurales, numeros) se resuelve sin modelo. El LLM solo elige intenciones.
"""
from __future__ import annotations

import re

PRODUCTOS = ("papa", "cebolla", "tomate", "zanahoria", "ajo")
_PLURAL = {p + "s": p for p in PRODUCTOS}  # papas -> papa, etc.

_CONFIRMA_RE = re.compile(
    r"\b(si|sí|dale|confirma\w*|ok|okay|yes|acepta\w*|va|hazlo|metele|adelante)\b",
    re.IGNORECASE,
)
_RECHAZA_RE = re.compile(
    r"\b(no\b(?!\s+(s[ée]|estoy|creo)\b)|nope|negativo|rechaza\w*|cancela\w*|descarta\w*|anula\w*)",
    re.IGNORECASE,
)


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


def normalize_producto(val: str) -> str:
    """'Zanahorias' -> 'zanahoria'. '' si no matchea ningun producto."""
    v = (val or "").strip().lower()
    if v in PRODUCTOS:
        return v
    if v in _PLURAL:
        return _PLURAL[v]
    for p in PRODUCTOS:
        if p in v:
            return p
    for pl, sg in _PLURAL.items():
        if pl in v:
            return sg
    return ""


def extract_producto(query: str) -> str | None:
    q = query.lower()
    for p in PRODUCTOS:
        if p in q:
            return p
    for pl, sg in _PLURAL.items():
        if pl in q:
            return sg
    return None


def normalize_args(name: str, args: dict, query: str) -> dict:
    """Sanea los argumentos que escupe el modelo contra la query original."""
    if name in ("agregar_inventario", "remover_inventario"):
        prod = normalize_producto(str(args.get("producto", "")))
        cant = args.get("cantidad")
        if isinstance(cant, str):
            m = re.search(r"(\d+)", cant)
            cant = int(m.group(1)) if m else 0
        elif not isinstance(cant, int) or isinstance(cant, bool):
            cant = 0
        if not prod:
            prod = extract_producto(query) or ""
        if not cant:
            #  Ultimo numero del texto (el primero suele ser un id de contexto)
            nums = re.findall(r"(\d+)", query)
            cant = int(nums[-1]) if nums else 0
        return {"producto": prod, "cantidad": cant}

    if name in ("consultar_inventario", "investigar_sospechosos"):
        prod = normalize_producto(str(args.get("producto", "") or "")) or None
        if prod is None:
            prod = extract_producto(query)
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
