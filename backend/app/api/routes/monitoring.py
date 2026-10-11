"""Monitoring endpoints (docs/monitoring.md).

* ``GET /api/health/live``: 200 while the process runs, without touching PostgreSQL or
  Redis; for a hosting platform's liveness check (``GET /api/health`` checks the
  dependencies too, and answers 503 when one is down).
* ``POST /api/client-errors``: the website reports an error a visitor hit. It is logged
  (phone numbers and emails masked), counted, and sent to Sentry when it is on. No login
  is needed (errors can happen before one), so it is rate-limited like a login.

``GET /metrics`` (Prometheus) is on the app itself, outside /api (app/main.py).
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents.guardrails import mask_pii
from app.core.logging import get_logger
from app.core.monitoring import report_client_error
from app.core.rate_limit import login_rate_limit

router = APIRouter(tags=["monitoring"])
logger = get_logger(__name__)


class ClientErrorReport(BaseModel):
    kind: Literal["error", "unhandledrejection", "render"] = "error"
    message: str = Field(min_length=1, max_length=500)
    page: str = Field(default="", max_length=300, description="The page's path")
    stack: str = Field(default="", max_length=4000)


@router.get("/health/live", summary="The process is running (no dependency checks)")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/client-errors", status_code=202, summary="An error the website hit")
async def client_error(
    report: ClientErrorReport, _: Annotated[None, Depends(login_rate_limit)]
) -> dict[str, bool]:
    message = mask_pii(report.message)
    page = mask_pii(report.page.split("?", 1)[0])  # the path only, never the query
    logger.warning(
        "client_error",
        kind=report.kind,
        message=message,
        page=page,
        stack=mask_pii(report.stack)[:1500],
    )
    report_client_error(f"Website {report.kind} on {page or '/'}: {message}")
    return {"received": True}
