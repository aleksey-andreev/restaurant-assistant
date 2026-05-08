#!/usr/bin/env python3
"""
Offline geo for rows in ``afisha_restaurants``.

Default: **OSM** (Nominatim forward geocode + Overpass subway nodes) — same logic as
``sync_afisha_catalog.py --enrich``. Suitable for background batches; respect OSM usage
policies (delay between Nominatim requests).

Optional ``--llm``: legacy LLM path (``llm_geo_infer_one``) instead of OSM.

Examples::

  python3 backend/scripts/geo_backfill_afisha_catalog.py spb --limit 100
  python3 backend/scripts/geo_backfill_afisha_catalog.py spb --limit 500 --concurrency 2
  python3 backend/scripts/geo_backfill_afisha_catalog.py spb --force --limit 50
  python3 backend/scripts/geo_backfill_afisha_catalog.py spb --limit 20 --llm
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


async def _run_osm(
    city_slug: str,
    *,
    limit: int,
    concurrency: int,
    force: bool,
    only_errors: bool,
) -> int:
    backend = Path(__file__).resolve().parents[1]
    project = backend.parent
    sys.path.insert(0, str(backend))

    from dotenv import load_dotenv

    load_dotenv(project / ".env")

    from app.services.afisha_city_slug import display_city_label_for_slug
    from app.services.osm_geo import osm_geo_enabled, resolve_osm_geo
    from app.storage.afisha_catalog_repository import AfishaCatalogRepository
    from app.storage.database import get_session_maker, init_db

    init_db()
    if not osm_geo_enabled():
        print("OSM_GEO_ENABLED is off; nothing to do.")
        return 0
    city_label = display_city_label_for_slug(city_slug)
    repo = AfishaCatalogRepository(get_session_maker())
    rows = repo.list_rows_for_osm_geo_backfill(
        city_slug,
        limit=limit,
        force=force,
        only_errors=only_errors,
    )
    if not rows:
        print("No rows to process (check city_slug, --limit, --only-errors, geo_osm_at, or --force).")
        return 0

    sem = asyncio.Semaphore(max(1, concurrency))
    ua = os.environ.get("OSM_HTTP_USER_AGENT", "RestaurantAssistant-geo-backfill/1.0")
    timeout = httpx.Timeout(45.0)

    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": ua}) as http:

        async def one(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            url = str(row.get("url") or "").strip()
            if not url:
                return None
            async with sem:
                try:
                    og = await resolve_osm_geo(
                        address=str(row.get("address") or "").strip() or None,
                        name=str(row.get("name") or "").strip() or None,
                        city=city_label,
                        client=http,
                    )
                except Exception:
                    return {
                        "url": url,
                        "geo_osm_error": "osm_exception",
                        "geo_osm_error_at": datetime.utcnow(),
                    }
                if not og.ok:
                    return {
                        "url": url,
                        "geo_osm_error": "osm_no_result",
                        "geo_osm_error_at": datetime.utcnow(),
                    }
                return {
                    "url": url,
                    "geo_inferred_metro": og.primary_metro,
                    "geo_inferred_area": og.district,
                    "geo_osm_metros": og.metros if og.metros else None,
                    "geo_osm_at": datetime.utcnow(),
                    "geo_osm_error": None,
                    "geo_osm_error_at": None,
                    "geo_llm_at": None,
                }

        done = 0
        step = max(5, concurrency * 2)
        for i in range(0, len(rows), step):
            chunk = rows[i : i + step]
            batch = await asyncio.gather(*[one(r) for r in chunk])
            updates = [u for u in batch if u is not None]
            if updates:
                repo.apply_osm_geo_batch(updates)
            done += len(chunk)
            print(f"  osm geo {min(done, len(rows))}/{len(rows)}", flush=True)

    return len(rows)


async def _run_llm(
    city_slug: str,
    *,
    limit: int,
    concurrency: int,
    force: bool,
) -> int:
    backend = Path(__file__).resolve().parents[1]
    project = backend.parent
    sys.path.insert(0, str(backend))

    from dotenv import load_dotenv

    load_dotenv(project / ".env")

    from app.services.afisha_city_slug import display_city_label_for_slug
    from app.services.llm import LLMClientRegistry
    from app.services.llm_geo_match import build_restaurant_address_for_geo, llm_geo_infer_one
    from app.storage.afisha_catalog_repository import AfishaCatalogRepository
    from app.storage.database import get_session_maker, init_db

    init_db()
    city_label = display_city_label_for_slug(city_slug)
    registry = LLMClientRegistry.from_config()
    llm_client, _sys, node_params = registry.get_default_node()
    repo = AfishaCatalogRepository(get_session_maker())
    rows = repo.list_rows_for_geo_backfill(city_slug, limit=limit, force=force)
    if not rows:
        print("No rows to process (check city_slug, --limit, or already filled geo).")
        return 0

    sem = asyncio.Semaphore(max(1, concurrency))
    cache: Dict[str, tuple] = {}

    async def one(row: Dict[str, Any]) -> Optional[tuple]:
        url = str(row.get("url") or "").strip()
        if not url:
            return None
        cand = {
            "url": url,
            "name": row.get("name"),
            "address": row.get("address"),
        }
        addr = build_restaurant_address_for_geo(cand)
        if not (addr or "").strip():
            return None
        nm = cand.get("name") if isinstance(cand.get("name"), str) else None
        async with sem:
            im, ia = await llm_geo_infer_one(
                llm_client.chat,
                city=city_label,
                restaurant_address=addr,
                restaurant_name=nm,
                node_params=dict(node_params),
                cache=cache,
                isolation_key=url,
            )
        return (url, im, ia)

    step = max(10, concurrency * 4)
    done = 0
    for i in range(0, len(rows), step):
        chunk = rows[i : i + step]
        batch = await asyncio.gather(*[one(r) for r in chunk])
        updates = [u for u in batch if u is not None]
        if updates:
            repo.apply_geo_updates(updates)
        done += len(chunk)
        print(f"  llm geo {min(done, len(rows))}/{len(rows)}", flush=True)

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Geo backfill for afisha_restaurants (OSM by default).")
    parser.add_argument("city_slug", help="Afisha city slug, e.g. spb, msk")
    parser.add_argument("--limit", type=int, default=200, help="Max rows to process (default 200).")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("AFISHA_GEO_BACKFILL_CONCURRENCY", "2")),
        help="Parallel workers (default 2; keep low for OSM etiquette).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even when geo already filled (OSM: ignores geo_osm_at; LLM: overwrites geo_inferred_*).",
    )
    parser.add_argument(
        "--only-errors",
        action="store_true",
        help="OSM mode only: process rows with previous OSM error (geo_osm_error_at is not null).",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Use LLM geo inference instead of OSM (requires LLM env + nodes.yaml).",
    )
    args = parser.parse_args()
    city_slug = args.city_slug.strip().lower()
    if not city_slug:
        parser.error("city_slug required")
    if args.llm:
        n = asyncio.run(
            _run_llm(
                city_slug,
                limit=args.limit,
                concurrency=args.concurrency,
                force=args.force,
            )
        )
        print(f"LLM geo: processed up to {n} row(s) for city_slug={city_slug!r}")
    else:
        n = asyncio.run(
            _run_osm(
                city_slug,
                limit=args.limit,
                concurrency=args.concurrency,
                force=args.force,
                only_errors=args.only_errors,
            )
        )
        print(f"OSM geo: processed up to {n} row(s) for city_slug={city_slug!r}")


if __name__ == "__main__":
    main()
