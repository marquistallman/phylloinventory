"""env_loader: carga variables de entorno con prioridad definida.

Orden de prioridad (mayor a menor):
  1. Variables ya en os.environ (exportadas en shell, seteadas por Docker
     compose, seteadas por el caller antes de importar este modulo).
  2. Archivo .env del project root (valores del usuario).
  3. Archivo .env.example del project root (defaults razonables).

Reglas:
  - Si una variable YA esta en os.environ, NUNCA se sobreescribe.
  - Si .env existe y define la variable, se setea (si no estaba antes).
  - Si despues de leer .env la variable sigue sin estar, se busca en
    .env.example y se setea.
  - Es idempotente: llamar load_env() varias veces es seguro.
  - Es un no-op si ya esta todo cargado (chequea una sentinel).

Uso:
    from llm_common.env_loader import load_env
    load_env()  # al top del main.py / entry point
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:  # pragma: no cover
    dotenv_values = None  # type: ignore
    load_dotenv = None  # type: ignore


_SENTINEL = "__CACTUS_ENV_LOADED__"


def _strip_inline_comment(value: str) -> str:
    """Quita comentarios inline tipo 'valor  # comentario' -> 'valor'.

    python-dotenv no los maneja por default; nuestros .env/.env.example
    usan el formato `KEY=valor  # explicacion` que viene bien para docs
    pero rompe el parseo si no se les quita el sufijo.
    """
    if value is None:
        return ""
    #  Cortamos en el primer '#' que aparezca (sin comilla antes)
    #  Si el value viene entre comillas, lo dejamos pasar
    s = value.strip()
    if s.startswith(('"', "'")):
        return s
    idx = s.find("#")
    if idx == -1:
        return s
    return s[:idx].rstrip()


def _project_root() -> Path:
    """Busca el project root: directorio que contiene .env o .env.example.

    Sube desde la ubicacion de este archivo hacia arriba hasta encontrarlo.
    Esto permite que el loader funcione tanto desde servicios (deep path)
    como desde la raiz del repo.
    """
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / ".env").exists() or (cand / ".env.example").exists():
            return cand
    #  Fallback: cwd (lo normal cuando se corre `python -m ...` desde la raiz)
    return Path.cwd()


def load_env(override: bool = False, root: Path | None = None) -> dict[str, str]:
    """Carga .env + .env.example. Devuelve dict con TODO lo aplicado.

    Args:
        override: si True, permite que .env sobrescriba el shell env.
                  Default False (el shell gana sobre .env).
        root:    directorio raiz a usar. Default: auto-detectado.

    Returns:
        Dict {nombre_var: valor_final_en_os_environ} con todas las vars
        que este loader termino definiendo (no incluye las que ya estaban).
    """
    if os.environ.get(_SENTINEL) == "1":
        return {}

    if load_dotenv is None or dotenv_values is None:
        #  python-dotenv no instalado: nada que hacer
        os.environ[_SENTINEL] = "1"
        return {}

    project_root = root or _project_root()
    applied: dict[str, str] = {}

    #  1) Cargar .env (si existe). override=False -> el shell gana.
    env_path = project_root / ".env"
    if env_path.is_file():
        for k, v in dotenv_values(env_path).items():
            if v is None:
                continue
            v = _strip_inline_comment(v)
            if not v:
                continue
            if override or k not in os.environ:
                os.environ[k] = v
                applied[k] = v

    #  2) Cargar .env.example solo para vars que SIGUEN sin estar.
    #     Asi .env.example actua como defaults para los huecos.
    example_path = project_root / ".env.example"
    if example_path.is_file():
        for k, v in dotenv_values(example_path).items():
            if v is None:
                continue
            v = _strip_inline_comment(v)
            if not v:
                continue
            if k not in os.environ:
                os.environ[k] = v
                applied[k] = v

    os.environ[_SENTINEL] = "1"
    return applied


def is_loaded() -> bool:
    """True si load_env() ya se ejecuto en este proceso."""
    return os.environ.get(_SENTINEL) == "1"
