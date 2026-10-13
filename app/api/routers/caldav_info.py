"""CalDAV connection details for the owner. No password and no internal account."""

from fastapi import APIRouter, Request

from app.services.sources import build_caldav_info

router = APIRouter()


@router.get("/api/caldav-info")
def read_caldav_info(request: Request) -> dict:
    return build_caldav_info(request.app.state.engine)
