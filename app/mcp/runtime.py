"""Process-wide handles for MCP tools (set from app lifespan)."""

from __future__ import annotations

from typing import Any


class McpRuntime:
    engine: Any = None
    radicale: Any = None
    confirms: Any = None


RUNTIME = McpRuntime()


def bind(engine: Any, radicale: Any, confirms: Any) -> None:
    RUNTIME.engine = engine
    RUNTIME.radicale = radicale
    RUNTIME.confirms = confirms


def require() -> tuple[Any, Any, Any]:
    if RUNTIME.engine is None or RUNTIME.radicale is None or RUNTIME.confirms is None:
        raise RuntimeError("MCP runtime is not ready.")
    return RUNTIME.engine, RUNTIME.radicale, RUNTIME.confirms
