"""CalDAV connection details for the owner. No password and no internal account."""

import os

from fastapi import APIRouter, Request
from sqlalchemy.engine import Engine

from app.api.sources import list_sources

router = APIRouter()


def build_caldav_info(engine: Engine) -> dict:
    collections = []
    for item in list_sources(engine):
        collections.append(
            {
                "name": item["name"],
                "url": item["collection_url"],
                "color": item["color"],
                "color_name": item["color_name"],
            }
        )
    return {
        "host": os.environ.get("HUB_HOST", "").strip(),
        "username": os.environ.get("HUB_USER", "").strip(),
        "collections": collections,
    }


@router.get("/api/caldav-info")
def read_caldav_info(request: Request) -> dict:
    return build_caldav_info(request.app.state.engine)
