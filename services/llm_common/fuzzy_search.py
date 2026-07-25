"""Fuzzy search sobre catalogo de productos por bodega.

Usa rapidfuzz con token_sort_ratio para matching robusto de nombres
en espanol. La busqueda se limita a la bodega activa (~50-350 productos).
"""
from __future__ import annotations

from rapidfuzz import process, fuzz


def fuzzy_match_product(
    query: str,
    candidates: list[dict],
    *,
    threshold: int = 75,
) -> dict | None:
    """Busca el mejor match de `query` entre una lista de productos.

    Args:
        query: Texto a buscar (ej: "harina trigo")
        candidates: Lista de dicts con al menos {"id", "nombre", "unidad"}
        threshold: Puntaje minimo de similitud (0-100)

    Returns:
        El dict del producto matcheado, o None si no hay match.
    """
    if not candidates or not query.strip():
        return None

    q = query.strip().lower()
    names = [p["nombre"].lower() for p in candidates]

    result = process.extractOne(q, names, scorer=fuzz.token_sort_ratio)
    if result is None:
        return None

    matched_name, score, idx = result
    if score < threshold:
        return None

    return candidates[idx]


def fuzzy_search_candidates(
    query: str,
    candidates: list[dict],
    *,
    limit: int = 10,
    threshold: int = 60,
) -> list[dict]:
    """Retorna los N mejores matches ordenados por score.

    Util para mostrar opciones cuando el mejor match es ambiguo.
    """
    if not candidates or not query.strip():
        return []

    q = query.strip().lower()
    names = [p["nombre"].lower() for p in candidates]

    results = process.extract(q, names, scorer=fuzz.token_sort_ratio, limit=limit)
    return [
        candidates[idx]
        for _, score, idx in results
        if score >= threshold
    ]