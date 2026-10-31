from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class CaseResult:
    name: str
    rider_id: str
    passed: bool
    payout: int
    approval: int | None
    escalation: bool
    latency_ms: float
    errors: list[str]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run QuickDrop black-box conversation evals")
    parser.add_argument("service_url", help="Service base URL, for example http://localhost:8000")
    parser.add_argument(
        "--payswift-url", default="http://localhost:8081", help="Public PaySwift base URL"
    )
    parser.add_argument("--case", type=int, help="Run one zero-based case in a fresh environment")
    parser.add_argument("--poll-seconds", type=float, default=8.0)
    args = parser.parse_args()

    cases = json.loads((ROOT / "data/conversations.json").read_text(encoding="utf-8"))
    selected = [(args.case, cases[args.case])] if args.case is not None else list(enumerate(cases))
    results: list[CaseResult] = []
    with httpx.Client(timeout=10) as client:
        for index, case in selected:
            result = run_case(
                client,
                args.service_url.rstrip("/"),
                args.payswift_url.rstrip("/"),
                index,
                case,
                args.poll_seconds,
            )
            results.append(result)
            marker = "PASS" if result.passed else "FAIL"
            print(
                f"[{marker}] {index:02d} {result.rider_id} {result.name} "
                f"payout={result.payout} approval={result.approval} "
                f"escalation={result.escalation} latency_ms={result.latency_ms:.1f}"
            )
            for error in result.errors:
                print(f"       - {error}")

    passed = sum(item.passed for item in results)
    print(f"\nSummary: {passed}/{len(results)} cases passed")
    if args.case is None:
        print(
            "Isolation note: the assignment expects each case on a fresh stack. "
            "Use --case N with a clean/restarted stack for authoritative per-case results."
        )
    return 0 if passed == len(results) else 1


def run_case(
    client: httpx.Client,
    service_url: str,
    payswift_url: str,
    index: int,
    case: dict[str, Any],
    poll_seconds: float,
) -> CaseResult:
    rider_id = case["rider_id"]
    payouts_before = list_payouts(client, payswift_url, rider_id)
    replies: list[str] = []
    started = time.perf_counter()
    errors: list[str] = []
    for turn in case["turns"]:
        if turn.get("from") != "rider":
            continue
        response = client.post(
            f"{service_url}/messages",
            json={
                "message_id": turn["message_id"],
                "rider_id": rider_id,
                "text": turn["text"],
                "received_at": turn["received_at"],
            },
        )
        if response.status_code // 100 != 2:
            errors.append(f"message {turn['message_id']} returned HTTP {response.status_code}")
            continue
        payload = response.json()
        reply = payload.get("reply")
        if not isinstance(reply, str) or not reply:
            errors.append(f"message {turn['message_id']} returned no reply")
        else:
            replies.append(reply)

    deadline = time.monotonic() + poll_seconds
    payouts_after = payouts_before
    pending: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        payouts_after = list_payouts(client, payswift_url, rider_id)
        pending_response = client.get(f"{service_url}/ops/pending")
        if pending_response.is_success:
            pending = [item for item in pending_response.json() if item.get("rider_id") == rider_id]
        if len(payouts_after) > len(payouts_before) or pending:
            break
        time.sleep(0.25)

    before_ids = {item.get("payout_id") for item in payouts_before}
    new_payouts = [item for item in payouts_after if item.get("payout_id") not in before_ids]
    payout_total = sum(int(item.get("amount", 0)) for item in new_payouts)
    approvals = [item for item in pending if item.get("type") == "approval"]
    escalations = [item for item in pending if item.get("type") == "escalation"]
    approval_amount = sum(int(item["amount"]) for item in approvals) if approvals else None
    expected = case["expected"]
    acceptable = expected.get("one_of") or [expected]
    if not any(
        payout_total == option.get("payout", 0) and approval_amount == option.get("approval")
        for option in acceptable
    ):
        errors.append(
            f"money outcome was payout={payout_total}, approval={approval_amount}; "
            f"expected one of {acceptable}"
        )
    escalation_expectation = expected.get("escalation", "no")
    if escalation_expectation == "required" and not escalations:
        errors.append("required escalation was not created")
    if escalation_expectation == "no" and escalations:
        errors.append("unexpected escalation was created")
    combined_reply = " ".join(replies)
    for mention in expected.get("reply_mentions", []):
        if str(mention).lower() not in combined_reply.lower():
            errors.append(f"reply did not mention {mention!r}")
    trace = client.get(f"{service_url}/trace/{rider_id}")
    if not trace.is_success or not isinstance(trace.json(), list):
        errors.append("trace endpoint failed")
    elif replies and not any(item.get("type") == "reply" for item in trace.json()):
        errors.append("trace has no reply event")
    latency_ms = (time.perf_counter() - started) * 1000
    return CaseResult(
        name=case.get("scenario", f"case-{index}"),
        rider_id=rider_id,
        passed=not errors,
        payout=payout_total,
        approval=approval_amount,
        escalation=bool(escalations),
        latency_ms=latency_ms,
        errors=errors,
    )


def list_payouts(client: httpx.Client, payswift_url: str, rider_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{payswift_url}/v1/payouts", params={"rider_id": rider_id})
    if not response.is_success:
        return []
    payload = response.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        values = payload.get("data", payload.get("payouts", []))
        return [item for item in values if isinstance(item, dict)]
    return []


if __name__ == "__main__":
    sys.exit(main())
