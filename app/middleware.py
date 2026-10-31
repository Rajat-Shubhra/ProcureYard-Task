from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from fastapi import FastAPI

from app.errors import TRACE_HEADER, unexpected_error_response

logger = logging.getLogger("app.requests")

AsgiMessage = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]

_TRACE_HEADER_BYTES = TRACE_HEADER.encode("ascii")
# A caller-supplied trace ID is honoured only when it is short and header/log safe.
_SUPPLIED_TRACE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def resolve_trace_id(supplied: str | None) -> str:
    if supplied and _SUPPLIED_TRACE_PATTERN.match(supplied):
        return supplied
    return f"trace_{uuid4().hex}"


class TraceMiddleware:
    """Pure ASGI trace middleware that does not buffer request/response bodies.

    Every HTTP response, including the envelope for an unexpected failure, is sent through
    ``send_with_trace`` so the ``x-trace-id`` header is always present.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied_trace = headers.get(_TRACE_HEADER_BYTES, b"").decode("utf-8", errors="ignore")
        trace_id = resolve_trace_id(supplied_trace)
        scope.setdefault("state", {})["trace_id"] = trace_id
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_trace(message: AsgiMessage) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                response_headers = list(message.get("headers", []))
                if not any(key.lower() == _TRACE_HEADER_BYTES for key, _ in response_headers):
                    response_headers.append((_TRACE_HEADER_BYTES, trace_id.encode("utf-8")))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        except Exception as exc:
            logger.exception(
                "unhandled_request_error",
                extra={
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                },
            )
            if response_started:
                # The status line is already on the wire; nothing safe can be sent anymore.
                raise
            response = unexpected_error_response(trace_id)
            await response(scope, receive, send_with_trace)
        finally:
            logger.info(
                "request_complete",
                extra={
                    "trace_id": trace_id,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )


def install_request_middleware(app: FastAPI) -> None:
    app.add_middleware(TraceMiddleware)
