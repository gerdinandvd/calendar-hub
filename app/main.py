"""FastAPI process. Opens the database on startup and closes it on shutdown."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.access import install_access
from app.api.routers.agent import router as agent_router
from app.api.routers.caldav_info import router as caldav_router
from app.api.routers.health import router as health_router
from app.api.routers.sources import router as sources_router
from app.api.routers.sync import router as sync_router
from app.api.routers.webhooks import router as webhooks_router
from app.db.models import Base
from app.db.session import make_engine
from app.health import Heartbeat
from app.log import configure_logging
from app.mcp.auth import AgentBearerMiddleware
from app.mcp.runtime import bind as bind_mcp
from app.mcp.server import build_mcp_app
from app.mcp.server import mcp as mcp_server
from app.radicale.client import RadicaleClient
from app.services.agent import ConfirmStore
from app.services.agent.query import ensure_mapping_uid_index
from app.services.rate_limit import RateLimiter
from app.services.sources.sync_ops import (
    ensure_sync_states,
    migrate_ics_collection_paths,
)
from app.sync.runner import arm
from app.sync.scheduler import Scheduler
from app.ui.pages import router as pages_router


class _McpMount:
    """ASGI placeholder replaced during lifespan once the DB is open."""

    def __init__(self) -> None:
        self.app: Any | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self.app is None:
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {"error": "not_ready", "detail": "MCP is starting."},
                status_code=503,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


_mcp_mount = _McpMount()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = make_engine()
    app.state.engine = engine
    app.state.radicale = RadicaleClient.from_env()
    heartbeat = None
    scheduler = None
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        Base.metadata.create_all(engine)
        ensure_mapping_uid_index(engine)
        ensure_sync_states(engine)
        migrate_ics_collection_paths(engine, app.state.radicale)
        app.state.webhook_rates = RateLimiter()
        app.state.agent_rates = RateLimiter()
        app.state.agent_confirms = ConfirmStore()
        bind_mcp(engine, app.state.radicale, app.state.agent_confirms)
        mcp_asgi = build_mcp_app()
        _mcp_mount.app = AgentBearerMiddleware(
            mcp_asgi, engine=engine, rates=app.state.agent_rates
        )
        arm()
        heartbeat = Heartbeat()
        app.state.heartbeat = heartbeat
        heartbeat.start()
        scheduler = Scheduler(engine, app.state.radicale)
        app.state.scheduler = scheduler
        scheduler.start()
        async with mcp_server.session_manager.run():
            yield
    finally:
        _mcp_mount.app = None
        if heartbeat is not None:
            await heartbeat.shutdown()
        if scheduler is not None:
            await scheduler.shutdown()
        engine.dispose()


def create_app() -> FastAPI:
    configure_logging()
    application = FastAPI(lifespan=lifespan)
    install_access(application)
    application.include_router(health_router)
    application.include_router(sources_router)
    application.include_router(sync_router)
    application.include_router(webhooks_router)
    application.include_router(caldav_router)
    application.include_router(agent_router)
    application.include_router(pages_router)
    application.mount("/mcp", _mcp_mount)
    return application


app = create_app()
