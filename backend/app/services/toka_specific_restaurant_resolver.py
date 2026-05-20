from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..storage.afisha_catalog_repository import AfishaCatalogRepository
from ..storage.database import get_session_maker


def _norm(s: Any) -> str:
    return str(s or "").strip().lower().replace("ё", "е")


def _sort_by_name_match(candidates: List[Dict[str, Any]], target_name: str) -> List[Dict[str, Any]]:
    tn = _norm(target_name)
    scored: List[Dict[str, Any]] = []
    for c in candidates:
        cn = _norm(c.get("name"))
        exact = 1 if cn == tn else 0
        contains = 1 if tn and tn in cn else 0
        score = exact * 2 + contains
        cc = dict(c)
        cc["_specific_match_score"] = score
        scored.append(cc)
    return sorted(scored, key=lambda x: int(x.get("_specific_match_score") or 0), reverse=True)


def _rows_with_address(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        addr = (r or {}).get("address")
        if isinstance(addr, str) and addr.strip():
            out.append(r)
    return out


def _apply_optional_location_filter(
    rows: List[Dict[str, Any]],
    location_hint: str,
) -> List[Dict[str, Any]]:
    hint = _norm(location_hint)
    if not hint or not rows:
        return rows
    filtered: List[Dict[str, Any]] = []
    for r in rows:
        hay = " ".join(
            x
            for x in (
                r.get("address"),
                r.get("metro"),
                r.get("geo_inferred_metro"),
                r.get("geo_inferred_area"),
            )
            if isinstance(x, str) and x.strip()
        )
        if hint in _norm(hay):
            filtered.append(r)
    return filtered if filtered else rows


def _rank_and_status(
    ranked: List[Dict[str, Any]],
    *,
    db_match_count: int,
    match_mode: str,
) -> Dict[str, Any]:
    strong = [x for x in ranked if int(x.get("_specific_match_score") or 0) >= 1]
    base = {"db_match_count": db_match_count, "match_mode": match_mode, "errors": []}
    if len(strong) == 1:
        return {
            **base,
            "status": "resolved",
            "selected": strong[0],
            "candidates": strong,
        }
    if len(strong) > 1:
        return {
            **base,
            "status": "ambiguous",
            "selected": None,
            "candidates": strong[:5],
        }
    if ranked:
        return {
            **base,
            "status": "ambiguous",
            "selected": None,
            "candidates": ranked[:5],
        }
    return {
        **base,
        "status": "not_found",
        "selected": None,
        "candidates": [],
    }


async def resolve_specific_restaurant_candidates(
    *,
    city_slug: str,
    restaurant_name: str,
    address_hint: str = "",
    max_cards: int = 8,
    city_label: Optional[str] = None,
    llm_chat: Optional[Any] = None,
    llm_node_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resolve a named restaurant using only the Afisha catalog in PostgreSQL.

    *address_hint* is optional: if the user mentioned a street/metro, narrow DB rows
    (not a required search criterion). *llm_chat* / *llm_node_params* are ignored (legacy args).
    """
    _ = (city_label, llm_chat, llm_node_params)
    if not city_slug.strip() or not restaurant_name.strip():
        return {
            "status": "invalid",
            "selected": None,
            "candidates": [],
            "db_match_count": 0,
            "match_mode": "invalid",
            "errors": [],
        }

    lim = max(5, int(max_cards))
    errors: List[str] = []
    try:
        catalog_repo = AfishaCatalogRepository(get_session_maker())
        db_rows, match_mode = catalog_repo.find_rows_for_city_by_restaurant_name(
            city_slug=city_slug.strip(),
            restaurant_name=restaurant_name.strip(),
            limit=lim,
        )
    except Exception as exc:
        errors.append(f"DB catalog search failed: {exc}")
        db_rows = []
        match_mode = "error"

    db_rows = _rows_with_address(db_rows)
    db_rows = _apply_optional_location_filter(db_rows, address_hint.strip())
    db_match_count = len(db_rows)
    ranked = _sort_by_name_match(db_rows, restaurant_name)
    out = _rank_and_status(ranked, db_match_count=db_match_count, match_mode=match_mode)
    out["errors"] = errors
    return out
