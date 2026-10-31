from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import GateRun, RuleCheckResult

RuleStatus = Literal["pass", "fail", "not_applicable"]


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    status: RuleStatus
    detail: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GateReport:
    gate: Literal["interpretation", "action", "reply"]
    checker_version: str
    results: tuple[RuleResult, ...]
    blocking_reasons: tuple[str, ...]
    accepted_complaint_indexes: tuple[int, ...] = ()
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def passed(self) -> bool:
        return not self.blocking_reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "checker_version": self.checker_version,
            "checked_at": self.checked_at.isoformat(),
            "results": [
                {
                    "rule_id": item.rule_id,
                    "status": item.status,
                    "detail": item.detail,
                    "details": item.details,
                }
                for item in self.results
            ],
            "blocking_reasons": list(self.blocking_reasons),
            "accepted_complaint_indexes": list(self.accepted_complaint_indexes),
        }


def persist_gate_report(
    session: Session,
    report: GateReport,
    *,
    message_id: str | None = None,
    dispute_id: str | None = None,
    decision_id: str | None = None,
) -> str:
    gate_run_id = f"gate_{uuid4().hex}"
    session.add(
        GateRun(
            gate_run_id=gate_run_id,
            message_id=message_id,
            dispute_id=dispute_id,
            decision_id=decision_id,
            gate=report.gate,
            passed=report.passed,
            checker_version=report.checker_version,
            blocking_reasons=list(report.blocking_reasons),
            created_at=report.checked_at,
        )
    )
    for item in report.results:
        session.add(
            RuleCheckResult(
                result_id=f"rule_{uuid4().hex}",
                gate_run_id=gate_run_id,
                decision_id=decision_id,
                gate=report.gate,
                rule_id=item.rule_id,
                result=item.status,
                rule_version=report.checker_version,
                details={"detail": item.detail, **item.details},
                checked_at=report.checked_at,
            )
        )
    return gate_run_id
