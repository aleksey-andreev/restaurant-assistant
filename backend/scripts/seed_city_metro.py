#!/usr/bin/env python3
"""Seed ``city_metro_stations`` for an Afisha *city_slug*.

Usage from repo root (with ``DATABASE_URL`` in ``.env``):

  python3 backend/scripts/seed_city_metro.py spb --fetch-wikipedia
  python3 backend/scripts/seed_city_metro.py msk --fetch-wikipedia --replace
  python3 backend/scripts/seed_city_metro.py spb --station "Невский проспект" --replace
  python3 backend/scripts/seed_city_metro.py spb --file data/spb_metro.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List


def _gather_labels(ns: argparse.Namespace) -> List[str]:
    labels: List[str] = []
    labels.extend(ns.station)
    if ns.file:
        text = Path(ns.file).read_text(encoding="utf-8")
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            labels.append(s)
    if ns.fetch_wikipedia:
        slug = ns.city_slug.strip().lower()
        if slug == "spb":
            from app.services.metro_catalog_fetch import fetch_spb_metro_stations_from_wikipedia

            labels.extend(fetch_spb_metro_stations_from_wikipedia())
        elif slug == "msk":
            from app.services.metro_catalog_fetch import fetch_msk_metro_stations_from_wikipedia

            labels.extend(fetch_msk_metro_stations_from_wikipedia())
        else:
            print("--fetch-wikipedia is only implemented for spb and msk", file=sys.stderr)
            sys.exit(2)
    return labels


def main() -> None:
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project / "backend"))

    parser = argparse.ArgumentParser(description="Seed city_metro_stations table")
    parser.add_argument("city_slug", help="Afisha segment, e.g. spb")
    parser.add_argument(
        "--station",
        action="append",
        default=[],
        metavar="NAME",
        help="Station display name (repeatable)",
    )
    parser.add_argument("--file", metavar="PATH", help="UTF-8 text file, one station per line")
    parser.add_argument(
        "--fetch-wikipedia",
        action="store_true",
        help="Fetch metro list from ru.wikipedia (spb or msk category members)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing rows for this city_slug before upsert",
    )
    ns = parser.parse_args()
    slug = str(ns.city_slug or "").strip().lower()
    if not slug:
        print("city_slug required", file=sys.stderr)
        sys.exit(2)

    try:
        from dotenv import load_dotenv

        load_dotenv(project / ".env")
    except ImportError:
        pass

    from app.storage.afisha_catalog_repository import AfishaCatalogRepository
    from app.storage.database import get_session_maker, init_db

    init_db()
    labels = _gather_labels(ns)
    if not labels:
        print("No labels: pass --fetch-wikipedia, --station and/or --file", file=sys.stderr)
        sys.exit(1)

    repo = AfishaCatalogRepository(get_session_maker())
    source = "wikipedia_ru" if ns.fetch_wikipedia else "manual"
    n = repo.seed_city_metro_stations(slug, labels, replace=bool(ns.replace), source=source)
    print(
        f"Upserted {n} distinct station_norm key(s) for city_slug={slug!r}, "
        f"source={source!r}, replace={bool(ns.replace)}."
    )


if __name__ == "__main__":
    main()
