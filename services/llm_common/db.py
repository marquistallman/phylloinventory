"""Cliente asincrono para PostgreSQL usando asyncpg.

Lo comparten needle-service, openrouter-service, api-gateway y voice-service.
La conexion se hace por pool asincrono (no psycopg2) para integrarse limpio
con el event loop de FastAPI.

Esquema 3FN:
  unidades, productos (catalogo), stock (per-bodega).
  Para compatibilidad, las queries SELECT usan la view productos_en_bodega
  (misma forma que la antigua tabla productos) — el codigo aguas arriba
  no necesita saber del split.
"""
import json
import os
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
#  Catalogo / stock
#  Todas las funciones devuelven la forma antigua (id per-bodega)
#  usando la view productos_en_bodega para no romper el codigo
#  que ya las consume.
# =====================================================================

async def find_producto(nombre: str, bodega_id: int | None = None) -> dict | None:
    """Busca producto por nombre (case-insensitive).

    Sin bodega_id: match global DETERMINISTA — gana la bodega de menor id
    (bodega_default=1 en el demo), no una fila al azar de las 55 bodegas.
    """
    if bodega_id is not None:
        return await fetchrow(
            """SELECT id, nombre, unidad, stock_actual, bodega_id
               FROM productos_en_bodega
               WHERE lower(nombre) = lower($1) AND bodega_id = $2""",
            nombre, bodega_id,
        )
    return await fetchrow(
        """SELECT id, nombre, unidad, stock_actual, bodega_id
           FROM productos_en_bodega
           WHERE lower(nombre) = lower($1)
           ORDER BY bodega_id, id
           LIMIT 1""",
        nombre,
    )


async def find_producto_fuzzy(query: str, bodega_id: int) -> dict | None:
    """Busca producto con fuzzy matching contra el catalogo de la bodega."""
    from llm_common.fuzzy_search import fuzzy_match_product

    candidates = await fetch(
        """SELECT id, nombre, unidad, stock_actual, bodega_id
           FROM productos_en_bodega
           WHERE bodega_id = $1""",
        bodega_id,
    )
    if not candidates:
        return None
    return fuzzy_match_product(query, candidates)


async def get_catalogo_bodega(
    bodega_id: int,
    q: str | None = None,
    solo_pendientes: bool = False,
    sesion_id: int | None = None,
) -> list[dict]:
    """Retorna el catalogo de productos de una bodega con estado de conteo."""
    params: list[Any] = [bodega_id]
    select_extra = ""
    joins = ""
    where = "WHERE peb.bodega_id = $1"

    if q:
        where += " AND peb.nombre ILIKE $2"
        params.append(f"%{q}%")

    if sesion_id is not None:
        joins = """
            LEFT JOIN registros_conteo rc
                ON rc.producto_id = peb.id
               AND rc.bodega_id   = peb.bodega_id
               AND rc.sesion_id   = $%d
        """ % (len(params) + 1)
        select_extra = """,
            rc.cantidad_normalizada AS stock_contado,
            rc.decision_kalman      AS estado_conteo
        """
        params.append(sesion_id)

    if solo_pendientes:
        if sesion_id is not None:
            where += " AND rc.id IS NULL"
        else:
            where += " AND NOT EXISTS (SELECT 1 FROM registros_conteo rc2 WHERE rc2.producto_id = peb.id AND rc2.bodega_id = peb.bodega_id)"

    sql = f"""
        SELECT
            peb.id, peb.nombre, peb.codigo_articulo, peb.unidad,
            peb.stock_actual AS stock_sistema
            {select_extra}
        FROM productos_en_bodega peb
        {joins}
        {where}
        ORDER BY peb.nombre
    """

    rows = await fetch(sql, *params)
    result = []
    for r in rows:
        item = dict(r)
        if sesion_id is not None:
            if item.get("stock_contado") is not None:
                item["estado_conteo"] = "alerta" if item.get("estado_conteo") == "SOSPECHOSA" else "contado"
            else:
                item["estado_conteo"] = "pendiente"
                item["stock_contado"] = None
        result.append(item)
    return result


async def get_producto_nombres_bodega(bodega_id: int | None = None) -> list[dict]:
    """Retorna id, nombre, unidad de los productos de una bodega.

    bodega_id=None -> catalogo GLOBAL: un representante por nombre
    (gana la bodega de menor id). Es el fallback para que la CLI sin
    bodega activa pueda resolver productos igualmente.
    """
    if bodega_id is not None:
        return await fetch(
            """SELECT id, nombre, unidad
               FROM productos_en_bodega
               WHERE bodega_id = $1
               ORDER BY nombre""",
            bodega_id,
        )
    return await fetch(
        """SELECT DISTINCT ON (lower(nombre)) id, nombre, unidad
           FROM productos_en_bodega
           ORDER BY lower(nombre), bodega_id, id"""
    )


# =====================================================================
#  Cola pending_evaluations
# =====================================================================

async def enqueue_pending(
    *,
    session_id: str,
    tool_name: str,
    arguments: dict,
    bodega_id: int | None = None,
) -> int:
    """Inserta una operacion en la cola pending_evaluations. Retorna el id."""
    from llm_common import nlu

    producto_id: int | None = None
    tipo: str | None = None
    cantidad: float | None = None
    resolved_bodega_id: int | None = None

    if tool_name in ("agregar_inventario", "remover_inventario"):
        prod_nombre = arguments.get("producto", "")
        cant = float(arguments.get("cantidad", 0))
        unidad_usuario = arguments.get("unidad", "") or None

        prod = None
        if prod_nombre and bodega_id:
            prod = await find_producto_fuzzy(prod_nombre, bodega_id)
        if prod is None and prod_nombre:
            prod = await find_producto(prod_nombre)

        if prod is None:
            raise ValueError(f"Producto '{prod_nombre}' no encontrado")

        producto_id = prod["id"]
        resolved_bodega_id = prod.get("bodega_id") or bodega_id
        tipo = "entrada" if tool_name == "agregar_inventario" else "salida"

        unidad_catalogo = prod.get("unidad", "Unidad")
        cantidad_normalizada, _ = nlu.normalize_unidad(cant, unidad_usuario, unidad_catalogo)

        if cantidad_normalizada <= 0:
            raise ValueError("cantidad debe ser > 0")

        cantidad = cantidad_normalizada
        arguments["unidad"] = unidad_catalogo
        arguments["cantidad_normalizada"] = cantidad_normalizada

    elif tool_name == "confirmar_movimiento":
        producto_id = None
        tipo = None
        cantidad = None
        resolved_bodega_id = None
    elif tool_name in ("consultar_inventario", "investigar_sospechosos"):
        raise ValueError(f"tool_name '{tool_name}' no se encola (es lectura)")

    row = await fetchrow(
        """
        INSERT INTO pending_evaluations
            (session_id, tool_name, producto_id, bodega_id, tipo, cantidad, payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        RETURNING id
        """,
        session_id,
        tool_name,
        producto_id,
        resolved_bodega_id,
        tipo,
        cantidad,
        json.dumps(arguments),
    )
    if row is None:
        raise RuntimeError("No se pudo encolar la operacion")

    pending_id = int(row["id"])

    #  Si el session_id es numerico, crear tambien registro_conteo
    if producto_id is not None and cantidad is not None and resolved_bodega_id is not None and session_id.isdigit():
        prod = await fetchrow(
            """SELECT stock_actual
               FROM productos_en_bodega
               WHERE id = $1 AND bodega_id = $2""",
            producto_id, resolved_bodega_id,
        )
        stock_sistema = float(prod["stock_actual"]) if prod else 0.0
        await execute(
            """
            INSERT INTO registros_conteo
                (sesion_id, producto_id, bodega_id, cantidad_contada, unidad_usada,
                 cantidad_normalizada, stock_sistema, decision_kalman, pending_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'PENDIENTE', $8)
            """,
            int(session_id),
            producto_id,
            resolved_bodega_id,
            cantidad,
            arguments.get("unidad_usada", arguments.get("unidad", "Unidad")),
            cantidad,
            stock_sistema,
            pending_id,
        )

    return pending_id


async def enqueue_registro_manual(
    *,
    session_id: str,
    producto_id: int,
    cantidad: float,
    unidad: str,
) -> int:
    """Encola un registro manual (modo Tablet) a pending_evaluations.

    No pasa por LLM — va directo a la cola con producto_id ya resuelto.
    Tambien crea el registro en registros_conteo.
    """
    prod = await fetchrow(
        """SELECT id, nombre, unidad, stock_actual, bodega_id
           FROM productos_en_bodega
           WHERE id = $1""",
        producto_id,
    )
    if prod is None:
        raise ValueError(f"Producto id={producto_id} no encontrado")

    from llm_common import nlu

    bodega_id = prod["bodega_id"]
    unidad_catalogo = prod["unidad"]
    cantidad_normalizada, unidad_final = nlu.normalize_unidad(cantidad, unidad, unidad_catalogo)

    if cantidad_normalizada <= 0:
        raise ValueError("cantidad debe ser > 0")

    arguments = {
        "producto": prod["nombre"],
        "producto_id": producto_id,
        "cantidad": cantidad_normalizada,
        "unidad": unidad_final,
        "unidad_usada": unidad,
    }

    row = await fetchrow(
        """
        INSERT INTO pending_evaluations
            (session_id, tool_name, producto_id, bodega_id, tipo, cantidad, payload)
        VALUES ($1, 'agregar_inventario', $2, $3, 'entrada', $4, $5::jsonb)
        RETURNING id
        """,
        session_id,
        producto_id,
        bodega_id,
        cantidad_normalizada,
        json.dumps(arguments),
    )
    if row is None:
        raise RuntimeError("No se pudo encolar la operacion")

    pending_id = int(row["id"])

    await execute(
        """
        INSERT INTO registros_conteo
            (sesion_id, producto_id, bodega_id, cantidad_contada, unidad_usada,
             cantidad_normalizada, stock_sistema, decision_kalman, pending_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'PENDIENTE', $8)
        """,
        int(session_id) if session_id.isdigit() else None,
        producto_id,
        bodega_id,
        cantidad,
        unidad,
        cantidad_normalizada,
        float(prod["stock_actual"]),
        pending_id,
    )

    return pending_id


async def get_pending_status(pending_id: int) -> dict | None:
    return await fetchrow(
        """
        SELECT id, session_id, tool_name, producto_id, bodega_id, status, decision,
               residual, umbral, movimiento_id, payload, created_at, resolved_at
        FROM pending_evaluations
        WHERE id = $1
        """,
        pending_id,
    )
