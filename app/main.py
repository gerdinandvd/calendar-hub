"""FastAPI-proces. Opent de database bij start en sluit die bij stop."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.db.session import make_engine


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
    application = FastAPI(lifespan=lifespan)
    application.include_router(health_router)
    return application


app = create_app()
