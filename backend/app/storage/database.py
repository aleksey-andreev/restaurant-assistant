import logging
import os
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)


def get_database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://assistant:assistant@localhost:5432/restaurant_assistant",
    )


def get_session_maker() -> Callable:
    engine = create_engine(get_database_url(), future=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _migrate_afisha_restaurants_columns(engine) -> None:
    """Add columns introduced after first deploy (create_all does not ALTER tables)."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "afisha_restaurants" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("afisha_restaurants")}
    parts: list[str] = []
    if "address" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS address TEXT")
    if "metro" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS metro VARCHAR(256)")
    if "tags" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS tags JSONB")
    if "avg_check" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS avg_check JSONB")
    if "flags" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS flags JSONB")
    if "open_now" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS open_now JSONB")
    if "card_extras" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS card_extras JSONB")
    if "venue_closed" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS venue_closed BOOLEAN NOT NULL DEFAULT false")
    if "geo_inferred_metro" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS geo_inferred_metro VARCHAR(256)")
    if "geo_inferred_area" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS geo_inferred_area VARCHAR(256)")
    if "geo_llm_at" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS geo_llm_at TIMESTAMP")
    if "geo_osm_metros" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS geo_osm_metros JSONB")
    if "geo_osm_at" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS geo_osm_at TIMESTAMP")
    if "geo_osm_error" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS geo_osm_error TEXT")
    if "geo_osm_error_at" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS geo_osm_error_at TIMESTAMP")
    if "yandex_rating" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS yandex_rating DOUBLE PRECISION")
    if "yandex_rating_confidence" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS yandex_rating_confidence DOUBLE PRECISION")
    if "yandex_rating_at" not in have:
        parts.append("ADD COLUMN IF NOT EXISTS yandex_rating_at TIMESTAMP")
    if parts:
        ddl = "ALTER TABLE afisha_restaurants " + ", ".join(parts)
        with engine.begin() as conn:
            conn.execute(text(ddl))

    insp2 = inspect(engine)
    if "afisha_restaurants" not in insp2.get_table_names():
        return
    cols = {c["name"] for c in insp2.get_columns("afisha_restaurants")}
    with engine.begin() as conn:
        if "prefetch_ready" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE afisha_restaurants "
                    "ADD COLUMN IF NOT EXISTS prefetch_ready BOOLEAN NOT NULL DEFAULT false"
                )
            )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_afisha_restaurants_geo_osm_metros_gin "
                "ON afisha_restaurants USING GIN (geo_osm_metros)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_afisha_restaurants_prefetch_ready "
                "ON afisha_restaurants (prefetch_ready)"
            )
        )
        conn.execute(
            text(
                "UPDATE afisha_restaurants "
                "SET prefetch_ready = (NOT venue_closed) "
                "AND name IS NOT NULL AND btrim(name) <> '' "
                "AND address IS NOT NULL AND btrim(address) <> '' "
                "AND geo_inferred_area IS NOT NULL AND btrim(geo_inferred_area) <> ''"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS city_districts ("
                "id SERIAL PRIMARY KEY, "
                "city_slug VARCHAR(64) NOT NULL, "
                "district_label VARCHAR(256) NOT NULL, "
                "district_norm VARCHAR(256) NOT NULL, "
                "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
                "CONSTRAINT uq_city_districts_city_norm UNIQUE (city_slug, district_norm)"
                ")"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE city_districts "
                "ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP"
            )
        )
        conn.execute(text("UPDATE city_districts SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
        conn.execute(
            text(
                "INSERT INTO city_districts (city_slug, district_label, district_norm, created_at) VALUES "
                "('spb', 'Адмиралтейский район', 'адмиралтейский район', NOW()), "
                "('spb', 'Василеостровский район', 'василеостровский район', NOW()), "
                "('spb', 'Выборгский район', 'выборгский район', NOW()), "
                "('spb', 'Калининский район', 'калининский район', NOW()), "
                "('spb', 'Кировский район', 'кировский район', NOW()), "
                "('spb', 'Колпинский район', 'колпинский район', NOW()), "
                "('spb', 'Красногвардейский район', 'красногвардейский район', NOW()), "
                "('spb', 'Красносельский район', 'красносельский район', NOW()), "
                "('spb', 'Кронштадтский район', 'кронштадтский район', NOW()), "
                "('spb', 'Курортный район', 'курортный район', NOW()), "
                "('spb', 'Московский район', 'московский район', NOW()), "
                "('spb', 'Невский район', 'невский район', NOW()), "
                "('spb', 'Петроградский район', 'петроградский район', NOW()), "
                "('spb', 'Петродворцовый район', 'петродворцовый район', NOW()), "
                "('spb', 'Приморский район', 'приморский район', NOW()), "
                "('spb', 'Пушкинский район', 'пушкинский район', NOW()), "
                "('spb', 'Фрунзенский район', 'фрунзенский район', NOW()), "
                "('spb', 'Центральный район', 'центральный район', NOW()) "
                "ON CONFLICT (city_slug, district_norm) DO NOTHING"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS city_metro_stations ("
                "id SERIAL PRIMARY KEY, "
                "city_slug VARCHAR(64) NOT NULL, "
                "station_label VARCHAR(256) NOT NULL, "
                "station_norm VARCHAR(256) NOT NULL, "
                "source VARCHAR(64), "
                "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
                "CONSTRAINT uq_city_metro_stations_city_norm UNIQUE (city_slug, station_norm)"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_city_metro_stations_city_slug "
                "ON city_metro_stations (city_slug)"
            )
        )


def _seed_toka_default_binding(engine) -> None:
    """Insert restaurant_name=default from env stubs + Toka login tokens (best-effort)."""
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker as sm_factory

    from .models import TokaRestaurantBinding

    Session = sm_factory(bind=engine)
    sess = Session()
    try:
        exists = sess.scalar(
            select(TokaRestaurantBinding.id).where(TokaRestaurantBinding.restaurant_name == "default").limit(1)
        )
        if exists is not None:
            return
        org = os.environ.get("TOKA_STUB_ORGANIZATION_ID", "").strip()
        store_env = os.environ.get("TOKA_STUB_STORE_ID", "").strip()
        user = os.environ.get("TOKA_USERNAME", "").strip()
        pwd = os.environ.get("TOKA_PASSWORD", "").strip()
        if not org or not store_env:
            logger.warning(
                "Toka default binding not seeded: set TOKA_STUB_ORGANIZATION_ID and TOKA_STUB_STORE_ID"
            )
            return
        if not user or not pwd:
            logger.warning(
                "Toka default binding not seeded: set TOKA_USERNAME and TOKA_PASSWORD"
            )
            return
        try:
            from app.services.toka_client import _toka_base_url, sync_toka_login

            _access, refresh = sync_toka_login(user, pwd, _toka_base_url())
        except Exception as exc:
            logger.warning("Toka default binding seed login failed: %s", exc)
            return
        if not refresh:
            logger.warning("Toka default binding seed: login returned no refresh_token")
            return
        sess.add(
            TokaRestaurantBinding(
                restaurant_name="default",
                org_id=org,
                store_id=store_env,
                refresh_token=str(refresh).strip(),
                token_type="refresh",
            )
        )
        sess.commit()
    except Exception:
        sess.rollback()
        logger.exception("Toka default binding seed failed")
    finally:
        sess.close()


def _migrate_toka_restaurant_bindings_drop_access(engine) -> None:
    """Legacy: drop access_token column; only refresh_token is stored."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "toka_restaurant_bindings" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("toka_restaurant_bindings")}
    if "access_token" not in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE toka_restaurant_bindings DROP COLUMN access_token"))


def _migrate_toka_restaurant_bindings_token_type(engine) -> None:
    """Add token_type column; default row always uses refresh token."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "toka_restaurant_bindings" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("toka_restaurant_bindings")}
    added = False
    with engine.begin() as conn:
        if "token_type" not in cols:
            added = True
            conn.execute(
                text(
                    "ALTER TABLE toka_restaurant_bindings "
                    "ADD COLUMN IF NOT EXISTS token_type VARCHAR(16) NOT NULL DEFAULT 'refresh'"
                )
            )
        if added:
            conn.execute(
                text(
                    "UPDATE toka_restaurant_bindings "
                    "SET token_type = CASE "
                    "WHEN restaurant_name = 'default' THEN 'refresh' "
                    "ELSE 'access' END"
                )
            )
        else:
            conn.execute(
                text(
                    "UPDATE toka_restaurant_bindings "
                    "SET token_type = CASE "
                    "WHEN restaurant_name = 'default' THEN 'refresh' "
                    "WHEN token_type NOT IN ('refresh', 'access') THEN 'access' "
                    "ELSE token_type END"
                )
            )


def init_db() -> None:
    """Create missing tables (dev/small deployments; use migrations in production if needed)."""
    from . import models  # noqa: F401 — register models on Base.metadata

    engine = create_engine(get_database_url(), future=True)
    models.Base.metadata.create_all(bind=engine)
    _migrate_afisha_restaurants_columns(engine)
    _migrate_toka_restaurant_bindings_drop_access(engine)
    _migrate_toka_restaurant_bindings_token_type(engine)
    _seed_toka_default_binding(engine)

