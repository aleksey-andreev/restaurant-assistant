import os
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def get_database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://assistant:assistant@localhost:5432/restaurant_assistant",
    )


def get_session_maker() -> Callable:
    engine = create_engine(get_database_url(), future=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """Create missing tables (dev/small deployments; use migrations in production if needed)."""
    from . import models  # noqa: F401 — register models on Base.metadata

    engine = create_engine(get_database_url(), future=True)
    models.Base.metadata.create_all(bind=engine)

