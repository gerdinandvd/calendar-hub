"""FastAPI process. Opens the database on startup and closes it on shutdown."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.access import install_access
from app.api.caldav_info import router as caldav_router
from app.api.health import router as health_router
from app.api.sources import router as sources_router
from app.db.models import Base
from app.db.session import make_engine
from app.log import configure_logging
from app.radicale.client import RadicaleClient
from app.ui.pages import router as pages_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = make_engine()
    app.state.engine = engine
    app.state.radicale = RadicaleClient.from_env()
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        Base.metadata.create_all(engine)
        yield
    finally:
        engine.dispose()


def create_app() -> FastAPI:
    configure_logging()
    application = FastAPI(lifespan=lifespan)
    install_access(application)
    application.include_router(health_router)
    application.include_router(sources_router)
    application.include_router(caldav_router)
    application.include_router(pages_router)
    return application


app = create_app()
