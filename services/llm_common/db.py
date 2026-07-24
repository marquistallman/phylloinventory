"""Cliente asincrono para PostgreSQL usando asyncpg.

Lo comparten needle-service, openrouter-service, api-gateway y voice-service.
La conexion se hace por pool asincrono (no psycopg2) para integrarse limpio
con el event loop de FastAPI.
"""
import os
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.getenv(
            "DATABASE_URL",
            "postgres://cactus:cactus@postgres:5432/inventario",
        )
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def fetch(sql: str, *args: Any) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]


async def fetchrow(sql: str, *args: Any) -> dict | None:
    async with acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None


async def execute(sql: str, *args: Any) -> str:
    async with acquire() as conn:
        return await conn.execute(sql, *args)


# =====================================================================
#  Helpers especificos de la cola pending_evaluations
# =====================================================================

async def find_producto(nombre: str) -> dict | None:
    return await fetchrow("SELECT id, nombre, stock_actual FROM productos WHERE nombre = $1", nombre)


async def enqueue_pending(
    *,
    session_id: str,
    tool_name: str,
    arguments: dict,
) -> int:
    """Inserta una operacion en la cola pending_evaluations. Retorna el id."""
    producto_id: int | None = None
    tipo: str | None = None
    cantidad: int | None = None

    if tool_name in ("agregar_inventario", "remover_inventario"):
        prod = await find_producto(arguments.get("producto", ""))
        if prod is None:
            raise ValueError(f"Producto '{arguments.get('producto')}' no encontrado")
        producto_id = prod["id"]
        tipo = "entrada" if tool_name == "agregar_inventario" else "salida"
        cantidad = int(arguments.get("cantidad", 0))
        if cantidad <= 0:
            raise ValueError("cantidad debe ser > 0")
    elif tool_name == "confirmar_movimiento":
        #  Para confirmaciones no se necesita producto_id; el worker lo
        #  resuelve mirando el payload.
        producto_id = None
        tipo = None
        cantidad = None
    elif tool_name in ("consultar_inventario", "investigar_sospechosos"):
        #  Reads no van a la cola — se responden sincronicos.
        raise ValueError(f"tool_name '{tool_name}' no se encola (es lectura)")

    row = await fetchrow(
        """
        INSERT INTO pending_evaluations
            (session_id, tool_name, producto_id, tipo, cantidad, payload)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id
        """,
        session_id,
        tool_name,
        producto_id,
        tipo,
        cantidad,
        json.dumps(arguments),
    )
    if row is None:
        raise RuntimeError("No se pudo encolar la operacion")
    return int(row["id"])


async def get_pending_status(pending_id: int) -> dict | None:
    return await fetchrow(
        """
        SELECT id, session_id, tool_name, status, decision,
               residual, umbral, movimiento_id, payload, created_at, resolved_at
        FROM pending_evaluations
        WHERE id = $1
        """,
        pending_id,
    )
