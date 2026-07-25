"""env_loader (para la CLI): carga .env / .env.example con la misma
semantica que services/llm_common/env_loader.py.

Duplicado intencional: la CLI corre como `python -m src.cli` y no tiene
`llm_common` en su PYTHONPATH. Mantener una copia chica (~30 lineas) aca
evita ensuciar la config de imports de la CLI.

Prioridad: shell env > .env > .env.example. Idempotente.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover
    dotenv_values = None  # type: ignore


_SENTINEL = "__CACTUS_ENV_LOADED__"


def _strip_inline_comment(value: str) -> str:
    """Quita comentarios inline tipo 'valor  # comentario' -> 'valor'."""
    if value is None:
        return ""
    s = value.strip()
    if s.startswith(('"', "'")):
        return s
    idx = s.find("#")
    if idx == -1:
        return s
    return s[:idx].rstrip()


def _project_root() -> Path:
    """Raiz del proyecto: directorio que contiene .env o .env.example."""
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / ".env").exists() or (cand / ".env.example").exists():
            return cand
    return Path.cwd()


def load_env(override: bool = False, root: Path | None = None) -> dict[str, str]:
    if os.environ.get(_SENTINEL) == "1":
        return {}
    if dotenv_values is None:
        os.environ[_SENTINEL] = "1"
        return {}

    project_root = root or _project_root()
    applied: dict[str, str] = {}

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
    return os.environ.get(_SENTINEL) == "1"
