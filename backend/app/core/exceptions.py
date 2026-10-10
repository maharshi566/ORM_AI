"""Application errors and the handlers that turn them into one JSON shape.

Every error response looks like::

    {"error": {"code": "not_found", "message": "...", "request_id": "...", "details": null}}
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for errors the app raises on purpose."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ForbiddenError(AppError):
    """The record exists but belongs to someone else (for example another shop)."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class ConflictError(AppError):
    """The request is valid but does not fit the record's state (e.g. already decided)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class InvalidRequestError(AppError):
    """The request is well-formed JSON but asks for something that cannot be done."""

    status_code = 422
    code = "invalid_request"


class DependencyUnavailableError(AppError):
    """A database, cache, vector store, LLM or external API is down."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "dependency_unavailable"


_HTTP_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "request_too_large",
    429: "rate_limited",
}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id(request),
            "details": details,
        }
    }
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("app_error", code=exc.code, status=exc.status_code, error=exc.message)
    response = error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )
    retry_after = (exc.details or {}).get("retry_after_seconds")
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    if exc.status_code == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


TIMEOUT_MESSAGE = "This took too long and was stopped. Please try again in a minute."
DATABASE_MESSAGE = "The database is not reachable right now. Is PostgreSQL running?"
INTERNAL_MESSAGE = "Something went wrong on our side."


def _database_down_types() -> tuple[type[BaseException], ...]:
    from sqlalchemy.exc import InterfaceError, OperationalError

    # Only "cannot reach the database". A broken rule (IntegrityError) or a bad query
    # (ProgrammingError) is a bug or a race, not an outage, and stays a 500.
    return (OperationalError, InterfaceError, ConnectionRefusedError)


def describe_error(exc: BaseException) -> tuple[int, str, str]:
    """(HTTP status, code, safe message) for an error.

    The handlers below use it, and so does the chat stream, which reports an error as
    an event instead of a response. The message never includes internal details.
    """
    from app.services.chat_model import LLMError

    if isinstance(exc, AppError):
        return exc.status_code, exc.code, exc.message
    if isinstance(exc, TimeoutError):
        return status.HTTP_504_GATEWAY_TIMEOUT, "timeout", TIMEOUT_MESSAGE
    if isinstance(exc, _database_down_types()):
        return status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", DATABASE_MESSAGE
    if isinstance(exc, LLMError):
        message = getattr(exc, "message", None) or "The language model is not available."
        return status.HTTP_503_SERVICE_UNAVAILABLE, "model_unavailable", message
    return status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", INTERNAL_MESSAGE


async def timeout_error_handler(request: Request, exc: TimeoutError) -> JSONResponse:
    logger.warning("request_timeout", error_type=type(exc).__name__)
    code, name, message = describe_error(exc)
    return error_response(request, status_code=code, code=name, message=message)


async def database_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("database_unavailable", error_type=type(exc).__name__)
    code, name, message = describe_error(exc)
    return error_response(request, status_code=code, code=name, message=message)


async def model_error_handler(request: Request, exc: Exception) -> JSONResponse:
    code, name, message = describe_error(exc)
    logger.warning("model_unavailable", error=message)
    return error_response(request, status_code=code, code=name, message=message)


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return error_response(
        request,
        status_code=exc.status_code,
        code=_HTTP_STATUS_CODES.get(exc.status_code, "http_error"),
        message=str(exc.detail),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        message="The request is not valid.",
        details=exc.errors(),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error", error_type=type(exc).__name__)
    return error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        message=INTERNAL_MESSAGE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    from app.services.chat_model import LLMError

    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(TimeoutError, timeout_error_handler)  # type: ignore[arg-type]
    for down in _database_down_types():
        app.add_exception_handler(down, database_error_handler)
    app.add_exception_handler(LLMError, model_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
