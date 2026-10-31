from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import Database
from app.models import Decision, Dispute, Evidence, Message, OpsItem, TraceEvent
from app.ops.service import OpsConflictError, OpsNotFoundError, OpsResult, OpsService
from app.trace import redact_trace_payload

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
    approvals = [item for item in items if item["type"] == "approval"]
    escalations = [item for item in items if item["type"] != "approval"]
    body = (
        "<!doctype html><html><head><title>QuickDrop Ops</title>"
        f"<style>{_CSS}</style></head><body>"
        "<h1>Ops review</h1>"
        f"<h2>Approvals waiting ({len(approvals)})</h2>{_table(approvals)}"
        f"<h2>Escalations waiting ({len(escalations)})</h2>{_table(escalations)}"
        "</body></html>"
    )
    return HTMLResponse(body)


@router.get("/ops/items/{item_id}", response_class=HTMLResponse)
def ops_item_page(item_id: str, request: Request) -> HTMLResponse:
    """Conversation, evidence, trace and review controls for one pending/closed ops item."""
    database: Database = request.app.state.database
    with database.session() as session:
        item = session.get(OpsItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Ops item not found")
        dispute = session.get(Dispute, item.dispute_id)
        decision = session.get(Decision, item.decision_id)
        evidence_payload: dict[str, object] = {}
        if decision is not None:
            for evidence_id in decision.evidence_ids or []:
                evidence = session.get(Evidence, evidence_id)
                if evidence is not None:
                    evidence_payload = evidence.payload
                    break
        messages = []
        if dispute is not None:
            messages = list(
                session.scalars(
                    select(Message)
                    .where(Message.conversation_id == dispute.conversation_id)
                    .order_by(Message.turn_number)
                )
            )
        events = list(
            session.scalars(
                select(TraceEvent)
                .where(TraceEvent.rider_id == item.rider_id)
                .order_by(TraceEvent.id)
                .limit(200)
            )
        )
        header = _item_json(item)
        header.update(
            {
                "status": item.status,
                "reviewer": item.reviewer,
                "reviewer_note": item.reviewer_note,
                "dispute_state": dispute.state if dispute else None,
                "dispute_type": dispute.dispute_type if dispute else None,
                "service_date_ist": (
                    dispute.service_date_ist.isoformat()
                    if dispute and dispute.service_date_ist
                    else None
                ),
                "decision": decision.decision_class if decision else None,
                "reason_codes": decision.reason_codes if decision else None,
            }
        )
        conversation_rows = "".join(
            "<tr>"
            f"<td>{message.turn_number}</td>"
            f"<td>{escape(message.received_at.isoformat())}</td>"
            f"<td>{escape(message.text)}</td>"
            f"<td>{escape(message.reply or '')}</td>"
            "</tr>"
            for message in messages
        )
        trace_rows = "".join(
            "<tr>"
            f"<td>{escape(event.created_at.isoformat())}</td>"
            f"<td>{escape(event.event_type)}</td>"
            f"<td>{escape(event.name)}</td>"
            f"<td><pre>{_trace_cell(event.input_payload, event.rider_id)}</pre></td>"
            f"<td><pre>{_trace_cell(event.output_payload, event.rider_id)}</pre></td>"
            "</tr>"
            for event in events
        )
    actions = ""
    if item.status == "pending":
        retry = (
            _button(item_id, "retry-payout", "Retry payout")
            if item.logical_key.startswith("provider-failure:")
            else ""
        )
        approve = _button(item_id, "approve", "Approve") if item.item_type == "approval" else ""
        actions = (
            "<h2>Review</h2>"
            "<label>Reviewer <input id='reviewer' value='ops-user'></label> "
            "<label>Note <input id='note' size='60'></label><p>"
            f"{approve}{_button(item_id, 'reject', 'Reject')}{retry}</p>"
            "<pre id='result'></pre>"
            f"<script>{_SCRIPT}</script>"
        )
    body = (
        "<!doctype html><html><head><title>QuickDrop Ops — item</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<p><a href='/ops'>&larr; all pending items</a></p>"
        f"<h1>{escape(item.item_type.title())} {escape(item_id)}</h1>"
        f"<pre>{escape(json.dumps(header, indent=2, default=str))}</pre>"
        f"{actions}"
        "<h2>Conversation</h2><table><thead><tr><th>Turn</th><th>Received</th>"
        f"<th>Rider</th><th>Agent reply</th></tr></thead><tbody>{conversation_rows}</tbody></table>"
        "<h2>Evidence used for this decision</h2>"
        f"<pre>{escape(json.dumps(evidence_payload, indent=2, default=str))}</pre>"
        "<h2>What the agent did (trace)</h2><table><thead><tr><th>At</th><th>Type</th>"
        f"<th>Name</th><th>Input</th><th>Output</th></tr></thead><tbody>{trace_rows}</tbody></table>"
        "</body></html>"
    )
    return HTMLResponse(body)


_CSS = (
    "body{font:14px system-ui;margin:2rem;max-width:1200px}table{border-collapse:collapse;"
    "width:100%}th,td{border:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}"
    "th{background:#f5f5f5}pre{white-space:pre-wrap;word-break:break-word;margin:0}"
    "button{margin-right:.5rem;padding:.4rem .9rem}"
)

_SCRIPT = (
    "async function review(id, action){"
    "const body={reviewer:document.getElementById('reviewer').value,"
    "note:document.getElementById('note').value||null};"
    "const r=await fetch(`/ops/items/${id}/${action}`,{method:'POST',"
    "headers:{'content-type':'application/json'},body:JSON.stringify(body)});"
    "document.getElementById('result').textContent=r.status+' '+await r.text();"
    "if(r.ok){setTimeout(()=>location.reload(),800);}}"
)


def _trace_cell(payload: object, rider_id: str) -> str:
    redacted = redact_trace_payload(payload, rider_id=rider_id)
    return escape(json.dumps(redacted, default=str)[:600])


def _button(item_id: str, action: str, label: str) -> str:
    return f"<button onclick=\"review('{escape(item_id)}','{action}')\">{label}</button>"


def _table(items: list[dict[str, object]]) -> str:
    if not items:
        return "<p>None.</p>"
    rows = "".join(
        "<tr>"
        f"<td><a href='/ops/items/{escape(str(item['id']))}'>{escape(str(item['id']))}</a></td>"
        f"<td>{escape(str(item['rider_id']))}</td>"
        f"<td>{escape(str(item['amount']))}</td>"
        f"<td>{escape(str(item['reason']))}</td>"
        f"<td>{escape(str(item['created_at']))}</td>"
        "</tr>"
        for item in items
    )
    return (
        "<table><thead><tr><th>ID</th><th>Rider</th><th>Amount</th><th>Reason</th>"
        f"<th>Created</th></tr></thead><tbody>{rows}</tbody></table>"
    )


@router.post("/ops/items/{item_id}/approve")
def approve_item(item_id: str, payload: ReviewRequest, request: Request) -> dict[str, object]:
    service: OpsService = request.app.state.ops_service
    result = _review(lambda: service.approve(item_id, payload.reviewer, payload.note))
    return {
        "id": result.item_id,
        "status": result.status,
        "payout_intent_id": result.payout_intent_id,
    }


@router.post("/ops/items/{item_id}/reject")
def reject_item(item_id: str, payload: ReviewRequest, request: Request) -> dict[str, object]:
    service: OpsService = request.app.state.ops_service
    result = _review(lambda: service.reject(item_id, payload.reviewer, payload.note))
    return {"id": result.item_id, "status": result.status}


@router.post("/ops/items/{item_id}/retry-payout")
def retry_payout(item_id: str, payload: ReviewRequest, request: Request) -> dict[str, object]:
    service: OpsService = request.app.state.ops_service
    result = _review(lambda: service.retry_payout(item_id, payload.reviewer, payload.note))
    return {
        "id": result.item_id,
        "status": result.status,
        "payout_intent_id": result.payout_intent_id,
    }


def _review(action: Callable[[], OpsResult]) -> OpsResult:
    """Run an ops mutation, mapping service errors to the frozen 404/409 envelopes."""

    try:
        return action()
    except OpsNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Ops item not found") from exc
    except OpsConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
