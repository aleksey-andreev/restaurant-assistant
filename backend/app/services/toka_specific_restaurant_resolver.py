from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

from .afisha_parser import fetch_and_parse_afisha_card
from .afisha_urls import filter_and_order_afisha_restaurant_urls
from .external_rating import enrich_candidate_external_rating_structured
from .yandex_web_search import YandexWebSearchClient
from ..storage.afisha_catalog_repository import AfishaCatalogRepository
from ..storage.database import get_session_maker


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _build_queries(city_slug: str, name: str, hint: str) -> List[str]:
    base = f"site:afisha.ru {city_slug}/restaurant"
    q1 = f'{base} "{name}"'
    q2 = f'{base} "{name}" {hint}'.strip() if hint else q1
    q3 = f'{base} "{name}" меню'.strip()
    q4 = f'{base} "{name}" адрес'.strip()
    seen = set()
    out: List[str] = []
    for q in [q1, q2, q3, q4]:
        qn = q.strip()
        if not qn or qn in seen:
            continue
        seen.add(qn)
        out.append(qn)
    return out[:4]


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


def _is_junk_name(name: str) -> bool:
    n = _norm(name)
    if not n:
        return True
    junk_markers = [" отзывы", ", отзывы", " меню", ", меню", " фото", ", фото"]
    return any(m in n for m in junk_markers)


async def resolve_specific_restaurant_candidates(
    *,
    city_slug: str,
    restaurant_name: str,
    address_hint: str = "",
    max_cards: int = 8,
    city_label: Optional[str] = None,
    llm_chat: Optional[Callable[..., Awaitable[str]]] = None,
    llm_node_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not city_slug.strip() or not restaurant_name.strip():
        return {"status": "invalid", "candidates": [], "queries": [], "errors": []}

    errors: List[str] = []
    urls: List[str] = []
    queries = _build_queries(city_slug.strip(), restaurant_name.strip(), address_hint.strip())

    try:
        client = YandexWebSearchClient.from_env()
    except Exception as exc:
        return {
            "status": "search_error",
            "candidates": [],
            "queries": queries,
            "errors": [str(exc)],
        }

    for q in queries:
        try:
            urls.extend(await client.search(q, page=0, max_docs=30))
        except Exception as exc:
            errors.append(f"Yandex search failed for query '{q}': {exc}")

    card_urls = filter_and_order_afisha_restaurant_urls(urls)[: max(1, int(max_cards))]
    parsed: List[Dict[str, Any]] = []
    for url in card_urls:
        try:
            c = await fetch_and_parse_afisha_card(url)
            if not isinstance(c, dict):
                continue
            if c.get("venue_closed"):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            if _is_junk_name(name):
                continue
            # Option A (strict): for specific booking flow, address is mandatory.
            address = c.get("address")
            if not (isinstance(address, str) and address.strip()):
                continue
            # Extract structured rating signals for specific flow card preview.
            try:
                rr = await enrich_candidate_external_rating_structured(
                    client,
                    restaurant_name=name,
                    city=city_label or city_slug,
                    address=address.strip(),
                    llm_chat=llm_chat,
                    node_params=(llm_node_params or {}),
                )
            except Exception:
                rr = {"rating": None, "confidence": 0.0, "sources": []}
            if rr.get("rating") is not None:
                c["external_rating"] = rr.get("rating")
                c["external_rating_confidence"] = rr.get("confidence")
            if isinstance(rr.get("sources"), list) and rr.get("sources"):
                extras = c.get("card_extras")
                if not isinstance(extras, dict):
                    extras = {}
                extras["rating_sources"] = rr.get("sources")
                c["card_extras"] = extras
            parsed.append(c)
        except Exception:
            continue

    ranked = _sort_by_name_match(parsed, restaurant_name)
    strong = [x for x in ranked if int(x.get("_specific_match_score") or 0) >= 1]
    if len(strong) == 1:
        return {
            "status": "resolved",
            "selected": strong[0],
            "candidates": strong,
            "queries": queries,
            "errors": errors,
        }
    if len(strong) > 1:
        return {
            "status": "ambiguous",
            "selected": None,
            "candidates": strong[:5],
            "queries": queries,
            "errors": errors,
        }
    if ranked:
        return {
            "status": "ambiguous",
            "selected": None,
            "candidates": ranked[:5],
            "queries": queries,
            "errors": errors,
        }

    # Fallback: restaurant might exist only in our DB catalog.
    try:
        catalog_repo = AfishaCatalogRepository(get_session_maker())
        db_rows = catalog_repo.find_rows_for_city_by_name_like(
            city_slug=city_slug,
            restaurant_name=restaurant_name,
            limit=max(5, int(max_cards)),
        )
    except Exception as exc:
        errors.append(f"DB fallback search failed: {exc}")
        db_rows = []

    # Defensive filter: fallback must require address.
    db_rows = [
        r
        for r in (db_rows or [])
        if isinstance((r or {}).get("address"), str) and (r or {}).get("address", "").strip()
    ]

    db_ranked = _sort_by_name_match(db_rows, restaurant_name)
    db_strong = [x for x in db_ranked if int(x.get("_specific_match_score") or 0) >= 1]
    if len(db_strong) == 1:
        return {
            "status": "resolved",
            "selected": db_strong[0],
            "candidates": db_strong,
            "queries": queries,
            "errors": errors,
        }
    if len(db_strong) > 1:
        return {
            "status": "ambiguous",
            "selected": None,
            "candidates": db_strong[:5],
            "queries": queries,
            "errors": errors,
        }
    if db_ranked:
        return {
            "status": "ambiguous",
            "selected": None,
            "candidates": db_ranked[:5],
            "queries": queries,
            "errors": errors,
        }

    return {
        "status": "not_found",
        "selected": None,
        "candidates": [],
        "queries": queries,
        "errors": errors,
    }
