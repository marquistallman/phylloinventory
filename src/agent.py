import json
import os
import re
from typing import Callable

import requests

TOOL_EXECUTOR_URL = os.getenv("TOOL_SERVER_URL", "http://127.0.0.1:8000")
NEEDLE_URL = os.getenv("NEEDLE_URL", "http://127.0.0.1:8081")

PRODUCTOS_RAW = {"papa", "cebolla", "tomate", "zanahoria", "ajo",
                 "papas", "cebollas", "tomates", "zanahorias", "ajos"}
PRODUCTO_SINGULAR = {
    "papas": "papa", "cebollas": "cebolla", "tomates": "tomate",
    "zanahorias": "zanahoria", "ajos": "ajo",
}


def call_tool(name: str, arguments: dict) -> dict:
    url = f"{TOOL_EXECUTOR_URL}/tool/{name}"
    resp = requests.post(url, json=arguments, timeout=10)
    resp.raise_for_status()
    return resp.json()


def execute_tools(calls: list[dict], print_fn: Callable = print) -> list[dict]:
    results = []
    for call in calls:
        name = call["name"]
        args = call["arguments"]
        print_fn(f"  🔧 [{name}] {json.dumps(args, ensure_ascii=False)}")
        result = call_tool(name, args)
        results.append(result)
    return results


NO_PRODUCTO = {"agregar", "agrega", "saca", "remover", "quita", "vender", "retirar",
               "pone", "poner", "carga", "cargar", "mete", "meter", "meteme",
               "ingresa", "ingresar", "auditoria", "auditar", "raro", "error",
               "sospechoso", "investigar", "consultar", "inventario", "stock",
               "todo", "todos", "cuanto", "hay", "tenemos", "revisa", "revisar",
               "dime", "como", "esta", "el", "la", "los", "las", "del", "de",
               "una", "un", "unas", "unos", "muestrame", "dame", "ver", "mostrar"}

def _normalize_producto(val: str) -> str:
    val = val.strip().lower()
    if val in PRODUCTO_SINGULAR:
        return PRODUCTO_SINGULAR[val]
    if val in PRODUCTOS_RAW:
        return val
    for p in PRODUCTOS_RAW:
        if p in val:
            return PRODUCTO_SINGULAR.get(p, p)
    return val

def _is_producto(val: str) -> bool:
    return val not in NO_PRODUCTO and _normalize_producto(val) != val or val in PRODUCTOS_RAW


def _extract_number(val) -> int | None:
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        m = re.search(r"(\d+)", val)
        return int(m.group(1)) if m else None
    return None


def _extract_producto(query: str) -> str | None:
    for p in PRODUCTOS_RAW:
        if p in query.lower():
            return _normalize_producto(p)
    return None


_CONFIRMA_RE = re.compile(
    r"\b(si|sí|dale|confirma\w*|ok|okay|yes|acepta\w*|va|hazlo|metele|adelante)\b",
    re.IGNORECASE,
)
_RECHAZA_RE = re.compile(
    r"\b(no\b(?!\s+(s[ée]|estoy|creo)\b)|nope|negativo|rechaza\w*|cancela\w*|descarta\w*|anula\w*)",
    re.IGNORECASE,
)


def _parse_confirmacion(text: str) -> bool | None:
    """Detecta si el usuario confirma (True) o rechaza (False).

    None si no hay una intencion clara. Gana la palabra que aparece primero.
    """
    matches = [(m.start(), True) for m in _CONFIRMA_RE.finditer(text)]
    matches += [(m.start(), False) for m in _RECHAZA_RE.finditer(text)]
    if not matches:
        return None
    matches.sort()
    return matches[0][1]


def _normalize_args(name: str, args: dict, query: str) -> dict:
    if name in ("agregar_inventario", "remover_inventario"):
        prod = _normalize_producto(str(args.get("producto", "")))
        cant = _extract_number(args.get("cantidad"))
        if not prod or prod in NO_PRODUCTO:
            prod = _extract_producto(query)
        if not cant:
            nums = re.findall(r"(\d+)", query)
            cant = int(nums[-1]) if nums else 0
        return {"producto": prod or "", "cantidad": cant or 0}

    if name in ("consultar_inventario", "investigar_sospechosos"):
        prod = _normalize_producto(str(args.get("producto", "")))
        if prod in NO_PRODUCTO:
            prod = None
        if not prod:
            prod = _extract_producto(query)
        return {"producto": prod}

    if name == "confirmar_movimiento":
        mid = _extract_number(args.get("movimiento_id")) or 0
        conf = args.get("confirmar", True)
        if isinstance(conf, str):
            conf = conf.lower() in ("true", "si", "yes", "1")
        return {"movimiento_id": mid, "confirmar": bool(conf)}

    return dict(args)


class NeedleHTTPAgent:

    def __init__(self, server_url: str | None = None):
        self.server_url = server_url or NEEDLE_URL
        self._l1: dict = {}
        self._l2: dict = {}
        self._l3: dict = {}

    def load_tools(self, tools_path: str):
        with open(tools_path, encoding="utf-8") as f:
            data = json.load(f)
        self._l1 = data["l1_tools"]
        self._l2 = {"leer_inventario": data["l2_read"], "modificar_inventario": data["l2_write"]}
        self._l3 = data["l3_args"]

    def _infer_raw(self, query: str, tools: list[dict]) -> tuple[list[dict], str]:
        resp = requests.post(
            f"{self.server_url}/infer",
            json={"query": query, "tools": json.dumps(tools)},
            timeout=60,
        )
        resp.raise_for_status()
        d = resp.json()
        raw = d.get("raw_output", "")
        calls = [{"name": tc["name"], "arguments": tc.get("arguments", {})}
                 for tc in d.get("tool_calls", [])]
        return calls, raw

    def infer(self, query: str, pending_alert: dict | None = None) -> tuple[list[dict], str]:
        if not self._l1:
            raise RuntimeError("Tools not loaded.")

        full_raw = []

        if pending_alert:
            mid = pending_alert["movimiento_id"]

            # 1) Via determinista: un si/no no deberia depender de un modelo de 26M
            conf = _parse_confirmacion(query)
            if conf is not None:
                full_raw.append(f"regex:confirmar={conf}")
                return [{"name": "confirmar_movimiento",
                         "arguments": {"movimiento_id": mid, "confirmar": conf}}], " | ".join(full_raw)

            # 2) Needle solo con el schema de confirmar
            ctx = (f"Hay una alerta de inventario pendiente (ID {mid}, "
                   f"producto {pending_alert['producto']}, {pending_alert['cantidad']} unidades). "
                   f"El usuario dice: {query}")
            schema = self._l3.get("confirmar_movimiento")
            if schema:
                c3, r3 = self._infer_raw(ctx, [schema])
                full_raw.append(r3)
                for tc in c3:
                    if tc["name"] == "confirmar_movimiento":
                        args = _normalize_args("confirmar_movimiento", tc["arguments"], ctx)
                        args["movimiento_id"] = mid  # el ID real es el del estado local
                        return [{"name": "confirmar_movimiento", "arguments": args}], " | ".join(full_raw)

            # 3) Con alerta pendiente NUNCA se cae al pipeline generico:
            #    el contexto de la alerta dispararia escrituras fantasma.
            return [], " | ".join(full_raw)

        first_result = self._pipeline(query, list(self._l1), full_raw)
        if first_result and not self._is_suspicious(query, first_result):
            return first_result, " | ".join(full_raw)

        has_p = _extract_producto(query) is not None
        has_n = bool(re.search(r"(\d+)", query))
        needs_write = has_p and has_n
        needs_read = has_p and not has_n

        failed_l1 = None
        failed_l2 = None
        for r in full_raw:
            if r.startswith("L1:"):
                failed_l1 = r[3:]
            elif r.startswith("L2:"):
                failed_l2 = r[3:]

        if not failed_l1:
            return first_result or [], " | ".join(full_raw)

        # Si la query tiene producto pero la tool no cuadra → retry
        wrong_l2 = (
            (needs_write and failed_l2 in ("investigar_sospechosos", "consultar_inventario"))
            or (needs_read and failed_l2 in ("agregar_inventario", "remover_inventario"))
        )
        if wrong_l2:
            alt_l2 = [t for t in self._l2.get(failed_l1, []) if t["name"] != failed_l2]
            if alt_l2:
                full_raw.append("retry:L2")
                c2, r2 = self._infer_raw(query, alt_l2)
                full_raw.append(r2)
                if c2:
                    l2c = c2[0]["name"]
                    full_raw.append(f"L2:{l2c}")
                    # Si la alternativa tambien no cuadra → escalar L1
                    still_wrong = (
                        (needs_write and l2c in ("investigar_sospechosos", "consultar_inventario"))
                        or (needs_read and l2c in ("agregar_inventario", "remover_inventario"))
                    )
                    if still_wrong:
                        full_raw.append("escalate:L1")
                    else:
                        schema = self._l3.get(l2c)
                        if schema:
                            c3, r3 = self._infer_raw(query, [schema])
                            full_raw.append(r3)
                            if c3:
                                args = _normalize_args(l2c, c3[0]["arguments"], query)
                                return [{"name": l2c, "arguments": args}], " | ".join(full_raw)

        # Reintentar con la otra L1
        alt_l1 = [t for t in self._l1 if t["name"] != failed_l1]
        full_raw.append("retry:L1")
        result2 = self._pipeline(query, alt_l1, full_raw)
        if result2:
            return result2, " | ".join(full_raw)

        return first_result or [], " | ".join(full_raw)

    def _is_suspicious(self, query: str, result: list[dict]) -> bool:
        if not result:
            return True
        tool_name = result[0]["name"]
        has_p = _extract_producto(query) is not None
        has_n = bool(re.search(r"(\d+)", query))
        if has_p and has_n and tool_name in ("investigar_sospechosos", "consultar_inventario"):
            return True
        if has_p and not has_n and tool_name in ("agregar_inventario", "remover_inventario"):
            return True
        return False

    def _pipeline(self, query, l1_options, full_raw):
        c1, r1 = self._infer_raw(query, l1_options)
        full_raw.append(r1)
        if not c1:
            return None

        l1_choice = c1[0]["name"]
        full_raw.append(f"L1:{l1_choice}")

        l2_tools = self._l2.get(l1_choice, [])
        c2, r2 = self._infer_raw(query, l2_tools)
        full_raw.append(r2)
        if not c2:
            return None

        l2_choice = c2[0]["name"]
        full_raw.append(f"L2:{l2_choice}")

        schema = self._l3.get(l2_choice)
        if schema:
            c3, r3 = self._infer_raw(query, [schema])
            full_raw.append(r3)
            if c3:
                args = _normalize_args(l2_choice, c3[0]["arguments"], query)
                return [{"name": l2_choice, "arguments": args}]

        return None

    def health(self) -> bool:
        try:
            r = requests.get(f"{self.server_url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False


class CactusAgent:
    def __init__(self, server_url: str | None = None):
        self.server_url = server_url or os.getenv("CACTUS_SERVER_URL", "http://127.0.0.1:8080")
        self.tools: list[dict] = []

    def load_tools(self, tools_path: str):
        with open(tools_path, encoding="utf-8") as f:
            data = json.load(f)
        self.tools = data["tools"]

    def process(self, query: str) -> str:
        resp = requests.post(
            f"{self.server_url}/v1/chat/completions",
            json={
                "model": "cactus",
                "messages": [
                    {"role": "system", "content": "Eres un asistente de inventario."},
                    {"role": "user", "content": query},
                ],
                "tools": self.tools,
                "temperature": 0.1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]

        if msg.get("tool_calls"):
            tool_results = []
            for tc in msg["tool_calls"]:
                name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"])
                result = call_tool(name, args)
                tool_results.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})

            resp2 = requests.post(
                f"{self.server_url}/v1/chat/completions",
                json={
                    "model": "cactus",
                    "messages": [
                        {"role": "system", "content": "Eres un asistente de inventario."},
                        {"role": "user", "content": query},
                        msg,
                        *tool_results,
                    ],
                    "temperature": 0.1,
                },
                timeout=30,
            )
            resp2.raise_for_status()
            return resp2.json()["choices"][0]["message"]["content"]

        return msg.get("content", "")


def parse_intent_fallback(text: str) -> list[dict]:
    text = text.lower().strip()
    calls = []

    for pattern, tool_name in [
        (r"(?:agreg(?:ar?|a)|anadir?|ingresa[r]?|met(?:er?|a|eme)|pon(?:er?|e|ga?)|carga[r]?)\s+(\d+)\s+(?:de\s+)?(?:unidades?\s+(?:de\s+)?)?(\w[\w\s]*)", "agregar_inventario"),
        (r"(?:sac(?:ar?|a)|remover|retirar|quita[r]?|vender|descontar|restar)\s+(\d+)\s+(?:de\s+)?(\w[\w\s]*)", "remover_inventario"),
    ]:
        m = re.search(pattern, text)
        if m:
            calls.append({"name": tool_name, "arguments": {"producto": m.group(2).strip().rstrip("s"), "cantidad": int(m.group(1))}})

    if re.search(r"consultar|ver|mostrar|cu[aá]nt[oa]s?\s+(?:hay|tenemos|quedan)|qu[eé]\s+(?:hay|tenemos)|listar|inventario\s*$|c[oó]mo\s+est[aá]", text):
        m = re.search(r"(?:de\s+|del\s+|en\s+)?(\w+)\s*$", text)
        producto = None
        if m and m.group(1) not in ("hay", "tenemos", "quedan", "inventario", "stock"):
            producto = m.group(1).strip().rstrip("s")
        calls.append({"name": "consultar_inventario", "arguments": {"producto": producto}})

    if re.search(r"investigar|sospechos[oa]|raro|error|auditor[ií]a|discrepancia|revisa[r]?|audita[r]?", text):
        calls.append({"name": "investigar_sospechosos", "arguments": {"producto": None}})

    return calls
