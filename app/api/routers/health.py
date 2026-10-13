"""HTTP health endpoint."""

from fastapi import APIRouter, Request

from app.health import report

router = APIRouter()


@router.get("/api/health")
def health_endpoint(request: Request) -> dict[str, object]:
    return report(request.app.state.engine, request.app.state.radicale)
