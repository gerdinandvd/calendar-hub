"""MCP adapter mounted in-process on the hub."""

from app.mcp.server import build_mcp_app, mcp

__all__ = ["build_mcp_app", "mcp"]
