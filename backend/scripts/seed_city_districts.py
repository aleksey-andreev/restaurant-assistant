#!/usr/bin/env python3
"""Load canonical administrative district labels into ``city_districts`` for an Afisha *city_slug*.

Usage from repo root (with ``DATABASE_URL`` in ``.env``):

  python3 backend/scripts/seed_city_districts.py msk --district \"Тверской район\" \"Хамовники\"
  python3 backend/scripts/seed_city_districts.py voronezh --file data/vrn_districts.txt
  python3 backend/scripts/seed_city_districts.py msk --json-file data/msk.json --replace

Text file: UTF-8, one label per line; ``#`` starts a comment. JSON: array of strings, or objects
with ``label``, ``name`` or ``district``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List


def _gather_labels(ns: argparse.Namespace) -> List[str]:
    labels: List[str] = []
    labels.extend(ns.district)
    if ns.file:
        text = Path(ns.file).read_text(encoding="utf-8")
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            labels.append(s)
    if ns.json_file:
        data = json.loads(Path(ns.json_file).read_text(encoding="utf-8"))
        if isinstance(data, list):
            for it in data:
                if isinstance(it, str) and it.strip():
                    labels.append(it.strip())
                elif isinstance(it, dict):
                    v = it.get("label") or it.get("name") or it.get("district")
                    if isinstance(v, str) and v.strip():
                        labels.append(v.strip())
    return labels


def main() -> None:
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project / "backend"))

    parser = argparse.ArgumentParser(description="Seed city_districts table")
    parser.add_argument("city_slug", help="Afisha segment, e.g. msk, spb, voronezh")
    parser.add_argument(
        "--district",
        action="append",
        default=[],
        metavar="LABEL",
        help="District label as stored in geo_inferred_area (repeatable)",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="Text file: one district label per line",
    )
    parser.add_argument(
        "--json-file",
        metavar="PATH",
        help="JSON list of strings or {label|name|district: ...}",
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

    labels = _gather_labels(ns)
    if not labels:
        print("No labels: pass --district, --file and/or --json-file", file=sys.stderr)
        sys.exit(1)

    try:
        from dotenv import load_dotenv

        load_dotenv(project / ".env")
    except ImportError:
        pass

    from app.storage.afisha_catalog_repository import AfishaCatalogRepository
    from app.storage.database import get_session_maker

    repo = AfishaCatalogRepository(get_session_maker())
    n = repo.seed_city_districts(slug, labels, replace=bool(ns.replace))
    print(f"Upserted {n} distinct district_norm key(s) for city_slug={slug!r}, replace={bool(ns.replace)}.")


if __name__ == "__main__":
    main()
