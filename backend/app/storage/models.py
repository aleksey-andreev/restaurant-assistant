from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GraphState(Base):
    __tablename__ = "graph_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    current_node: Mapped[str] = mapped_column(String(128), default="root")
    history: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    context: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AfishaRestaurant(Base):
    """
    Afisha restaurant catalog per city_slug: URLs from sitemap plus optional enriched fields
    from card HTML (sync --enrich) for filtering without hitting Afisha on every dialog turn.
    """

    __tablename__ = "afisha_restaurants"
    __table_args__ = (UniqueConstraint("url", name="uq_afisha_restaurants_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(768), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metro: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    tags: Mapped[Optional[List[Any]]] = mapped_column(JSON, nullable=True)
    avg_check: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # Service flags (parking, banquets, …) as object {key: bool|null}; legacy list [] in DB.
    flags: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    open_now: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # Snapshot from card JSON-LD / extra labels (not metro); avoids live Afisha HTML during dialog.
    card_extras: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    venue_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    geo_inferred_metro: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    geo_inferred_area: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    geo_llm_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # OSM-derived metro candidates (Nominatim + Overpass); primary also in geo_inferred_metro.
    geo_osm_metros: Mapped[Optional[List[Any]]] = mapped_column(JSON, nullable=True)
    geo_osm_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    geo_osm_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    geo_osm_error_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Yandex Search SERP rating snapshot (filled by sync --enrich); used for formal score in dialog.
    yandex_rating: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    yandex_rating_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    yandex_rating_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    prefetch_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class CityDistrict(Base):
    __tablename__ = "city_districts"
    __table_args__ = (UniqueConstraint("city_slug", "district_norm", name="uq_city_districts_city_norm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    district_label: Mapped[str] = mapped_column(String(256), nullable=False)
    district_norm: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class TokaRestaurantBinding(Base):
    """
    Toka Backoffice credentials per restaurant key (normalized name) or explicit default row.

    Row with restaurant_name \"default\" is the fallback when no name/org match exists.
    ``refresh_token`` stores either refresh-token or access-token depending on ``token_type``.
    """

    __tablename__ = "toka_restaurant_bindings"
    __table_args__ = (UniqueConstraint("restaurant_name", name="uq_toka_restaurant_bindings_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_type: Mapped[str] = mapped_column(String(16), nullable=False, default="refresh")


class PipelineEvent(Base):
    """
    Append-only log of graph pipeline stages for analytics (one row per stage event).
    Rows from the same user message / graph invocation share batch_id.
    """

    __tablename__ = "pipeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        index=True,
    )
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str] = mapped_column(String(128), index=True)
    body: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

