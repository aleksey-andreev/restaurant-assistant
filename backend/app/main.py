from contextlib import asynccontextmanager

from fastapi import FastAPI

from .routers import dialog
from .routers import restaurants
from .storage.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Restaurant Assistant API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(dialog.router, prefix="/api")
    app.include_router(restaurants.router, prefix="/api")

    return app


app = create_app()

