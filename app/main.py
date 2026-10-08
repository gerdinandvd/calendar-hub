"""FastAPI process. Opens the database on startup and closes it on shutdown."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.access import install_access
from app.api.health import router as health_router
from app.db.session import make_engine
from app.log import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = make_engine()
    app.state.engine = engine
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        yield
    finally:
        engine.dispose()


def create_app() -> FastAPI:
    configure_logging()
    application = FastAPI(lifespan=lifespan)
    install_access(application)
    application.include_router(health_router)
    return application


app = create_app()
