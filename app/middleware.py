from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from fastapi import FastAPI

logger = logging.getLogger("app.requests")

AsgiMessage = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]


class TraceMiddleware:
    """Pure ASGI trace middleware that does not buffer request/response bodies."""

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
        supplied_trace = headers.get(b"x-trace-id", b"").decode("utf-8", errors="ignore")
        trace_id = supplied_trace or f"trace_{uuid4().hex}"
        scope.setdefault("state", {})["trace_id"] = trace_id
        started = time.perf_counter()
        status_code = 500

        async def send_with_trace(message: AsgiMessage) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-trace-id", trace_id.encode("utf-8")))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
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
