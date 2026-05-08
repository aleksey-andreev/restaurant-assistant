#!/usr/bin/env python3
"""
Sync Afisha restaurant catalog into PostgreSQL.

1) URLs from official sitemaps (fast).
2) Optional ``--enrich``: fetch each card HTML and store address, tags, avg_check, service flags,
   ``open_now``, ``card_extras``, Yandex rating (``yandex_rating*``), и гео из OSM (Nominatim + Overpass):
   ``geo_inferred_*``, ``geo_osm_metros``, ``geo_osm_at``. Метро с карточки Афиши не сохраняется.

Usage (from repo root, with ``.env`` containing ``DATABASE_URL``):

  python3 backend/scripts/sync_afisha_catalog.py spb
  python3 backend/scripts/sync_afisha_catalog.py spb --enrich
  python3 backend/scripts/sync_afisha_catalog.py msk --enrich --enrich-limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx


async def _enrich_urls(
    city_slug: str,
    urls: List[str],
    *,
    concurrency: int,
    enrich_limit: Optional[int],
    enrich_yandex_rating: bool,
    force_geo_refresh: bool,
    existing_geo_by_url: Dict[str, Dict[str, Optional[object]]],
    persist_batch: Optional[Callable[[List[Dict]], int]] = None,
) -> Tuple[List[Dict], int]:
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    from app.services.afisha_city_slug import display_city_label_for_slug
    from app.services.afisha_parser import fetch_and_parse_afisha_card
    from app.services.external_rating import enrich_candidate_external_rating_structured
    from app.services.llm import LLMClientRegistry
    from app.services.osm_geo import osm_geo_enabled, resolve_osm_geo
    from app.services.searxng_search import SearxngSearchClient
    from app.services.yandex_web_search import YandexWebSearchClient
    from app.storage.afisha_catalog_repository import candidate_dict_to_enriched_row

    take = urls if enrich_limit is None else urls[: max(1, enrich_limit)]
    sem = asyncio.Semaphore(max(1, concurrency))
    ysem = asyncio.Semaphore(max(1, int(os.environ.get("AFISHA_ENRICH_YANDEX_CONCURRENCY", "2"))))

    city_label = display_city_label_for_slug(city_slug)

    do_yandex = enrich_yandex_rating and os.environ.get("AFISHA_ENRICH_YANDEX_RATING", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    y_client = None
    rating_llm_chat = None
    rating_llm_params: Dict = {}
    rating_provider = (os.environ.get("AFISHA_RATING_SEARCH_PROVIDER") or "yandex").strip().lower()
    if do_yandex:
        try:
            if rating_provider == "searxng":
                y_client = SearxngSearchClient.from_env()
            else:
                y_client = YandexWebSearchClient.from_env()
        except Exception:
            do_yandex = False
        if do_yandex:
            try:
                llm_client, _sp, rating_llm_params = LLMClientRegistry.from_config().get_default_node()
                rating_llm_chat = llm_client.chat
            except Exception:
                rating_llm_chat = None
                rating_llm_params = {}

    ua = os.environ.get("OSM_HTTP_USER_AGENT", "RestaurantAssistant-enrich/1.0")
    timeout = httpx.Timeout(45.0)

    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": ua}) as http:

        async def one(u: str) -> Optional[Dict]:
            async with sem:
                try:
                    cand = await fetch_and_parse_afisha_card(u)
                except Exception:
                    return None
                row = candidate_dict_to_enriched_row(city_slug, cand)
                prev_geo = existing_geo_by_url.get(str(row.get("url") or "").strip(), {})
                prev_geo_osm_at = prev_geo.get("geo_osm_at")
                has_prev_geo = (
                    prev_geo_osm_at is not None
                    or prev_geo.get("geo_inferred_metro") is not None
                    or prev_geo.get("geo_inferred_area") is not None
                    or prev_geo.get("geo_osm_metros") is not None
                )
                # Keep None by default: DB upsert uses COALESCE and preserves existing geo values.
                row["geo_inferred_metro"] = None
                row["geo_inferred_area"] = None
                row["geo_osm_metros"] = None
                row["geo_osm_at"] = None
                row["geo_osm_error"] = None
                row["geo_osm_error_at"] = None
                row["geo_llm_at"] = None
                row["yandex_rating"] = None
                row["yandex_rating_confidence"] = None
                row["yandex_rating_at"] = None
                if osm_geo_enabled() and (force_geo_refresh or not has_prev_geo):
                    try:
                        og = await resolve_osm_geo(
                            address=str(row.get("address") or "").strip() or None,
                            name=str(row.get("name") or "").strip() or None,
                            city=city_label,
                            client=http,
                        )
                        if og.ok:
                            row["geo_inferred_area"] = og.district
                            row["geo_inferred_metro"] = og.primary_metro
                            row["geo_osm_metros"] = og.metros if og.metros else None
                            row["geo_osm_at"] = datetime.utcnow()
                            row["geo_osm_error"] = None
                            row["geo_osm_error_at"] = None
                            row["geo_llm_at"] = None
                        else:
                            row["geo_osm_error"] = "osm_no_result"
                            row["geo_osm_error_at"] = datetime.utcnow()
                    except Exception:
                        row["geo_osm_error"] = "osm_exception"
                        row["geo_osm_error_at"] = datetime.utcnow()
                if do_yandex and y_client is not None and city_label:
                    nm = str(row.get("name") or "").strip()
                    if nm:
                        try:
                            async with ysem:
                                rr = await enrich_candidate_external_rating_structured(
                                    y_client,  # type: ignore[arg-type]
                                    restaurant_name=nm,
                                    city=city_label,
                                    address=str(row.get("address") or "").strip() or None,
                                    llm_chat=rating_llm_chat,
                                    node_params=rating_llm_params,
                                )
                        except Exception:
                            rr = {"rating": None, "confidence": 0.0, "sources": []}
                        r = rr.get("rating")
                        conf = rr.get("confidence")
                        sources = rr.get("sources")
                        if r is not None:
                            row["yandex_rating"] = r
                            row["yandex_rating_confidence"] = conf
                            row["yandex_rating_at"] = datetime.utcnow()
                        if isinstance(sources, list) and sources:
                            ex = row.get("card_extras")
                            if not isinstance(ex, dict):
                                ex = {}
                            ex["rating_sources"] = sources
                            row["card_extras"] = ex
                return row

        out: List[Dict] = []
        persisted_total = 0
        step = 80
        for i in range(0, len(take), step):
            chunk = take[i : i + step]
            batch = await asyncio.gather(*[one(u) for u in chunk])
            ready_rows: List[Dict] = []
            for row in batch:
                if row and row.get("url"):
                    ready_rows.append(row)
            if ready_rows:
                if persist_batch is not None:
                    persisted_total += int(persist_batch(ready_rows) or 0)
                else:
                    out.extend(ready_rows)
            print(f"  enrich {min(i + step, len(take))}/{len(take)}", flush=True)
        return out, persisted_total


async def _run(
    city_slug: str,
    *,
    enrich: bool,
    enrich_limit: Optional[int],
    enrich_concurrency: int,
    enrich_yandex_rating: bool,
    force: bool,
) -> Tuple[int, int]:
    backend = Path(__file__).resolve().parents[1]
    project = backend.parent
    sys.path.insert(0, str(backend))

    from dotenv import load_dotenv

    load_dotenv(project / ".env")

    from app.services.afisha_catalog_sitemap import fetch_restaurant_urls_for_city
    from app.storage.afisha_catalog_repository import AfishaCatalogRepository
    from app.storage.database import get_session_maker, init_db

    init_db()
    urls = await fetch_restaurant_urls_for_city(city_slug)
    repo = AfishaCatalogRepository(get_session_maker())
    n_urls = repo.upsert_url_index(city_slug, urls)
    n_enriched = 0
    if enrich:
        limit_for_existing = enrich_limit if enrich_limit is not None else max(1, len(urls))
        existing_rows = repo.list_catalog_rows_for_city(city_slug, limit=limit_for_existing)
        existing_geo_by_url: Dict[str, Dict[str, Optional[object]]] = {}
        for r in existing_rows:
            u = str(r.get("url") or "").strip()
            if not u:
                continue
            existing_geo_by_url[u] = {
                "geo_inferred_metro": r.get("geo_inferred_metro"),
                "geo_inferred_area": r.get("geo_inferred_area"),
                "geo_osm_metros": r.get("geo_osm_metros"),
                "geo_osm_at": r.get("geo_osm_at"),
            }
        rows, persisted_total = await _enrich_urls(
            city_slug,
            urls,
            concurrency=enrich_concurrency,
            enrich_limit=enrich_limit,
            enrich_yandex_rating=enrich_yandex_rating,
            force_geo_refresh=force,
            existing_geo_by_url=existing_geo_by_url,
            persist_batch=repo.upsert_enriched_rows,
        )
        if persisted_total > 0:
            n_enriched = persisted_total
        elif rows:
            n_enriched = repo.upsert_enriched_rows(rows)
    return n_urls, n_enriched


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Afisha restaurant catalog into DB.")
    parser.add_argument(
        "city_slug",
        help="Afisha city path segment, e.g. spb, msk, voronezh (see afisha_city_slug).",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Fetch each card and store address/tags/avg_check (slow; respect Afisha load).",
    )
    parser.add_argument(
        "--enrich-limit",
        type=int,
        default=None,
        metavar="N",
        help="Only enrich first N URLs (for dev smoke tests).",
    )
    parser.add_argument(
        "--enrich-concurrency",
        type=int,
        default=int(os.environ.get("AFISHA_ENRICH_CONCURRENCY", "5")),
        help="Parallel HTTP fetches during enrich (default 5 or AFISHA_ENRICH_CONCURRENCY).",
    )
    parser.add_argument(
        "--skip-yandex-rating",
        action="store_true",
        help="During --enrich, do not call Yandex Search for per-venue rating (card HTML only).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --enrich: recompute and overwrite geo even if already present in DB.",
    )
    args = parser.parse_args()
    city_slug = args.city_slug.strip().lower()
    if not city_slug:
        parser.error("city_slug required")
    n_urls, n_enriched = asyncio.run(
        _run(
            city_slug,
            enrich=args.enrich,
            enrich_limit=args.enrich_limit,
            enrich_concurrency=args.enrich_concurrency,
            enrich_yandex_rating=not args.skip_yandex_rating,
            force=args.force,
        )
    )
    print(f"URL index upserted: {n_urls} for city_slug={city_slug!r}")
    if args.enrich:
        print(f"Enriched rows upserted: {n_enriched}")


if __name__ == "__main__":
    main()
