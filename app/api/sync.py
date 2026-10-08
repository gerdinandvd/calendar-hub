"""Sync status for each source."""

from fastapi import APIRouter, Request

from app.api.sources import list_sync_status

router = APIRouter()


@router.get("/api/sync")
def read_sync(request: Request) -> dict:
    return {"sources": list_sync_status(request.app.state.engine)}
