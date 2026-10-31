from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

TRACE_HEADER = "x-trace-id"

# Frozen envelope codes for ``HTTPException`` statuses (docs/API_CONTRACT.md, "Common error
# envelope"). Anything else that is a client error is reported as ``invalid_request``.
HTTP_STATUS_CODES: dict[int, str] = {
    404: "not_found",
    405: "invalid_request",
    409: "conflict",
    422: "validation_error",
}
DEFAULT_HTTP_CODE = "invalid_request"

UNEXPECTED_ERROR_MESSAGE = "The service could not complete the request."
DATABASE_UNAVAILABLE_MESSAGE = "The service cannot reach its database; retry shortly."
PROCESSING_INCOMPLETE_MESSAGE = (
    "Processing did not complete; the message will be resumed on the next delivery."
)
INVALID_JSON_MESSAGE = "The request body is not valid JSON."
VALIDATION_ERROR_MESSAGE = "The request did not match the required shape."

# Pydantic/FastAPI error types that mean the body could not be decoded at all.
_JSON_DECODE_ERROR_TYPES = frozenset({"json_invalid", "json_type"})


class RetryableProcessingError(RuntimeError):
    """Raised after a message was durably received but processing failed unexpectedly.

    The HTTP layer maps it to a retryable 503 so the vendor resends and the next delivery
    resumes the same message instead of leaving it stuck in ``processing``.
    """


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "trace_unknown")


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    trace_id: str,
    extra: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build the common error envelope.

    The trace header is set here as well as by ``TraceMiddleware`` so that an envelope
    produced outside the middleware (for example by Starlette's server-error layer) still
    carries the correlation ID required by the contract.
    """

    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "trace_id": trace_id,
        }
    }
    if extra:
        body.update(extra)
    response_headers = {**(headers or {}), TRACE_HEADER: trace_id}
    return JSONResponse(status_code=status_code, content=body, headers=response_headers)


def unexpected_error_response(trace_id: str) -> JSONResponse:
    """Envelope for an unexpected failure: retryable 503 (docs/API_CONTRACT.md:84)."""

    return error_response(
        status_code=503,
        code="internal_error",
        message=UNEXPECTED_ERROR_MESSAGE,
        retryable=True,
        trace_id=trace_id,
    )


def is_invalid_json(exc: RequestValidationError) -> bool:
    """True when the request body itself could not be decoded, not merely a field."""

    for error in exc.errors():
        error_type = error.get("type")
        location = tuple(error.get("loc", ()))
        if error_type in _JSON_DECODE_ERROR_TYPES:
            return True
        if error_type == "missing" and location == ("body",):
            # FastAPI reports an empty body as a missing body: there was nothing to decode.
            return True
    return False


def _is_database_unavailable(exc: DBAPIError) -> bool:
    return isinstance(exc, OperationalError | InterfaceError) or exc.connection_invalidated


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        if is_invalid_json(exc):
            return error_response(
                status_code=400,
                code="invalid_json",
                message=INVALID_JSON_MESSAGE,
                retryable=False,
                trace_id=_trace_id(request),
            )
        return error_response(
            status_code=422,
            code="validation_error",
            message=VALIDATION_ERROR_MESSAGE,
            retryable=False,
            trace_id=_trace_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        server_side = exc.status_code >= 500
        code = (
            "internal_error"
            if server_side
            else HTTP_STATUS_CODES.get(exc.status_code, DEFAULT_HTTP_CODE)
        )
        return error_response(
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            retryable=server_side,
            trace_id=_trace_id(request),
            headers=exc.headers,
        )

    @app.exception_handler(DBAPIError)
    async def database_error(request: Request, exc: DBAPIError) -> JSONResponse:
        trace_id = _trace_id(request)
        unavailable = _is_database_unavailable(exc)
        logger.exception(
            "database_error",
            extra={
                "trace_id": trace_id,
                "error_type": type(exc).__name__,
                "database_unavailable": unavailable,
            },
        )
        if unavailable:
            return error_response(
                status_code=503,
                code="database_unavailable",
                message=DATABASE_UNAVAILABLE_MESSAGE,
                retryable=True,
                trace_id=trace_id,
            )
        return unexpected_error_response(trace_id)

    @app.exception_handler(RetryableProcessingError)
    async def processing_incomplete(
        request: Request, exc: RetryableProcessingError
    ) -> JSONResponse:
        trace_id = _trace_id(request)
        logger.exception(
            "message_processing_incomplete",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
        )
        return error_response(
            status_code=503,
            code="internal_error",
            message=PROCESSING_INCOMPLETE_MESSAGE,
            retryable=True,
            trace_id=trace_id,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Fallback only: ``TraceMiddleware`` normally converts unexpected failures itself so
        # the response goes through the trace-aware ``send``. Starlette invokes this handler
        # when something escapes that layer (for example after the response already started).
        logger.exception(
            "unhandled_request_error",
            extra={"trace_id": _trace_id(request), "error_type": type(exc).__name__},
        )
        return unexpected_error_response(_trace_id(request))
