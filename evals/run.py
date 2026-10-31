"""Black-box conversation evals for the QuickDrop dispute desk.

    python evals/run.py http://localhost:8000 [--reset-cmd '<shell>'] [--case N] [...]

The runner uses only the public interface (``POST /messages``, ``GET /trace/{rider_id}``,
``GET /ops/pending``, ``GET /health``) and the PaySwift sandbox (``GET /v1/payouts``), so it can
be pointed at any implementation. It needs nothing beyond the standard library and ``httpx``.

For every case in ``data/conversations.json`` it

* optionally recreates the stack (``--reset-cmd``) and waits for ``/health``. ``data/README.md``
  requires a fresh system per conversation; without a reset all cases share one stack, can
  interact, and the report is marked NON-AUTHORITATIVE;
* sends the rider turns verbatim (same ``message_id``/``received_at``) and times every
  ``POST /messages`` round-trip;
* polls PaySwift and ``/ops/pending`` until the observed ``(payout, approval)`` end state matches
  ``expected`` (or one of ``expected.one_of``) and stays unchanged for ``--settle-seconds``, or
  until ``--poll-seconds`` have passed. Pending items alone never end the wait;
* checks escalation semantics, ``reply_mentions``, trace completeness and per-turn latency.

Exit status is 1 when any case fails or the stack could not be reset.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "data" / "conversations.json"

TRACE_TYPES = frozenset({"message_in", "tool_call", "decision", "reply", "error"})
TRACE_KEYS = frozenset({"at", "type", "name", "input", "output"})
POLL_INTERVAL_SECONDS = 0.5
HEALTH_INTERVAL_SECONDS = 1.0
LEDGER_SNAPSHOT_ATTEMPTS = 5

Clock = Callable[[], float]
Sleep = Callable[[float], None]
JsonList = list[dict[str, Any]]


class StackResetError(RuntimeError):
    """``--reset-cmd`` failed or the service did not become healthy in time."""


@dataclass(slots=True)
class RunOptions:
    poll_seconds: float = 8.0
    settle_seconds: float = 2.0
    max_turn_seconds: float = 9.0


@dataclass(slots=True)
class TurnTiming:
    message_id: str
    seconds: float
    status: int | None  # None when the request raised before a response arrived


@dataclass(slots=True)
class MoneyOutcome:
    payout_total: int
    approval_amount: int | None
    approvals: JsonList
    escalations: JsonList

    @property
    def end_state(self) -> tuple[int, int | None]:
        return (self.payout_total, self.approval_amount)

    @property
    def signature(self) -> tuple[int, int | None, int, int]:
        return (self.payout_total, self.approval_amount, len(self.approvals), len(self.escalations))


@dataclass(slots=True)
class CaseResult:
    index: int
    name: str
    rider_id: str
    passed: bool
    payout: int
    approval: int | None
    escalation: bool
    turns: list[TurnTiming]
    settle_seconds: float
    errors: list[str]

    @property
    def max_turn_seconds(self) -> float:
        return max((turn.seconds for turn in self.turns), default=0.0)


# --------------------------------------------------------------------------- pure decision logic


def acceptable_end_states(expected: dict[str, Any]) -> list[dict[str, int | None]]:
    """Normalise ``expected`` into the list of acceptable ``{payout, approval}`` end states."""

    options = expected.get("one_of") or [expected]
    states: list[dict[str, int | None]] = []
    for option in options:
        approval = option.get("approval")
        states.append(
            {
                "payout": int(option.get("payout", 0) or 0),
                "approval": None if approval is None else int(approval),
            }
        )
    return states


def end_state_satisfied(
    observed: tuple[int, int | None], acceptable: Sequence[dict[str, int | None]]
) -> bool:
    payout_total, approval_amount = observed
    return any(
        payout_total == option["payout"] and approval_amount == option["approval"]
        for option in acceptable
    )


def money_outcome(
    payouts_before: JsonList, payouts_after: JsonList, pending: JsonList
) -> MoneyOutcome:
    """Diff the cumulative PaySwift ledger by payout id; split this rider's pending items."""

    before_ids = {item.get("payout_id") for item in payouts_before}
    new_payouts = [item for item in payouts_after if item.get("payout_id") not in before_ids]
    payout_total = sum(int(item.get("amount", 0) or 0) for item in new_payouts)
    approvals = [item for item in pending if item.get("type") == "approval"]
    escalations = [item for item in pending if item.get("type") == "escalation"]
    approval_amount = sum(int(item.get("amount") or 0) for item in approvals) if approvals else None
    return MoneyOutcome(payout_total, approval_amount, approvals, escalations)


def evaluate_escalation(expectation: str, escalations: Sequence[Any]) -> list[str]:
    """``required`` needs one, ``no`` forbids one, ``optional`` accepts either."""

    errors: list[str] = []
    if expectation == "required" and not escalations:
        errors.append("required escalation was not created")
    if expectation == "no" and escalations:
        errors.append("unexpected escalation was created")
    return errors


def reply_mention_errors(mentions: Iterable[Any], replies: Sequence[str]) -> list[str]:
    combined = " ".join(replies).lower()
    return [
        f"reply did not mention {mention!r}"
        for mention in mentions
        if str(mention).lower() not in combined
    ]


def trace_complete(events: Any, message_ids: Iterable[str]) -> list[str]:
    """Validate the public trace shape and that every accepted message is traced and answered.

    Every event needs ``at, type, name, input, output`` with ``type`` from the fixed enum. When
    ``message_in`` events expose ``input.message_id`` every accepted id must appear; otherwise the
    number of ``message_in`` events must at least cover the distinct ids. There must be at least
    one ``reply`` per distinct accepted id. Ordering is the server's sequence and is not checked.
    """

    errors: list[str] = []
    if not isinstance(events, list):
        return ["trace is not a JSON list"]
    valid: JsonList = []
    for position, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"trace event {position} is not an object")
            continue
        missing = sorted(TRACE_KEYS - event.keys())
        if missing:
            errors.append(f"trace event {position} is missing keys {missing}")
        if event.get("type") not in TRACE_TYPES:
            errors.append(f"trace event {position} has unknown type {event.get('type')!r}")
        valid.append(event)

    distinct = {str(message_id) for message_id in message_ids}
    message_in = [event for event in valid if event.get("type") == "message_in"]
    replies = [event for event in valid if event.get("type") == "reply"]
    traced_ids = {
        str(event["input"]["message_id"])
        for event in message_in
        if isinstance(event.get("input"), dict) and event["input"].get("message_id") is not None
    }
    if traced_ids:
        errors.extend(
            f"trace has no message_in event for message {message_id}"
            for message_id in sorted(distinct - traced_ids)
        )
    elif len(message_in) < len(distinct):
        errors.append(
            f"trace has {len(message_in)} message_in events for {len(distinct)} distinct messages"
        )
    if len(replies) < len(distinct):
        errors.append(
            f"trace has {len(replies)} reply events for {len(distinct)} distinct messages"
        )
    return errors


def turn_latency_errors(turns: Sequence[TurnTiming], max_turn_seconds: float) -> list[str]:
    return [
        f"message {turn.message_id} took {turn.seconds:.2f} s (limit {max_turn_seconds:.1f} s)"
        for turn in turns
        if turn.seconds > max_turn_seconds
    ]


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty sample."""

    if not values:
        return 0.0
    ordered = sorted(values)
    rank = min(len(ordered), max(1, math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def parse_payout_listing(payload: Any) -> JsonList:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        values = payload.get("data", payload.get("payouts", []))
        if isinstance(values, list):
            return [item for item in values if isinstance(item, dict)]
    return []


# ------------------------------------------------------------------------------- HTTP helpers


def safe_json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def list_payouts(client: httpx.Client, payswift_url: str, rider_id: str) -> JsonList | None:
    """This rider's PaySwift ledger, or ``None`` when the sandbox did not answer usefully."""

    try:
        response = client.get(f"{payswift_url}/v1/payouts", params={"rider_id": rider_id})
    except httpx.HTTPError:
        return None
    if not response.is_success:
        return None
    payload = safe_json(response)
    return None if payload is None else parse_payout_listing(payload)


def snapshot_ledger(
    client: httpx.Client, payswift_url: str, rider_id: str, *, sleep: Sleep = time.sleep
) -> JsonList | None:
    for attempt in range(LEDGER_SNAPSHOT_ATTEMPTS):
        ledger = list_payouts(client, payswift_url, rider_id)
        if ledger is not None:
            return ledger
        if attempt + 1 < LEDGER_SNAPSHOT_ATTEMPTS:
            sleep(POLL_INTERVAL_SECONDS)
    return None


def list_pending(client: httpx.Client, service_url: str, rider_id: str) -> JsonList | None:
    """This rider's ``/ops/pending`` items, or ``None`` when the endpoint did not answer."""

    try:
        response = client.get(f"{service_url}/ops/pending")
    except httpx.HTTPError:
        return None
    payload = safe_json(response) if response.is_success else None
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict) and item.get("rider_id") == rider_id]


def fetch_trace(client: httpx.Client, service_url: str, rider_id: str) -> Any | None:
    try:
        response = client.get(f"{service_url}/trace/{rider_id}")
    except httpx.HTTPError:
        return None
    return safe_json(response) if response.is_success else None


def wait_for_health(
    client: httpx.Client,
    service_url: str,
    timeout: float,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> float:
    """Poll ``/health`` until it returns 200; raise ``StackResetError`` after ``timeout`` s."""

    started = clock()
    last = "no response yet"
    while True:
        try:
            response = client.get(f"{service_url}/health")
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        else:
            if response.status_code == 200:
                return clock() - started
            last = f"HTTP {response.status_code}"
        if clock() - started >= timeout:
            raise StackResetError(
                f"{service_url}/health did not return 200 within {timeout:.0f} s (last: {last})"
            )
        sleep(HEALTH_INTERVAL_SECONDS)


def reset_stack(
    reset_cmd: str,
    client: httpx.Client,
    service_url: str,
    timeout: float,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> float:
    """Run ``reset_cmd`` through the shell, then wait for the service to be healthy."""

    say(f"[reset] $ {reset_cmd}")
    try:
        subprocess.run(reset_cmd, shell=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise StackResetError(f"--reset-cmd exited with status {exc.returncode}") from exc
    waited = wait_for_health(client, service_url, timeout, clock=clock, sleep=sleep)
    say(f"[reset] {service_url}/health returned 200 after {waited:.1f} s")
    return waited


# ----------------------------------------------------------------------------------- one case


def run_case(
    client: httpx.Client,
    service_url: str,
    payswift_url: str,
    index: int,
    case: dict[str, Any],
    options: RunOptions,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> CaseResult:
    rider_id = case["rider_id"]
    expected = case.get("expected", {})
    acceptable = acceptable_end_states(expected)
    errors: list[str] = []

    payouts_before = snapshot_ledger(client, payswift_url, rider_id, sleep=sleep)
    if payouts_before is None:
        errors.append(
            "PaySwift ledger could not be read before the case; payout total is unreliable"
        )
        payouts_before = []

    replies: list[str] = []
    turns: list[TurnTiming] = []
    accepted_ids: list[str] = []
    for turn in case["turns"]:
        if turn.get("from") != "rider":
            continue
        message_id = str(turn["message_id"])
        started = clock()
        try:
            response = client.post(
                f"{service_url}/messages",
                json={
                    "message_id": message_id,
                    "rider_id": rider_id,
                    "text": turn["text"],
                    "received_at": turn["received_at"],
                },
            )
        except httpx.HTTPError as exc:
            turns.append(TurnTiming(message_id, clock() - started, None))
            errors.append(f"message {message_id} failed: {type(exc).__name__}: {exc}")
            continue
        turns.append(TurnTiming(message_id, clock() - started, response.status_code))
        if response.status_code // 100 != 2:
            errors.append(f"message {message_id} returned HTTP {response.status_code}")
            continue
        accepted_ids.append(message_id)
        payload = safe_json(response)
        reply = payload.get("reply") if isinstance(payload, dict) else None
        if not isinstance(reply, str) or not reply:
            errors.append(f"message {message_id} returned no reply")
        else:
            replies.append(reply)

    # Poll until the acceptable end state is observed and stable, or the deadline passes.
    # A pending item on its own never ends the wait (payouts are asynchronous).
    poll_started = clock()
    deadline = poll_started + options.poll_seconds
    payouts_after: JsonList = payouts_before
    pending: JsonList = []
    outcome = money_outcome(payouts_before, payouts_after, pending)
    signature: tuple[int, int | None, int, int] | None = None
    stable_since = poll_started
    while True:
        ledger = list_payouts(client, payswift_url, rider_id)
        if ledger is not None:
            payouts_after = ledger
        pending_now = list_pending(client, service_url, rider_id)
        if pending_now is not None:
            pending = pending_now
        outcome = money_outcome(payouts_before, payouts_after, pending)
        now = clock()
        if outcome.signature != signature:
            signature = outcome.signature
            stable_since = now
        satisfied = end_state_satisfied(outcome.end_state, acceptable)
        if (satisfied and now - stable_since >= options.settle_seconds) or now >= deadline:
            break
        sleep(POLL_INTERVAL_SECONDS)
    settle_seconds = clock() - poll_started

    if not end_state_satisfied(outcome.end_state, acceptable):
        errors.append(
            f"money outcome was payout={outcome.payout_total}, "
            f"approval={outcome.approval_amount}; expected one of {acceptable}"
        )
    errors.extend(evaluate_escalation(expected.get("escalation", "no"), outcome.escalations))
    errors.extend(reply_mention_errors(expected.get("reply_mentions", []), replies))
    events = fetch_trace(client, service_url, rider_id)
    if events is None:
        errors.append("trace endpoint failed")
    else:
        errors.extend(trace_complete(events, accepted_ids))
    errors.extend(turn_latency_errors(turns, options.max_turn_seconds))

    return CaseResult(
        index=index,
        name=case.get("scenario", f"case-{index}"),
        rider_id=rider_id,
        passed=not errors,
        payout=outcome.payout_total,
        approval=outcome.approval_amount,
        escalation=bool(outcome.escalations),
        turns=turns,
        settle_seconds=settle_seconds,
        errors=errors,
    )


# ----------------------------------------------------------------------------------- reporting


def say(message: str) -> None:
    print(message, flush=True)


def shared_stack_banner(case_count: int) -> str:
    lines = [
        "NOTE: no --reset-cmd given, so nothing is reset between cases; all cases share one stack."
    ]
    if case_count > 1:
        lines += [
            "      data/README.md requires a fresh system per conversation. On a shared stack",
            "      cases can interact (same rider in several cases, daily auto-pay limits,",
            "      leftover /ops/pending items), so these results are NON-AUTHORITATIVE.",
        ]
    else:
        lines.append(
            "      This case is authoritative only if the stack was freshly started for this run."
        )
    lines += [
        "      For authoritative results pass, for example:",
        "        --reset-cmd 'docker compose down --volumes && docker compose up --detach'",
    ]
    return "\n".join(lines)


def format_result(result: CaseResult) -> str:
    marker = "PASS" if result.passed else "FAIL"
    return (
        f"[{marker}] {result.index:02d} {result.rider_id} {result.name} "
        f"payout={result.payout} approval={result.approval} escalation={result.escalation} "
        f"max_turn_ms={result.max_turn_seconds * 1000:.1f} settled_in_s={result.settle_seconds:.1f}"
    )


def summary_report(
    results: Sequence[CaseResult], *, total: int, isolated: bool, options: RunOptions
) -> str:
    passed = sum(result.passed for result in results)
    latencies = [turn.seconds for result in results for turn in result.turns]
    lines = [
        "",
        f"Summary: {passed}/{total} cases passed"
        + (f" ({len(results)} run)" if len(results) != total else ""),
        f"Turn latency: {len(latencies)} POST /messages turns, "
        f"p95 {percentile(latencies, 0.95) * 1000:.1f} ms, "
        f"max {max(latencies, default=0.0) * 1000:.1f} ms "
        f"(limit {options.max_turn_seconds:.1f} s per turn)",
    ]
    if isolated:
        lines.append("Isolation: fresh stack per case via --reset-cmd (authoritative).")
    elif total > 1:
        lines.append(
            "Isolation: NON-AUTHORITATIVE, all cases shared one stack (no --reset-cmd); "
            "cases can interact."
        )
    else:
        lines.append(
            "Isolation: single case without --reset-cmd; authoritative only on a fresh stack."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the QuickDrop black-box conversation evals against a running stack.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("service_url", help="Service base URL, for example http://localhost:8000")
    parser.add_argument(
        "--payswift-url", default="http://localhost:8081", help="Public PaySwift base URL"
    )
    parser.add_argument("--case", type=int, help="Run only this zero-based case")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=8.0,
        help="Longest wait for the expected PaySwift/ops end state after the last turn",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=2.0,
        help="How long the expected end state must stay unchanged before a case is accepted "
        "(catches late or duplicate payouts)",
    )
    parser.add_argument(
        "--max-turn-seconds",
        type=float,
        default=9.0,
        help="Fail a case when any POST /messages round-trip takes longer than this",
    )
    parser.add_argument(
        "--reset-cmd",
        help="Shell command run before EVERY case to recreate a fresh stack, e.g. "
        "'docker compose down --volumes && docker compose up --detach'; the runner then "
        "waits for <service_url>/health. Without it the report is NON-AUTHORITATIVE.",
    )
    parser.add_argument(
        "--reset-timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for /health to return 200 after --reset-cmd",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if args.case is not None and not 0 <= args.case < len(cases):
        print(f"--case must be between 0 and {len(cases) - 1}", file=sys.stderr)
        return 2
    selected = [(args.case, cases[args.case])] if args.case is not None else list(enumerate(cases))
    options = RunOptions(
        poll_seconds=args.poll_seconds,
        settle_seconds=args.settle_seconds,
        max_turn_seconds=args.max_turn_seconds,
    )
    service_url = args.service_url.rstrip("/")
    payswift_url = args.payswift_url.rstrip("/")
    if not args.reset_cmd:
        say(shared_stack_banner(len(selected)))

    results: list[CaseResult] = []
    aborted: str | None = None
    timeout = httpx.Timeout(max(10.0, options.max_turn_seconds + 1.0))
    with httpx.Client(timeout=timeout) as client:
        for index, case in selected:
            if args.reset_cmd:
                try:
                    reset_stack(args.reset_cmd, client, service_url, args.reset_timeout)
                except StackResetError as exc:
                    aborted = f"before case {index:02d}: {exc}"
                    break
            result = run_case(client, service_url, payswift_url, index, case, options)
            results.append(result)
            say(format_result(result))
            for error in result.errors:
                say(f"       - {error}")

    say(
        summary_report(results, total=len(selected), isolated=bool(args.reset_cmd), options=options)
    )
    if aborted:
        say(f"ABORTED {aborted}")
        return 1
    return 0 if results and all(result.passed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
