from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.errors import error_response
from app.health import ReadinessProbe

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request) -> JSONResponse:
    probe: ReadinessProbe = request.app.state.readiness_probe
    state = probe.check()
    if state.ready:
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "database": state.database,
                "data": state.data,
                "worker": state.worker,
            },
        )
    return error_response(
        status_code=503,
        code="service_not_ready",
        message="The service is not ready to accept work.",
        retryable=True,
        trace_id=request.state.trace_id,
        extra={
            "database": state.database,
            "data": state.data,
            "worker": state.worker,
            "reason": state.reason,
        },
    )
