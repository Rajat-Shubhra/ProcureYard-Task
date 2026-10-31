"""Unit tests for the black-box eval runner (``evals/run.py``); no network, no Docker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_runner", ROOT / "evals" / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


# ------------------------------------------------------------------------------------ fakes


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class FakeStack:
    """Scripted stand-in for the service + PaySwift as seen through the public interface.

    ``ledger`` is a list of ledger snapshots returned by successive ``GET /v1/payouts`` calls
    (the last one repeats); ``None`` entries simulate an unreadable sandbox response.
    """

    def __init__(
        self,
        *,
        clock: FakeClock,
        ledger: list[list[dict[str, Any]] | None] | None = None,
        pending: list[dict[str, Any]] | None = None,
        trace: Any = None,
        reply: str = "T926334 ke liye 25 rupaye process ho gaye.",
        post_status: int = 200,
        post_seconds: float = 0.05,
        health: list[Any] | None = None,
    ) -> None:
        self.clock = clock
        self.ledger = ledger or [[]]
        self.ledger_calls = 0
        self.pending = pending or []
        self.trace = [] if trace is None else trace
        self.reply = reply
        self.post_status = post_status
        self.post_seconds = post_seconds
        self.health = health or [FakeResponse(200, {"status": "ok"})]
        self.health_calls = 0
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        assert url.endswith("/messages")
        self.posts.append(json)
        self.clock.sleep(self.post_seconds)
        if self.post_status // 100 != 2:
            return FakeResponse(self.post_status, {"error": {"code": "internal_error"}})
        return FakeResponse(self.post_status, {"reply": self.reply})

    def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResponse:
        if url.endswith("/v1/payouts"):
            snapshot = self.ledger[min(self.ledger_calls, len(self.ledger) - 1)]
            self.ledger_calls += 1
            if snapshot is None:
                return FakeResponse(503, {"error": "service_unavailable"})
            return FakeResponse(200, {"data": snapshot, "count": len(snapshot)})
        if url.endswith("/ops/pending"):
            return FakeResponse(200, self.pending)
        if "/trace/" in url:
            return FakeResponse(200, self.trace)
        if url.endswith("/health"):
            outcome = self.health[min(self.health_calls, len(self.health) - 1)]
            self.health_calls += 1
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        raise AssertionError(f"unexpected GET {url}")


def _event(
    event_type: str, *, message_id: str | None = None, output: Any = None, **extra: Any
) -> dict[str, Any]:
    event = {
        "at": "2026-09-23T10:00:00+00:00",
        "type": event_type,
        "name": event_type,
        "input": {"message_id": message_id} if message_id else None,
        "output": output,
    }
    event.update(extra)
    return event


def _payout(payout_id: str, amount: int, rider_id: str = "R027") -> dict[str, Any]:
    return {
        "payout_id": payout_id,
        "rider_id": rider_id,
        "amount": amount,
        "reference": f"qd:{payout_id}",
        "status": "processed",
    }


def _case(
    rider_id: str = "R027",
    message_ids: tuple[str, ...] = ("m1",),
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "scenario": "unit",
        "rider_id": rider_id,
        "turns": [
            {
                "from": "rider",
                "message_id": message_id,
                "text": "payment kam aaya",
                "received_at": "2026-09-23T10:00:00+05:30",
            }
            for message_id in message_ids
        ]
        + [{"from": "agent", "text": "example reply, never sent"}],
        "expected": expected or {"payout": 25, "approval": None, "escalation": "no"},
    }


def _run(stack: FakeStack, case: dict[str, Any], clock: FakeClock, **options: Any) -> Any:
    return runner.run_case(
        stack,
        "http://svc",
        "http://pay",
        7,
        case,
        runner.RunOptions(**options),
        clock=clock,
        sleep=clock.sleep,
    )


# ------------------------------------------------------------------------- pure decision logic


def test_acceptable_end_states_single_and_one_of() -> None:
    assert runner.acceptable_end_states({"payout": 25, "approval": None, "escalation": "no"}) == [
        {"payout": 25, "approval": None}
    ]
    assert runner.acceptable_end_states({"escalation": "optional"}) == [
        {"payout": 0, "approval": None}
    ]
    assert runner.acceptable_end_states(
        {"one_of": [{"payout": 15, "approval": 10}, {"payout": 10, "approval": 15}]}
    ) == [{"payout": 15, "approval": 10}, {"payout": 10, "approval": 15}]


def test_end_state_satisfied_matches_any_option_exactly() -> None:
    acceptable = runner.acceptable_end_states(
        {"one_of": [{"payout": 15, "approval": 10}, {"payout": 10, "approval": 15}]}
    )
    assert runner.end_state_satisfied((15, 10), acceptable)
    assert runner.end_state_satisfied((10, 15), acceptable)
    assert not runner.end_state_satisfied((0, 10), acceptable)
    assert not runner.end_state_satisfied((25, 10), acceptable)
    assert not runner.end_state_satisfied((15, None), acceptable)
    no_action = runner.acceptable_end_states({"payout": 0, "approval": None})
    assert runner.end_state_satisfied((0, None), no_action)
    assert not runner.end_state_satisfied((0, 0), no_action)


def test_money_outcome_diffs_cumulative_ledger_by_payout_id() -> None:
    before = [_payout("old", 25)]
    after = [_payout("old", 25), _payout("new1", 15), _payout("new2", 10)]
    pending = [
        {"rider_id": "R027", "type": "approval", "amount": 10},
        {"rider_id": "R027", "type": "approval", "amount": "5"},
        {"rider_id": "R027", "type": "escalation", "amount": None},
    ]
    outcome = runner.money_outcome(before, after, pending)
    assert outcome.payout_total == 25
    assert outcome.approval_amount == 15
    assert len(outcome.approvals) == 2 and len(outcome.escalations) == 1
    assert outcome.end_state == (25, 15)
    assert outcome.signature == (25, 15, 2, 1)


def test_money_outcome_without_approvals_is_none_not_zero() -> None:
    outcome = runner.money_outcome([], [], [{"type": "escalation", "amount": None}])
    assert outcome.end_state == (0, None)
    assert outcome.escalations and not outcome.approvals


@pytest.mark.parametrize(
    ("expectation", "has_escalation", "expected_errors"),
    [
        ("required", True, 0),
        ("required", False, 1),
        ("no", True, 1),
        ("no", False, 0),
        ("optional", True, 0),
        ("optional", False, 0),
    ],
)
def test_evaluate_escalation_semantics(
    expectation: str, has_escalation: bool, expected_errors: int
) -> None:
    escalations = [{"type": "escalation"}] if has_escalation else []
    assert len(runner.evaluate_escalation(expectation, escalations)) == expected_errors


def test_reply_mentions_are_case_insensitive_substrings_over_all_replies() -> None:
    replies = ["Trip t926334 check kiya.", "Aapko ₹25 milenge."]
    assert runner.reply_mention_errors(["T926334", 25], replies) == []
    assert runner.reply_mention_errors(["T999999"], replies) == ["reply did not mention 'T999999'"]


def test_trace_complete_accepts_a_full_trace() -> None:
    events = [
        _event("message_in", message_id="m1"),
        _event("tool_call", output={"rows": 1}),
        _event("decision", output={"action": "pay"}),
        _event("reply", output="done"),
        _event("message_in", message_id="m2"),
        _event("error", output={"detail": "x"}),
        _event("reply", output="done again"),
    ]
    assert runner.trace_complete(events, ["m1", "m2", "m2"]) == []


def test_trace_complete_reports_shape_and_enum_violations() -> None:
    bad_key = _event("decision")
    del bad_key["output"]
    errors = runner.trace_complete(
        [bad_key, _event("thinking"), "not-an-object", _event("message_in", message_id="m1")],
        ["m1"],
    )
    assert "trace event 0 is missing keys ['output']" in errors
    assert "trace event 1 has unknown type 'thinking'" in errors
    assert "trace event 2 is not an object" in errors
    assert "trace has 0 reply events for 1 distinct messages" in errors


def test_trace_complete_requires_message_in_per_id_and_reply_per_id() -> None:
    events = [_event("message_in", message_id="m1"), _event("reply", output="hi")]
    errors = runner.trace_complete(events, ["m1", "m2"])
    assert errors == [
        "trace has no message_in event for message m2",
        "trace has 1 reply events for 2 distinct messages",
    ]


def test_trace_complete_falls_back_to_counts_when_ids_are_not_exposed() -> None:
    anonymous = [_event("message_in"), _event("reply", output="a")]
    assert runner.trace_complete(anonymous, ["m1"]) == []
    assert runner.trace_complete(anonymous, ["m1", "m2"]) == [
        "trace has 1 message_in events for 2 distinct messages",
        "trace has 1 reply events for 2 distinct messages",
    ]
    assert runner.trace_complete({"not": "a list"}, ["m1"]) == ["trace is not a JSON list"]
    assert runner.trace_complete([], []) == []


def test_turn_latency_errors_flag_only_slow_turns() -> None:
    turns = [runner.TurnTiming("m1", 0.4, 200), runner.TurnTiming("m2", 9.4, 200)]
    assert runner.turn_latency_errors(turns, 9.0) == ["message m2 took 9.40 s (limit 9.0 s)"]
    assert runner.turn_latency_errors(turns, 10.0) == []


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 21)]  # 1..20
    assert runner.percentile(values, 0.95) == 19.0
    assert runner.percentile(values, 0.5) == 10.0
    assert runner.percentile([3.0], 0.95) == 3.0
    assert runner.percentile([], 0.95) == 0.0


def test_parse_payout_listing_accepts_known_shapes() -> None:
    rows = [_payout("p1", 5), "junk"]
    assert runner.parse_payout_listing(rows) == [rows[0]]
    assert runner.parse_payout_listing({"data": rows, "count": 2}) == [rows[0]]
    assert runner.parse_payout_listing({"payouts": rows}) == [rows[0]]
    assert runner.parse_payout_listing({"data": "oops"}) == []
    assert runner.parse_payout_listing("oops") == []


# ------------------------------------------------------------------------------- run_case flow


def test_run_case_keeps_polling_past_pending_until_payout_lands() -> None:
    """Regression for finding 8-01: an approval exists at once, the payout only on poll three."""

    clock = FakeClock()
    paid = [_payout("p15", 15)]
    stack = FakeStack(
        clock=clock,
        ledger=[[], [], [], paid],  # snapshot, poll 1, poll 2, poll 3+
        pending=[{"rider_id": "R027", "type": "approval", "amount": 10}],
        trace=[
            _event("message_in", message_id="m1"),
            _event("reply", output="15 abhi, 10 approval me"),
        ],
        reply="15 abhi, 10 approval me",
    )
    case = _case(
        expected={
            "one_of": [{"payout": 15, "approval": 10}, {"payout": 10, "approval": 15}],
            "escalation": "no",
            "reply_mentions": ["15", "10"],
        }
    )
    result = _run(stack, case, clock, poll_seconds=8.0, settle_seconds=2.0)
    assert result.errors == []
    assert result.passed and (result.payout, result.approval) == (15, 10)
    assert stack.ledger_calls >= 4
    assert result.settle_seconds < 8.0  # stopped once the end state was stable


def test_run_case_fails_when_payout_never_arrives() -> None:
    clock = FakeClock()
    stack = FakeStack(
        clock=clock,
        pending=[{"rider_id": "R027", "type": "approval", "amount": 10}],
        trace=[_event("message_in", message_id="m1"), _event("reply", output="x")],
    )
    result = _run(stack, _case(expected={"payout": 15, "approval": 10}), clock, poll_seconds=3.0)
    assert not result.passed
    assert result.errors == [
        "money outcome was payout=0, approval=10; expected one of [{'payout': 15, 'approval': 10}]"
    ]
    assert result.settle_seconds >= 3.0  # waited for the whole deadline


def test_run_case_catches_late_payout_inside_settle_window() -> None:
    clock = FakeClock()
    stack = FakeStack(
        clock=clock,
        ledger=[[], [], [], [_payout("late", 25)]],
        trace=[_event("message_in", message_id="m1"), _event("reply", output="x")],
    )
    case = _case(expected={"payout": 0, "approval": None, "escalation": "no"})
    result = _run(stack, case, clock, poll_seconds=6.0, settle_seconds=2.0)
    assert not result.passed
    assert result.payout == 25
    assert result.errors[0].startswith("money outcome was payout=25, approval=None")


def test_run_case_escalation_and_mention_and_trace_checks() -> None:
    clock = FakeClock()
    stack = FakeStack(
        clock=clock,
        pending=[{"rider_id": "R027", "type": "escalation", "amount": None}],
        trace=[_event("message_in", message_id="m1")],  # no reply event
        reply="Hum check kar rahe hain.",
    )
    case = _case(
        expected={"payout": 0, "approval": None, "escalation": "no", "reply_mentions": [25]}
    )
    result = _run(stack, case, clock)
    assert result.escalation is True
    assert result.errors == [
        "unexpected escalation was created",
        "reply did not mention 25",
        "trace has 0 reply events for 1 distinct messages",
    ]


def test_run_case_duplicate_delivery_sends_both_and_expects_one_trace_pair() -> None:
    clock = FakeClock()
    stack = FakeStack(
        clock=clock,
        ledger=[[], [_payout("p25", 25, "R003")]],
        trace=[_event("message_in", message_id="dup"), _event("reply", output="25 done")],
    )
    case = _case(rider_id="R003", message_ids=("dup", "dup"))
    result = _run(stack, case, clock)
    assert [post["message_id"] for post in stack.posts] == ["dup", "dup"]
    assert result.passed, result.errors
    assert len(result.turns) == 2


def test_run_case_flags_slow_turns_and_reports_latency() -> None:
    clock = FakeClock()
    stack = FakeStack(
        clock=clock,
        ledger=[[], [_payout("p", 25)]],
        trace=[_event("message_in", message_id="m1"), _event("reply", output="ok")],
        post_seconds=9.5,
    )
    result = _run(stack, _case(), clock, max_turn_seconds=9.0)
    assert result.errors == ["message m1 took 9.50 s (limit 9.0 s)"]
    assert result.max_turn_seconds == pytest.approx(9.5)
    assert "max_turn_ms=9500.0" in runner.format_result(result)


def test_run_case_records_http_errors_without_crashing() -> None:
    clock = FakeClock()
    stack = FakeStack(clock=clock, post_status=500, trace=[])
    result = _run(stack, _case(message_ids=("m1", "m2")), clock, poll_seconds=0.0)
    assert "message m1 returned HTTP 500" in result.errors
    assert "message m2 returned HTTP 500" in result.errors
    assert all(turn.status == 500 for turn in result.turns)
    assert not any(error.startswith("trace has") for error in result.errors)


def test_run_case_keeps_last_good_ledger_when_payswift_hiccups() -> None:
    clock = FakeClock()
    stack = FakeStack(
        clock=clock,
        ledger=[[], [_payout("p", 25)], None, None, None, None, None],
        trace=[_event("message_in", message_id="m1"), _event("reply", output="ok")],
    )
    result = _run(stack, _case(), clock, settle_seconds=2.0)
    assert result.passed, result.errors
    assert result.payout == 25


def test_run_case_reports_unreadable_ledger_snapshot() -> None:
    clock = FakeClock()
    stack = FakeStack(clock=clock, ledger=[None], trace=[])
    result = _run(stack, _case(expected={"payout": 0, "approval": None}), clock, poll_seconds=0.0)
    assert result.errors[0].startswith("PaySwift ledger could not be read before the case")
    assert stack.ledger_calls > runner.LEDGER_SNAPSHOT_ATTEMPTS


# ------------------------------------------------------------------------- reset / health wait


def test_wait_for_health_retries_through_errors_until_200() -> None:
    clock = FakeClock()
    stack = FakeStack(
        clock=clock,
        health=[
            httpx.ConnectError("connection refused"),
            FakeResponse(503, {"error": {"code": "service_not_ready"}}),
            FakeResponse(200, {"status": "ok"}),
        ],
    )
    waited = runner.wait_for_health(stack, "http://svc", 30.0, clock=clock, sleep=clock.sleep)
    assert waited == pytest.approx(2 * runner.HEALTH_INTERVAL_SECONDS)
    assert stack.health_calls == 3


def test_wait_for_health_times_out() -> None:
    clock = FakeClock()
    stack = FakeStack(clock=clock, health=[FakeResponse(503, {})])
    with pytest.raises(runner.StackResetError, match="did not return 200 within 5 s"):
        runner.wait_for_health(stack, "http://svc", 5.0, clock=clock, sleep=clock.sleep)


def test_reset_stack_runs_shell_command_then_waits_for_health(
    capsys: pytest.CaptureFixture,
) -> None:
    clock = FakeClock()
    stack = FakeStack(clock=clock)
    runner.reset_stack("exit 0", stack, "http://svc", 10.0, clock=clock, sleep=clock.sleep)
    assert stack.health_calls == 1
    assert "[reset] $ exit 0" in capsys.readouterr().out


def test_reset_stack_failure_aborts_before_health_wait() -> None:
    clock = FakeClock()
    stack = FakeStack(clock=clock)
    with pytest.raises(runner.StackResetError, match="exited with status 3"):
        runner.reset_stack("exit 3", stack, "http://svc", 10.0, clock=clock, sleep=clock.sleep)
    assert stack.health_calls == 0


# ---------------------------------------------------------------------------- CLI / reporting


def test_parser_defaults_and_new_flags() -> None:
    args = runner.build_parser().parse_args(["http://localhost:8000"])
    assert args.poll_seconds == 8.0
    assert args.settle_seconds == 2.0
    assert args.max_turn_seconds == 9.0
    assert args.reset_cmd is None
    assert args.reset_timeout == 180.0
    args = runner.build_parser().parse_args(
        [
            "http://x",
            "--reset-cmd",
            "docker compose up -d",
            "--case",
            "4",
            "--max-turn-seconds",
            "6",
        ]
    )
    assert args.reset_cmd == "docker compose up -d"
    assert args.case == 4 and args.max_turn_seconds == 6.0


def test_main_rejects_out_of_range_case(capsys: pytest.CaptureFixture) -> None:
    assert runner.main(["http://localhost:8000", "--case", "999"]) == 2
    assert "--case must be between 0 and" in capsys.readouterr().err


def test_shared_stack_banner_and_summary_mark_non_authoritative_runs() -> None:
    banner = runner.shared_stack_banner(24)
    assert "NON-AUTHORITATIVE" in banner and "--reset-cmd" in banner
    assert "NON-AUTHORITATIVE" not in runner.shared_stack_banner(1)
    result = runner.CaseResult(
        index=1,
        name="unit",
        rider_id="R027",
        passed=True,
        payout=15,
        approval=10,
        escalation=False,
        turns=[runner.TurnTiming("m1", 0.1, 200), runner.TurnTiming("m2", 0.3, 200)],
        settle_seconds=2.5,
        errors=[],
    )
    options = runner.RunOptions()
    shared = runner.summary_report([result], total=24, isolated=False, options=options)
    assert "Summary: 1/24 cases passed (1 run)" in shared
    assert "NON-AUTHORITATIVE" in shared
    assert "p95 300.0 ms, max 300.0 ms" in shared
    isolated = runner.summary_report([result], total=1, isolated=True, options=options)
    assert "authoritative" in isolated and "NON-AUTHORITATIVE" not in isolated
