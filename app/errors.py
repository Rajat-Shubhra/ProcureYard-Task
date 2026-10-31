from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


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
) -> JSONResponse:
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
    return JSONResponse(status_code=status_code, content=body)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError) -> JSONResponse:
        return error_response(
            status_code=422,
            code="validation_error",
            message="The request did not match the required shape.",
            retryable=False,
            trace_id=_trace_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "invalid_request"
        return error_response(
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            retryable=False,
            trace_id=_trace_id(request),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_request_error",
            extra={"trace_id": _trace_id(request), "error_type": type(exc).__name__},
        )
        return error_response(
            status_code=500,
            code="internal_error",
            message="The service could not complete the request.",
            retryable=True,
            trace_id=_trace_id(request),
        )
