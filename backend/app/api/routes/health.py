from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.config.settings import Settings, get_settings
from app.models.schemas import HealthResponse
from app.services.health import collect_health

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Check the API and its dependencies",
    responses={503: {"model": HealthResponse, "description": "A dependency is down."}},
)
async def health(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    """Return 200 when Postgres and Redis both answer, 503 otherwise."""
    result = await collect_health(settings)
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
