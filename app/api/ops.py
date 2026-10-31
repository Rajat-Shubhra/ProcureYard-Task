from __future__ import annotations

from datetime import UTC
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import Database
from app.models import OpsItem
from app.ops.service import OpsConflictError, OpsNotFoundError, OpsService

router = APIRouter(tags=["ops"])


class ReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=1000)


@router.get("/ops/pending")
def pending_ops(request: Request) -> list[dict[str, object]]:
    database: Database = request.app.state.database
    with database.session() as session:
        items = list(
            session.scalars(
                select(OpsItem)
                .where(OpsItem.status == "pending")
                .order_by(OpsItem.created_at, OpsItem.item_id)
            )
        )
    return [_item_json(item) for item in items]


@router.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request) -> HTMLResponse:
    items = pending_ops(request)
    rows = "".join(
        "<tr>"
        f"<td>{escape(str(item['id']))}</td>"
        f"<td>{escape(str(item['rider_id']))}</td>"
        f"<td>{escape(str(item['type']))}</td>"
        f"<td>{escape(str(item['amount']))}</td>"
        f"<td>{escape(str(item['reason']))}</td>"
        f"<td>{escape(str(item['created_at']))}</td>"
        "</tr>"
        for item in items
    )
    body = (
        "<!doctype html><html><head><title>QuickDrop Ops</title>"
        "<style>body{font:14px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ddd;padding:.6rem;text-align:left}th{background:#f5f5f5}</style>"
        "</head><body><h1>Pending approvals and escalations</h1>"
        "<table><thead><tr><th>ID</th><th>Rider</th><th>Type</th><th>Amount</th>"
        f"<th>Reason</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table>"
        "</body></html>"
    )
    return HTMLResponse(body)


@router.post("/ops/items/{item_id}/approve")
def approve_item(item_id: str, payload: ReviewRequest, request: Request) -> dict[str, object]:
    service: OpsService = request.app.state.ops_service
    try:
        result = service.approve(item_id, payload.reviewer, payload.note)
    except OpsNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Ops item not found") from exc
    except OpsConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": result.item_id,
        "status": result.status,
        "payout_intent_id": result.payout_intent_id,
    }


@router.post("/ops/items/{item_id}/reject")
def reject_item(item_id: str, payload: ReviewRequest, request: Request) -> dict[str, object]:
    service: OpsService = request.app.state.ops_service
    try:
        result = service.reject(item_id, payload.reviewer, payload.note)
    except OpsNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Ops item not found") from exc
    except OpsConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": result.item_id, "status": result.status}


def _item_json(item: OpsItem) -> dict[str, object]:
    created = item.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {
        "id": item.item_id,
        "rider_id": item.rider_id,
        "type": item.item_type,
        "amount": item.amount,
        "reason": item.reason,
        "created_at": created.isoformat(),
    }
