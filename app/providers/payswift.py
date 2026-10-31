from __future__ import annotations

import hashlib
import json
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Database
from app.models import Dispute, OpsItem, PayoutAttempt, PayoutIntent
from app.trace import add_trace_event

logger = logging.getLogger(__name__)

# The sandbox blocks *every* payout POST for 60 s once more than 30 requests arrive inside a
# 10 s window (project-analysis/REPOSITORY_ANALYSIS.md). Settings has no dedicated field, so the
# cooldown floor lives here; a longer provider ``Retry-After`` is honoured over it.
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
RATE_LIMITED_ERROR_CODE = "http_429"
# Statuses the outbox may claim. ``processing`` is only claimable once its lease has expired,
# which means the previous holder crashed or was killed mid-flight.
CLAIMABLE_STATUSES = ("queued", "retry", "ambiguous", "processing")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    outcome: str
    payload: dict[str, Any] | None
    provider_payout_id: str | None = None
    retryable: bool = False
    ambiguous: bool = False
    error_code: str | None = None
    retry_after_seconds: float | None = None


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
        if response.status_code == 429:
            return ProviderResult(
                "rate_limited",
                payload,
                retryable=True,
                error_code=RATE_LIMITED_ERROR_CODE,
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code >= 500:
            return ProviderResult(
                "provider_retryable",
                payload,
                retryable=True,
                ambiguous=True,
                error_code=f"http_{response.status_code}",
                retry_after_seconds=_retry_after_seconds(response),
            )
        return ProviderResult("provider_error", payload, error_code=f"http_{response.status_code}")


class PayoutOutboxWorker:
    def __init__(self, database: Database, settings: Settings, client: PaySwiftClient) -> None:
        self.database = database
        self.settings = settings
        self.client = client
        # Injection points for tests: a clock so lease/backoff/cooldown expiry can be simulated
        # without sleeping, and the jitter source.
        self.clock: Callable[[], datetime] = _utcnow
        self.rng = random.Random()
        self._blocked_until: datetime | None = None

    @property
    def blocked_until(self) -> datetime | None:
        """End of the process-wide pause triggered by the last 429, if still active."""

        return self._blocked_until if self._in_cooldown() else None

    def tick(self, *, should_stop: Callable[[], bool] | None = None) -> int:
        processed = 0
        for _ in range(self.settings.worker_batch_size):
            if should_stop is not None and should_stop():
                break
            if self._in_cooldown():
                break
            intent_id = self._claim_one()
            if intent_id is None:
                break
            try:
                self._execute(intent_id)
            except Exception as exc:
                # Never leave a claimed row in ``processing``: route it through the normal
                # ambiguous path so the next tick reconciles against the ledger before any POST.
                logger.exception(
                    "payout_execute_failed",
                    extra={"intent_id": intent_id, "error_type": type(exc).__name__},
                )
                self._recover_from_worker_error(intent_id, exc)
            processed += 1
        return processed

    def requeue(self, session: Session, intent_id: str, *, reviewer: str) -> bool:
        """Operator retry: reset a ``failed`` intent so the next tick picks it up again."""

        return requeue_failed_intent(session, intent_id, reviewer=reviewer, now=self.clock())

    def _in_cooldown(self) -> bool:
        if self._blocked_until is None:
            return False
        if self.clock() >= self._blocked_until:
            self._blocked_until = None
            return False
        return True

    def _recover_from_worker_error(self, intent_id: str, exc: Exception) -> None:
        result = ProviderResult(
            "worker_error",
            None,
            retryable=True,
            ambiguous=True,
            error_code=type(exc).__name__,
        )
        try:
            self._reschedule_or_fail(intent_id, result)
        except Exception:
            # The lease still expires, so ``_claim_one`` reclaims the row on a later tick.
            logger.exception("payout_reschedule_failed", extra={"intent_id": intent_id})

    def _claim_one(self) -> str | None:
        now = self.clock()
        with self.database.session() as session, session.begin():
            statement = (
                select(PayoutIntent)
                .where(
                    PayoutIntent.status.in_(CLAIMABLE_STATUSES),
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
            if intent.status == "processing":
                logger.warning(
                    "payout_lease_reclaimed",
                    extra={
                        "intent_id": intent.intent_id,
                        "lease_until": intent.lease_until.isoformat()
                        if intent.lease_until
                        else None,
                    },
                )
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

        attempt_id = self._begin_attempt(intent_id)
        result = self.client.create_payout(
            rider_id=rider_id,
            amount=amount,
            reference=reference,
            idempotency_key=idempotency_key,
        )
        self._finish_attempt(attempt_id, result)
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
        except (httpx.HTTPError, ValueError):
            # ``ValueError`` covers a non-JSON 200 body (``JSONDecodeError``); an unreadable
            # ledger must not strand the intent, the same-key POST is itself a safe replay.
            logger.warning("payswift_ledger_unavailable", extra={"rider_id": rider_id})
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

    def _begin_attempt(self, intent_id: str) -> str:
        """Commit the audit row *before* the network call.

        A crash between the POST and the response then still leaves an ``outcome="sent"``
        attempt behind. ``attempt_number`` continues the per-intent sequence across operator
        requeues (which reset ``attempt_count``) so ``(intent_id, attempt_number)`` stays unique.
        """

        with self.database.session() as session, session.begin():
            intent = session.get(PayoutIntent, intent_id)
            if intent is None:
                raise LookupError(intent_id)
            last_number = session.scalar(
                select(func.max(PayoutAttempt.attempt_number)).where(
                    PayoutAttempt.intent_id == intent_id
                )
            )
            intent.attempt_count += 1
            attempt = PayoutAttempt(
                attempt_id=f"attempt_{uuid4().hex}",
                intent_id=intent_id,
                attempt_number=(last_number or 0) + 1,
                request_payload={
                    "rider_id": intent.rider_id,
                    "amount": intent.amount,
                    "reference": intent.provider_reference,
                    "idempotency_key": intent.idempotency_key,
                },
                response_payload=None,
                outcome="sent",
                error_code=None,
            )
            session.add(attempt)
            return attempt.attempt_id

    def _finish_attempt(self, attempt_id: str, result: ProviderResult) -> None:
        with self.database.session() as session, session.begin():
            attempt = session.get(PayoutAttempt, attempt_id)
            if attempt is None:
                return
            attempt.outcome = result.outcome
            attempt.response_payload = result.payload
            attempt.error_code = result.error_code

    def _confirm(self, intent_id: str, provider_payout_id: str | None, reason: str) -> None:
        now = self.clock()
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
        now = self.clock()
        with self.database.session() as session, session.begin():
            intent = session.get(PayoutIntent, intent_id)
            if intent is None:
                return
            intent.lease_until = None
            intent.last_error = result.error_code or result.outcome

            if result.error_code == RATE_LIMITED_ERROR_CODE:
                # A 429 is the provider's cooldown signal, not a failed attempt: give the budget
                # back, park this intent for the cooldown and pause the whole outbox, because the
                # sandbox block is global and hammering other intents would only extend it.
                cooldown = max(result.retry_after_seconds or 0.0, RATE_LIMIT_COOLDOWN_SECONDS)
                intent.attempt_count = max(intent.attempt_count - 1, 0)
                intent.next_attempt_at = now + timedelta(seconds=cooldown)
                intent.status = "retry"
                self._blocked_until = now + timedelta(seconds=cooldown)
                self._trace_failure(session, intent, result, terminal=False)
                logger.warning(
                    "payswift_rate_limited",
                    extra={"intent_id": intent_id, "cooldown_seconds": cooldown},
                )
                return

            if (
                result.retryable
                and intent.attempt_count < self.settings.payswift_max_retry_attempts
            ):
                delay = self._backoff_seconds(intent.attempt_count, result.retry_after_seconds)
                intent.next_attempt_at = now + timedelta(seconds=delay)
                intent.status = "ambiguous" if result.ambiguous else "retry"
                self._trace_failure(session, intent, result, terminal=False)
                return

            intent.status = "failed"
            self._trace_failure(session, intent, result, terminal=True)
            dispute = session.get(Dispute, intent.dispute_id)
            if dispute is not None:
                dispute.state = "escalated"
            self._escalate_provider_failure(session, intent, result)

    def _backoff_seconds(self, attempt_count: int, retry_after: float | None) -> float:
        ceiling = min(
            self.settings.payswift_retry_base_seconds * (2 ** max(attempt_count - 1, 0)),
            self.settings.payswift_retry_max_seconds,
        )
        # Half-jitter: spreads retries across [ceiling/2, ceiling] without exceeding the cap.
        delay = self.rng.uniform(0.5 * ceiling, ceiling)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay

    def _trace_failure(
        self,
        session: Session,
        intent: PayoutIntent,
        result: ProviderResult,
        *,
        terminal: bool,
    ) -> None:
        add_trace_event(
            session,
            rider_id=intent.rider_id,
            dispute_id=intent.dispute_id,
            event_type="error",
            name="payswift_failure",
            input_payload={"intent_id": intent.intent_id, "attempt": intent.attempt_count},
            output_payload={
                "error_code": result.error_code,
                "outcome": result.outcome,
                "terminal": terminal,
                "status": intent.status,
                "next_attempt_at": None if terminal else intent.next_attempt_at.isoformat(),
            },
        )

    def _escalate_provider_failure(
        self, session: Session, intent: PayoutIntent, result: ProviderResult
    ) -> None:
        base_key = f"provider-failure:{intent.intent_id}"
        existing = list(
            session.scalars(
                select(OpsItem).where(
                    or_(
                        OpsItem.logical_key == base_key,
                        OpsItem.logical_key.startswith(f"{base_key}:", autoescape=True),
                    )
                )
            )
        )
        if any(item.status == "pending" for item in existing):
            return
        # First failure uses the bare key; a failure after an operator requeue (whose escalation
        # was already resolved) gets a fresh pending item with a numbered key.
        logical_key = base_key if not existing else f"{base_key}:{len(existing) + 1}"
        attempts = intent.attempt_count
        reason = (
            f"PaySwift {result.error_code or result.outcome} after {attempts} attempt(s); "
            f"intent {intent.intent_id} retained for operator retry"
        )
        item = OpsItem(
            item_id=f"ops_{uuid4().hex}",
            logical_key=logical_key,
            rider_id=intent.rider_id,
            dispute_id=intent.dispute_id,
            decision_id=intent.decision_id,
            item_type="escalation",
            amount=None,
            reason=reason,
            status="pending",
        )
        session.add(item)
        add_trace_event(
            session,
            rider_id=intent.rider_id,
            dispute_id=intent.dispute_id,
            event_type="decision",
            name="provider_failure_escalation",
            input_payload={"intent_id": intent.intent_id, "attempt": attempts},
            output_payload={
                "item_id": item.item_id,
                "logical_key": logical_key,
                "reason": reason,
                "dispute_state": "escalated",
            },
        )


def requeue_failed_intent(
    session: Session,
    intent_id: str,
    reviewer: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Reset a ``failed`` payout intent to ``queued`` for a fresh retry episode.

    Runs inside the caller's transaction. The idempotency key, body fingerprint and provider
    reference are untouched, so the worker replays the *same* request (reconciling against the
    ledger first). Returns ``False`` when the intent does not exist or is not ``failed``.
    """

    statement = select(PayoutIntent).where(PayoutIntent.intent_id == intent_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    intent = session.scalar(statement)
    if intent is None or intent.status != "failed":
        return False
    now = now or _utcnow()
    intent.status = "queued"
    intent.attempt_count = 0
    intent.next_attempt_at = now
    intent.lease_until = None
    intent.last_error = None
    dispute = session.get(Dispute, intent.dispute_id)
    if dispute is not None and dispute.state == "escalated":
        dispute.state = "payout_processing"
    add_trace_event(
        session,
        rider_id=intent.rider_id,
        dispute_id=intent.dispute_id,
        event_type="decision",
        name="provider_failure_requeued",
        input_payload={"intent_id": intent_id, "reviewer": reviewer},
        output_payload={"status": "queued", "idempotency_key": intent.idempotency_key},
    )
    return True


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


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    if raw.replace(".", "", 1).isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)
