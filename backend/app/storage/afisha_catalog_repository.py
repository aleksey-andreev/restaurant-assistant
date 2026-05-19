from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, case, cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import AfishaRestaurant, CityDistrict, CityMetroStation


def norm_city_district_key(label: str) -> str:
    """Lowercase single-spaced key; must match geo_gate token normalization for districts."""
    return re.sub(r"\s+", " ", (label or "").strip().lower())


def norm_metro_station_key(label: str) -> str:
    """Normalized key for metro lookup (ё→е, lower, strip м./метро prefixes)."""
    from app.services.osm_geo import _normalize_metro_station_name

    display = _normalize_metro_station_name(label) or (label or "").strip()
    t = re.sub(r"\s+", " ", display).strip().lower()
    return t.replace("ё", "е")


def _prefetch_ready_expr() -> Any:
    return case(
        (
            and_(
                AfishaRestaurant.venue_closed.is_(False),
                AfishaRestaurant.name.isnot(None),
                AfishaRestaurant.address.isnot(None),
                AfishaRestaurant.geo_inferred_area.isnot(None),
            ),
            True,
        ),
        else_=False,
    )


def _flags_as_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return dict(v)
    return {}


def catalog_entry_to_candidate(row: Dict[str, Any]) -> Dict[str, Any]:
    """Shape stored catalog row like ``parse_afisha_restaurant_card`` output for downstream nodes."""
    tags = row.get("tags")
    if not isinstance(tags, list):
        tags = []
    flags = _flags_as_dict(row.get("flags"))
    open_now = row.get("open_now") if isinstance(row.get("open_now"), dict) else None
    card_extras = row.get("card_extras") if isinstance(row.get("card_extras"), dict) else None
    out: Dict[str, Any] = {
        "url": row["url"],
        "name": row.get("name"),
        "address": row.get("address"),
        "metro": None,
        "tags": tags,
        "avg_check": row.get("avg_check"),
        "flags": flags,
        "venue_closed": bool(row.get("venue_closed")),
        "open_now": open_now,
        "card_extras": card_extras,
        "debug": {"from_catalog": True},
    }
    if row.get("geo_inferred_metro") is not None or row.get("geo_inferred_area") is not None:
        out["geo_inferred_metro"] = row.get("geo_inferred_metro")
        out["geo_inferred_area"] = row.get("geo_inferred_area")
    gom = row.get("geo_osm_metros")
    if isinstance(gom, list) and gom:
        out["geo_osm_metros"] = [str(x) for x in gom if isinstance(x, str) and x.strip()]
    yr = row.get("yandex_rating")
    if yr is not None:
        try:
            out["yandex_rating"] = float(yr)
        except (TypeError, ValueError):
            pass
        else:
            yc = row.get("yandex_rating_confidence")
            if yc is not None:
                try:
                    out["yandex_rating_confidence"] = float(yc)
                except (TypeError, ValueError):
                    out["yandex_rating_confidence"] = None
    return out


def _catalog_row_has_prefetch(row: Dict[str, Any]) -> bool:
    if row.get("venue_closed"):
        return False
    name = row.get("name")
    address = row.get("address")
    area = row.get("geo_inferred_area")
    return bool(
        isinstance(name, str)
        and name.strip()
        and isinstance(address, str)
        and address.strip()
        and isinstance(area, str)
        and area.strip()
    )


class AfishaCatalogRepository:
    def __init__(self, session_maker: sessionmaker):
        self._session_maker = session_maker

    @staticmethod
    def _escape_ilike_pattern(s: str) -> str:
        """
        Escape %, _ and backslash for SQL LIKE patterns.
        We use it together with `ilike(..., escape='\\')`.
        """
        # Order matters: escape backslash first.
        return (
            (s or "")
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

    def find_rows_for_city_by_name_like(
        self,
        city_slug: str,
        restaurant_name: str,
        *,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        """
        Fallback search for "specific restaurant" flow.

        Matches `AfishaRestaurant.name` in the given `city_slug` via case-insensitive substring
        (ILIKE %name%). Returns up to *limit* rows ordered by URL.
        """
        city = str(city_slug or "").strip()
        name = str(restaurant_name or "").strip()
        if not city or not name:
            return []

        lim = max(1, int(limit))
        escaped = self._escape_ilike_pattern(name.lower())
        pattern = f"%{escaped}%"

        with self._session_maker() as db:  # type: Session
            orows = (
                db.query(AfishaRestaurant)
                .filter(AfishaRestaurant.city_slug == city)
                .filter(AfishaRestaurant.venue_closed.is_(False))
                .filter(AfishaRestaurant.name.isnot(None))
                # For "specific restaurant" fallback we require address.
                .filter(AfishaRestaurant.address.isnot(None))
                .filter(func.btrim(AfishaRestaurant.address) != "")
                .filter(AfishaRestaurant.name.ilike(pattern, escape="\\"))
                .order_by(AfishaRestaurant.url.asc())
                .limit(lim)
                .all()
            )

        out: List[Dict[str, Any]] = []
        for r in orows:
            out.append(
                {
                    "url": r.url,
                    "name": r.name,
                    "address": r.address,
                    "metro": None,
                    "tags": r.tags,
                    "avg_check": r.avg_check,
                    "flags": r.flags,
                    "venue_closed": r.venue_closed,
                    "open_now": r.open_now,
                    "card_extras": r.card_extras,
                    "geo_inferred_metro": r.geo_inferred_metro,
                    "geo_inferred_area": r.geo_inferred_area,
                    "geo_osm_metros": r.geo_osm_metros,
                    "geo_llm_at": r.geo_llm_at.isoformat() if r.geo_llm_at else None,
                    "geo_osm_at": r.geo_osm_at.isoformat() if r.geo_osm_at else None,
                    "yandex_rating": r.yandex_rating,
                    "yandex_rating_confidence": r.yandex_rating_confidence,
                    "yandex_rating_at": r.yandex_rating_at.isoformat() if r.yandex_rating_at else None,
                }
            )
        return out

    def count_for_city(self, city_slug: str) -> int:
        with self._session_maker() as db:  # type: Session
            return (
                db.query(func.count(AfishaRestaurant.id))
                .filter(AfishaRestaurant.city_slug == city_slug)
                .scalar()
                or 0
            )

    def list_urls_for_city(self, city_slug: str, *, limit: int) -> List[str]:
        rows = self.list_catalog_rows_for_city(city_slug, limit=limit)
        return [r["url"] for r in rows]

    def list_rows_for_metro_tag(
        self, city_slug: str, *, metro_tag: str, limit: int
    ) -> List[Dict[str, Any]]:
        """
        Search restaurants by exact metro tag stored in ``geo_osm_metros`` JSONB.
        """
        tag = str(metro_tag or "").strip()
        if not tag:
            return []
        lim = max(1, int(limit))
        with self._session_maker() as db:  # type: Session
            orows = (
                db.query(AfishaRestaurant)
                .filter(AfishaRestaurant.city_slug == city_slug)
                .filter(cast(AfishaRestaurant.geo_osm_metros, JSONB).contains([tag]))
                .order_by(AfishaRestaurant.url.asc())
                .limit(lim)
                .all()
            )
        return [
            {
                "url": r.url,
                "name": r.name,
                "address": r.address,
                "geo_osm_metros": r.geo_osm_metros,
                "geo_inferred_metro": r.geo_inferred_metro,
                "geo_inferred_area": r.geo_inferred_area,
            }
            for r in orows
        ]

    def list_catalog_rows_for_city(self, city_slug: str, *, limit: int) -> List[Dict[str, Any]]:
        lim = max(1, int(limit))
        with self._session_maker() as db:  # type: Session
            orows = (
                db.query(AfishaRestaurant)
                .filter(AfishaRestaurant.city_slug == city_slug)
                .order_by(AfishaRestaurant.url.asc())
                .limit(lim)
                .all()
            )
        out: List[Dict[str, Any]] = []
        for r in orows:
            out.append(
                {
                    "url": r.url,
                    "name": r.name,
                    "address": r.address,
                    "metro": None,
                    "tags": r.tags,
                    "avg_check": r.avg_check,
                    "flags": r.flags,
                    "venue_closed": r.venue_closed,
                    "open_now": r.open_now,
                    "card_extras": r.card_extras,
                    "geo_inferred_metro": r.geo_inferred_metro,
                    "geo_inferred_area": r.geo_inferred_area,
                    "geo_llm_at": r.geo_llm_at.isoformat() if r.geo_llm_at else None,
                    "geo_osm_metros": r.geo_osm_metros,
                    "geo_osm_at": r.geo_osm_at.isoformat() if r.geo_osm_at else None,
                    "yandex_rating": r.yandex_rating,
                    "yandex_rating_confidence": r.yandex_rating_confidence,
                    "yandex_rating_at": r.yandex_rating_at.isoformat() if r.yandex_rating_at else None,
                    "prefetch_ready": bool(r.prefetch_ready),
                }
            )
        return out

    def list_prefetch_ready_rows_for_city(self, city_slug: str, *, limit: int) -> List[Dict[str, Any]]:
        lim = max(1, int(limit))
        with self._session_maker() as db:  # type: Session
            orows = (
                db.query(AfishaRestaurant)
                .filter(AfishaRestaurant.city_slug == city_slug)
                .filter(AfishaRestaurant.prefetch_ready.is_(True))
                .filter(AfishaRestaurant.venue_closed.is_(False))
                .order_by(AfishaRestaurant.url.asc())
                .limit(lim)
                .all()
            )
        return [
            {
                "url": r.url,
                "name": r.name,
                "address": r.address,
                "metro": None,
                "tags": r.tags,
                "avg_check": r.avg_check,
                "flags": r.flags,
                "venue_closed": r.venue_closed,
                "open_now": r.open_now,
                "card_extras": r.card_extras,
                "geo_inferred_metro": r.geo_inferred_metro,
                "geo_inferred_area": r.geo_inferred_area,
                "geo_llm_at": r.geo_llm_at.isoformat() if r.geo_llm_at else None,
                "geo_osm_metros": r.geo_osm_metros,
                "geo_osm_at": r.geo_osm_at.isoformat() if r.geo_osm_at else None,
                "yandex_rating": r.yandex_rating,
                "yandex_rating_confidence": r.yandex_rating_confidence,
                "yandex_rating_at": r.yandex_rating_at.isoformat() if r.yandex_rating_at else None,
                "prefetch_ready": bool(r.prefetch_ready),
            }
            for r in orows
        ]

    def list_rows_for_geo_backfill(
        self, city_slug: str, *, limit: int, force: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Rows suitable for offline LLM geo inference: need some address signal, and unless *force*,
        both geo_inferred_* are still NULL.
        """
        lim = max(1, int(limit))
        with self._session_maker() as db:  # type: Session
            q = (
                db.query(AfishaRestaurant)
                .filter(AfishaRestaurant.city_slug == city_slug)
                .filter(
                    or_(
                        AfishaRestaurant.address.isnot(None),
                        AfishaRestaurant.name.isnot(None),
                    )
                )
            )
            if not force:
                q = q.filter(
                    and_(
                        AfishaRestaurant.geo_inferred_metro.is_(None),
                        AfishaRestaurant.geo_inferred_area.is_(None),
                    )
                )
            orows = q.order_by(AfishaRestaurant.url.asc()).limit(lim).all()
        rows: List[Dict[str, Any]] = []
        for r in orows:
            rows.append(
                {
                    "url": r.url,
                    "name": r.name,
                    "address": r.address,
                }
            )
        return rows

    def list_rows_for_osm_geo_backfill(
        self, city_slug: str, *, limit: int, force: bool = False, only_errors: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Rows for OSM geo (Nominatim + Overpass): need address or name; unless *force*,
        ``geo_osm_at`` is NULL (not yet resolved from OSM).
        """
        lim = max(1, int(limit))
        with self._session_maker() as db:  # type: Session
            q = (
                db.query(AfishaRestaurant)
                .filter(AfishaRestaurant.city_slug == city_slug)
                .filter(
                    or_(
                        AfishaRestaurant.address.isnot(None),
                        AfishaRestaurant.name.isnot(None),
                    )
                )
            )
            if only_errors:
                q = q.filter(AfishaRestaurant.geo_osm_error_at.isnot(None))
            elif not force:
                q = q.filter(AfishaRestaurant.geo_osm_at.is_(None))
            orows = q.order_by(AfishaRestaurant.url.asc()).limit(lim).all()
        rows: List[Dict[str, Any]] = []
        for r in orows:
            rows.append(
                {
                    "url": r.url,
                    "name": r.name,
                    "address": r.address,
                }
            )
        return rows

    def apply_osm_geo_batch(self, updates: List[Dict[str, Any]]) -> None:
        """Persist OSM-derived geo fields per row dict (must include ``url``)."""
        if not updates:
            return
        with self._session_maker() as db:  # type: Session
            for u in updates:
                url = str(u.get("url") or "").strip()
                if not url:
                    continue
                db.query(AfishaRestaurant).filter(AfishaRestaurant.url == url).update(
                    {
                        "geo_inferred_metro": u.get("geo_inferred_metro"),
                        "geo_inferred_area": u.get("geo_inferred_area"),
                        "geo_osm_metros": u.get("geo_osm_metros"),
                        "geo_osm_at": u.get("geo_osm_at"),
                        "geo_osm_error": u.get("geo_osm_error"),
                        "geo_osm_error_at": u.get("geo_osm_error_at"),
                        "geo_llm_at": u.get("geo_llm_at"),
                    },
                    synchronize_session=False,
                )
                db.query(AfishaRestaurant).filter(AfishaRestaurant.url == url).update(
                    {"prefetch_ready": _prefetch_ready_expr()},
                    synchronize_session=False,
                )
            db.commit()

    def apply_geo_updates(self, updates: List[Tuple[str, Optional[str], Optional[str]]]) -> None:
        """Set geo_inferred_metro, geo_inferred_area, geo_llm_at for each (url, metro, area)."""
        if not updates:
            return
        now = datetime.utcnow()
        with self._session_maker() as db:  # type: Session
            for url, im, ia in updates:
                db.query(AfishaRestaurant).filter(AfishaRestaurant.url == url).update(
                    {
                        "geo_inferred_metro": im,
                        "geo_inferred_area": ia,
                        "geo_llm_at": now,
                    },
                    synchronize_session=False,
                )
                db.query(AfishaRestaurant).filter(AfishaRestaurant.url == url).update(
                    {"prefetch_ready": _prefetch_ready_expr()},
                    synchronize_session=False,
                )
            db.commit()

    def upsert_url_index(self, city_slug: str, urls: Iterable[str]) -> int:
        """
        Upsert URLs for a city without wiping enriched columns (conflict: only city_slug + last_seen_at).
        """
        urls_list = list(dict.fromkeys(urls))
        now = datetime.utcnow()
        with self._session_maker() as db:  # type: Session
            for i in range(0, len(urls_list), 500):
                chunk = urls_list[i : i + 500]
                rows = [
                    {
                        "city_slug": city_slug,
                        "url": u,
                        "name": None,
                        "address": None,
                        "metro": None,
                        "tags": None,
                        "avg_check": None,
                        "flags": None,
                        "open_now": None,
                        "card_extras": None,
                        "yandex_rating": None,
                        "yandex_rating_confidence": None,
                        "yandex_rating_at": None,
                        "geo_inferred_metro": None,
                        "geo_inferred_area": None,
                        "geo_llm_at": None,
                        "geo_osm_metros": None,
                        "geo_osm_at": None,
                        "geo_osm_error": None,
                        "geo_osm_error_at": None,
                        "venue_closed": False,
                        "prefetch_ready": False,
                        "last_seen_at": now,
                    }
                    for u in chunk
                ]
                stmt = pg_insert(AfishaRestaurant).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[AfishaRestaurant.__table__.c.url],
                    set_={
                        "city_slug": stmt.excluded.city_slug,
                        "last_seen_at": stmt.excluded.last_seen_at,
                    },
                )
                db.execute(stmt)
            db.commit()
        return len(urls_list)

    def upsert_enriched_rows(self, rows: List[Dict[str, Any]]) -> int:
        """
        Upsert rows with full metadata (from card HTML). *rows* dict keys: city_slug, url, name, address,
        tags (list|None), avg_check (dict|None), flags (dict|None), open_now (dict|None),
        card_extras (dict|None), yandex_rating (float|None), yandex_rating_confidence (float|None),
        yandex_rating_at (datetime|None), venue_closed (bool).
        Card ``metro`` is not persisted (unreliable on Afisha); column is cleared on upsert.
        """
        if not rows:
            return 0
        now = datetime.utcnow()
        with self._session_maker() as db:  # type: Session
            for i in range(0, len(rows), 300):
                chunk = rows[i : i + 300]
                db_rows = []
                for r in chunk:
                    db_rows.append(
                        {
                            "city_slug": str(r["city_slug"]),
                            "url": str(r["url"]),
                            "name": r.get("name"),
                            "address": r.get("address"),
                            "metro": None,
                            "tags": r.get("tags"),
                            "avg_check": r.get("avg_check"),
                            "flags": r.get("flags"),
                            "open_now": r.get("open_now"),
                            "card_extras": r.get("card_extras"),
                            "yandex_rating": r.get("yandex_rating"),
                            "yandex_rating_confidence": r.get("yandex_rating_confidence"),
                            "yandex_rating_at": r.get("yandex_rating_at"),
                            "geo_inferred_metro": r.get("geo_inferred_metro"),
                            "geo_inferred_area": r.get("geo_inferred_area"),
                            "geo_llm_at": r.get("geo_llm_at"),
                            "geo_osm_metros": r.get("geo_osm_metros"),
                            "geo_osm_at": r.get("geo_osm_at"),
                            "geo_osm_error": r.get("geo_osm_error"),
                            "geo_osm_error_at": r.get("geo_osm_error_at"),
                            "venue_closed": bool(r.get("venue_closed")),
                            "prefetch_ready": _catalog_row_has_prefetch(r),
                            "last_seen_at": r.get("last_seen_at") or now,
                        }
                    )
                stmt = pg_insert(AfishaRestaurant).values(db_rows)
                t = AfishaRestaurant.__table__.c
                stmt = stmt.on_conflict_do_update(
                    index_elements=[AfishaRestaurant.__table__.c.url],
                    set_={
                        "city_slug": stmt.excluded.city_slug,
                        "name": stmt.excluded.name,
                        "address": stmt.excluded.address,
                        "metro": stmt.excluded.metro,
                        "tags": stmt.excluded.tags,
                        "avg_check": stmt.excluded.avg_check,
                        "flags": stmt.excluded.flags,
                        "open_now": stmt.excluded.open_now,
                        "card_extras": stmt.excluded.card_extras,
                        "yandex_rating": func.coalesce(
                            stmt.excluded.yandex_rating,
                            t.yandex_rating,
                        ),
                        "yandex_rating_confidence": func.coalesce(
                            stmt.excluded.yandex_rating_confidence,
                            t.yandex_rating_confidence,
                        ),
                        "yandex_rating_at": func.coalesce(
                            stmt.excluded.yandex_rating_at,
                            t.yandex_rating_at,
                        ),
                        "geo_inferred_metro": func.coalesce(stmt.excluded.geo_inferred_metro, t.geo_inferred_metro),
                        "geo_inferred_area": func.coalesce(stmt.excluded.geo_inferred_area, t.geo_inferred_area),
                        "geo_osm_metros": func.coalesce(stmt.excluded.geo_osm_metros, t.geo_osm_metros),
                        "geo_osm_at": func.coalesce(stmt.excluded.geo_osm_at, t.geo_osm_at),
                        "geo_osm_error": case(
                            (stmt.excluded.geo_osm_at.isnot(None), None),
                            else_=func.coalesce(stmt.excluded.geo_osm_error, t.geo_osm_error),
                        ),
                        "geo_osm_error_at": case(
                            (stmt.excluded.geo_osm_at.isnot(None), None),
                            else_=func.coalesce(stmt.excluded.geo_osm_error_at, t.geo_osm_error_at),
                        ),
                        "geo_llm_at": case(
                            (stmt.excluded.geo_osm_at.isnot(None), None),
                            else_=func.coalesce(stmt.excluded.geo_llm_at, t.geo_llm_at),
                        ),
                        "venue_closed": stmt.excluded.venue_closed,
                        "prefetch_ready": case(
                            (
                                and_(
                                    stmt.excluded.venue_closed.is_(False),
                                    func.coalesce(stmt.excluded.name, t.name).isnot(None),
                                    func.coalesce(stmt.excluded.address, t.address).isnot(None),
                                    func.coalesce(stmt.excluded.geo_inferred_area, t.geo_inferred_area).isnot(None),
                                ),
                                True,
                            ),
                            else_=False,
                        ),
                        "last_seen_at": stmt.excluded.last_seen_at,
                    },
                )
                db.execute(stmt)
            db.commit()
        return len(rows)

    def upsert_city_catalog(self, city_slug: str, urls: Iterable[str]) -> int:
        """Backward-compatible alias: URL-only index upsert."""
        return self.upsert_url_index(city_slug, urls)

    def list_city_districts(self, city_slug: str) -> List[Dict[str, str]]:
        with self._session_maker() as db:  # type: Session
            rows = (
                db.query(CityDistrict)
                .filter(CityDistrict.city_slug == city_slug)
                .order_by(CityDistrict.district_label.asc())
                .all()
            )
        return [
            {"district_label": str(r.district_label), "district_norm": str(r.district_norm)}
            for r in rows
        ]

    def list_city_metro_stations(self, city_slug: str) -> List[Dict[str, str]]:
        with self._session_maker() as db:  # type: Session
            rows = (
                db.query(CityMetroStation)
                .filter(CityMetroStation.city_slug == str(city_slug or "").strip().lower())
                .order_by(CityMetroStation.station_label.asc())
                .all()
            )
        return [
            {"station_label": str(r.station_label), "station_norm": str(r.station_norm)}
            for r in rows
        ]

    def list_distinct_metro_names(self, city_slug: str, *, limit: int = 500) -> List[str]:
        """
        Metro station labels for a city: ``city_metro_stations`` if seeded, else catalog OSM tags.
        """
        slug = str(city_slug or "").strip().lower()
        if not slug:
            return []
        seeded = self.list_city_metro_stations(slug)
        if seeded:
            return [r["station_label"] for r in seeded][: max(1, min(int(limit), 2000))]

        from sqlalchemy import text

        lim = max(1, min(int(limit), 2000))
        sql = text(
            """
            SELECT DISTINCT btrim(elem) AS name
            FROM afisha_restaurants,
                 jsonb_array_elements_text(geo_osm_metros) AS elem
            WHERE city_slug = :slug
              AND geo_osm_metros IS NOT NULL
              AND btrim(elem) <> ''
            ORDER BY name
            LIMIT :lim
            """
        )
        with self._session_maker() as db:  # type: Session
            rows = db.execute(sql, {"slug": slug, "lim": lim}).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]

    def seed_city_metro_stations(
        self,
        city_slug: str,
        station_labels: Iterable[str],
        *,
        replace: bool = False,
        source: str = "manual",
    ) -> int:
        slug = str(city_slug or "").strip().lower()
        if not slug:
            return 0
        uniq: Dict[str, str] = {}
        for raw in station_labels:
            if not isinstance(raw, str):
                continue
            label = raw.strip()
            if not label or label.startswith("#"):
                continue
            nk = norm_metro_station_key(label)
            if not nk:
                continue
            uniq.setdefault(nk, label)
        if not uniq:
            return 0
        now = datetime.utcnow()
        src = str(source or "manual").strip()[:64] or "manual"
        with self._session_maker() as db:  # type: Session
            if replace:
                db.query(CityMetroStation).filter(CityMetroStation.city_slug == slug).delete(
                    synchronize_session=False
                )
            ck = [
                {
                    "city_slug": slug,
                    "station_label": lbl,
                    "station_norm": nk,
                    "source": src,
                    "created_at": now,
                }
                for nk, lbl in uniq.items()
            ]
            step = 200
            for i in range(0, len(ck), step):
                chunk = ck[i : i + step]
                stmt = pg_insert(CityMetroStation).values(chunk)
                stmt = stmt.on_conflict_do_nothing(constraint="uq_city_metro_stations_city_norm")
                db.execute(stmt)
            db.commit()
        return len(uniq)

    def seed_city_districts(
        self,
        city_slug: str,
        district_labels: Iterable[str],
        *,
        replace: bool = False,
    ) -> int:
        slug = str(city_slug or "").strip().lower()
        if not slug:
            return 0
        uniq: Dict[str, str] = {}
        for raw in district_labels:
            if not isinstance(raw, str):
                continue
            label = raw.strip()
            if not label or label.startswith("#"):
                continue
            nk = norm_city_district_key(label)
            if not nk:
                continue
            uniq.setdefault(nk, label)
        if not uniq:
            return 0
        now = datetime.utcnow()
        with self._session_maker() as db:  # type: Session
            if replace:
                db.query(CityDistrict).filter(CityDistrict.city_slug == slug).delete(synchronize_session=False)
            ck = [{"city_slug": slug, "district_label": lbl, "district_norm": nk, "created_at": now} for nk, lbl in uniq.items()]
            step = 200
            for i in range(0, len(ck), step):
                chunk = ck[i : i + step]
                stmt = pg_insert(CityDistrict).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[CityDistrict.city_slug, CityDistrict.district_norm],
                    set_={"district_label": stmt.excluded.district_label},
                )
                db.execute(stmt)
            db.commit()
        return len(uniq)


def candidate_dict_to_enriched_row(city_slug: str, cand: Dict[str, Any]) -> Dict[str, Any]:
    """Map parser output to DB upsert row."""
    tags = cand.get("tags")
    if tags is not None and not isinstance(tags, list):
        tags = list(tags) if isinstance(tags, (list, tuple)) else None
    flags_out: Optional[Dict[str, Any]] = None
    fr = cand.get("flags")
    if isinstance(fr, dict):
        flags_out = {str(k): fr[k] for k in fr if isinstance(k, str)}
    avg = cand.get("avg_check")
    if avg is not None and not isinstance(avg, dict):
        avg = None
    open_now = cand.get("open_now") if isinstance(cand.get("open_now"), dict) else None
    card_extras = cand.get("card_extras") if isinstance(cand.get("card_extras"), dict) else None
    return {
        "city_slug": city_slug,
        "url": str(cand.get("url") or ""),
        "name": cand.get("name") if isinstance(cand.get("name"), str) else None,
        "address": cand.get("address") if isinstance(cand.get("address"), str) else None,
        "metro": None,
        "tags": tags,
        "avg_check": avg,
        "flags": flags_out,
        "open_now": open_now,
        "card_extras": card_extras,
        "venue_closed": bool(cand.get("venue_closed")),
    }


