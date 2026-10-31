from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import or_, select

from app.config import Settings
from app.db import Database
from app.models import Dispute, OpsItem, PayoutAttempt, PayoutIntent
from app.trace import add_trace_event


@dataclass(frozen=True, slots=True)
class ProviderResult:
    outcome: str
    payload: dict[str, Any] | None
    provider_payout_id: str | None = None
    retryable: bool = False
    ambiguous: bool = False
    error_code: str | None = None


class PaySwiftClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.client = client or httpx.Client(
            base_url=settings.payswift_base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=settings.payswift_connect_timeout_seconds,
                read=settings.payswift_read_timeout_seconds,
                write=settings.payswift_read_timeout_seconds,
                pool=settings.payswift_connect_timeout_seconds,
            ),
        )

    def list_payouts(self, rider_id: str) -> list[dict[str, Any]]:
        response = self.client.get("/v1/payouts", params={"rider_id": rider_id})
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            items = payload.get("data", payload.get("payouts", []))
            return [item for item in items if isinstance(item, dict)]
        return []

    def create_payout(
        self,
        *,
        rider_id: str,
        amount: int,
        reference: str,
        idempotency_key: str,
    ) -> ProviderResult:
        try:
            response = self.client.post(
                "/v1/payouts",
                headers={"Idempotency-Key": idempotency_key},
                json={"rider_id": rider_id, "amount": amount, "reference": reference},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return ProviderResult(
                outcome="transport_ambiguous",
                payload=None,
                retryable=True,
                ambiguous=True,
                error_code=type(exc).__name__,
            )
        payload = _safe_json(response)
        if response.status_code in {200, 201}:
            return ProviderResult(
                outcome="processed",
                payload=payload,
                provider_payout_id=str(payload.get("payout_id")) if payload else None,
            )
        if response.status_code == 400:
            return ProviderResult("validation_error", payload, error_code="http_400")
        if response.status_code == 409:
            if payload and payload.get("error") == "idempotency_key_reused":
                return ProviderResult(
                    "idempotency_conflict",
                    payload,
                    error_code="idempotency_key_reused",
                )
            return ProviderResult(
                "conflict_ambiguous",
                payload,
                retryable=True,
                ambiguous=True,
                error_code="http_409",
            )
        if response.status_code == 429 or response.status_code >= 500:
            return ProviderResult(
                "provider_retryable",
                payload,
                retryable=True,
                ambiguous=response.status_code >= 500,
                error_code=f"http_{response.status_code}",
            )
        return ProviderResult("provider_error", payload, error_code=f"http_{response.status_code}")


class PayoutOutboxWorker:
    def __init__(self, database: Database, settings: Settings, client: PaySwiftClient) -> None:
        self.database = database
        self.settings = settings
        self.client = client

    def tick(self) -> int:
        processed = 0
        for _ in range(self.settings.worker_batch_size):
            intent_id = self._claim_one()
            if intent_id is None:
                break
            self._execute(intent_id)
            processed += 1
        return processed

    def _claim_one(self) -> str | None:
        now = datetime.now(UTC)
        with self.database.session() as session, session.begin():
            statement = (
                select(PayoutIntent)
                .where(
                    PayoutIntent.status.in_({"queued", "retry", "ambiguous"}),
                    PayoutIntent.next_attempt_at <= now,
                    or_(PayoutIntent.lease_until.is_(None), PayoutIntent.lease_until < now),
                )
                .order_by(PayoutIntent.created_at, PayoutIntent.intent_id)
                .limit(1)
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            intent = session.scalar(statement)
            if intent is None:
                return None
            intent.status = "processing"
            intent.lease_until = now + timedelta(seconds=self.settings.worker_job_lease_seconds)
            return intent.intent_id

    def _execute(self, intent_id: str) -> None:
        with self.database.session() as session:
            intent = session.get(PayoutIntent, intent_id)
            if intent is None:
                return
            rider_id = intent.rider_id
            amount = intent.amount
            reference = intent.provider_reference
            idempotency_key = intent.idempotency_key

        reconciled = self._reconcile(rider_id, amount, reference)
        if reconciled is not None:
            self._confirm(intent_id, reconciled, "reconciled_before_submit")
            return

        result = self.client.create_payout(
            rider_id=rider_id,
            amount=amount,
            reference=reference,
            idempotency_key=idempotency_key,
        )
        self._record_attempt(intent_id, result)
        if result.outcome == "processed":
            self._confirm(intent_id, result.provider_payout_id, "provider_processed")
            return
        if result.ambiguous:
            reconciled = self._reconcile(rider_id, amount, reference)
            if reconciled is not None:
                self._confirm(intent_id, reconciled, "reconciled_after_ambiguous_result")
                return
        self._reschedule_or_fail(intent_id, result)

    def _reconcile(self, rider_id: str, amount: int, reference: str) -> str | None:
        try:
            payouts = self.client.list_payouts(rider_id)
        except httpx.HTTPError:
            return None
        for payout in payouts:
            if (
                payout.get("rider_id") == rider_id
                and payout.get("amount") == amount
                and payout.get("reference") == reference
                and payout.get("status") == "processed"
            ):
                payout_id = payout.get("payout_id")
                return str(payout_id) if payout_id else None
        return None

    def _record_attempt(self, intent_id: str, result: ProviderResult) -> None:
        with self.database.session() as session, session.begin():
            intent = session.get(PayoutIntent, intent_id)
            if intent is None:
                return
            intent.attempt_count += 1
            session.add(
                PayoutAttempt(
                    attempt_id=f"attempt_{uuid4().hex}",
                    intent_id=intent_id,
                    attempt_number=intent.attempt_count,
                    request_payload={
                        "rider_id": intent.rider_id,
                        "amount": intent.amount,
                        "reference": intent.provider_reference,
                        "idempotency_key": intent.idempotency_key,
                    },
                    response_payload=result.payload,
                    outcome=result.outcome,
                    error_code=result.error_code,
                )
            )

    def _confirm(self, intent_id: str, provider_payout_id: str | None, reason: str) -> None:
        now = datetime.now(UTC)
        with self.database.session() as session, session.begin():
            intent = session.get(PayoutIntent, intent_id)
            if intent is None:
                return
            intent.status = "confirmed"
            intent.provider_payout_id = provider_payout_id
            intent.confirmed_at = now
            intent.lease_until = None
            intent.last_error = None
            dispute = session.get(Dispute, intent.dispute_id)
            if dispute is not None:
                dispute.state = "paid"
            add_trace_event(
                session,
                rider_id=intent.rider_id,
                dispute_id=intent.dispute_id,
                event_type="tool_call",
                name="payswift_reconciliation",
                input_payload={"intent_id": intent_id},
                output_payload={
                    "status": "confirmed",
                    "provider_payout_id": provider_payout_id,
                    "reason": reason,
                },
            )

    def _reschedule_or_fail(self, intent_id: str, result: ProviderResult) -> None:
        now = datetime.now(UTC)
        with self.database.session() as session, session.begin():
            intent = session.get(PayoutIntent, intent_id)
            if intent is None:
                return
            intent.lease_until = None
            intent.last_error = result.error_code or result.outcome
            if (
                result.retryable
                and intent.attempt_count < self.settings.payswift_max_retry_attempts
            ):
                delay = min(
                    self.settings.payswift_retry_base_seconds
                    * (2 ** max(intent.attempt_count - 1, 0)),
                    self.settings.payswift_retry_max_seconds,
                )
                intent.next_attempt_at = now + timedelta(seconds=delay)
                intent.status = "ambiguous" if result.ambiguous else "retry"
                return
            intent.status = "failed"
            logical_key = f"provider-failure:{intent.intent_id}"
            exists = session.scalar(
                select(OpsItem.item_id).where(OpsItem.logical_key == logical_key)
            )
            if exists is None:
                session.add(
                    OpsItem(
                        item_id=f"ops_{uuid4().hex}",
                        logical_key=logical_key,
                        rider_id=intent.rider_id,
                        dispute_id=intent.dispute_id,
                        decision_id=intent.decision_id,
                        item_type="escalation",
                        amount=None,
                        reason="PaySwift could not be safely confirmed after bounded retries.",
                        status="pending",
                    )
                )


def payout_request_fingerprint(rider_id: str, amount: int, reference: str) -> str:
    encoded = json.dumps(
        {"rider_id": rider_id, "amount": amount, "reference": reference},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else {"value": payload}
