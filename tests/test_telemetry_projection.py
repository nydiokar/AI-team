from datetime import timedelta

from src.core.telemetry import build_event, utc_now
from src.core.telemetry_projection import normalize_context_tokens, project_turn


def _event(name, *, at, invocation_id=None, model_request_id=None, attributes=None):
    return build_event(
        name,
        turn_id="turn_1",
        session_id="session_1",
        node_id="worker-a",
        emitter_process_instance_id="worker_proc",
        source="worker" if name.startswith(("turn.", "invocation.")) else "backend",
        event_time=at,
        observed_time=at,
        invocation_id=invocation_id,
        model_request_id=model_request_id,
        backend="codex",
        model="gpt-5",
        attributes=attributes or {},
    )


def test_context_normalization_does_not_double_count_inclusive_cache():
    assert normalize_context_tokens(
        input_tokens=100,
        cache_read_tokens=80,
        cache_creation_tokens=10,
        input_token_semantics="includes_cache",
    ) == 100
    assert normalize_context_tokens(
        input_tokens=100,
        cache_read_tokens=80,
        cache_creation_tokens=10,
        input_token_semantics="excludes_cache",
    ) == 190
    assert normalize_context_tokens(
        input_tokens=100,
        cache_read_tokens=80,
        cache_creation_tokens=10,
        input_token_semantics="unknown",
    ) is None


def test_request_level_projection_separates_peak_from_total_work():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "invocation.created",
            at=start,
            invocation_id="inv_1",
            attributes={"attempt": 1, "spawn_reason": "initial", "action": "resume_session"},
        ),
        _event("invocation.started", at=start, invocation_id="inv_1"),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=1),
            invocation_id="inv_1",
            model_request_id="mr_1",
            attributes={
                "sequence": 1,
                "work_category": "primary",
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 50,
                "cache_creation_tokens": 0,
                "reasoning_tokens": 0,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "request",
                "usage_source": "fixture",
                "usage_coverage": "complete",
            },
        ),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=2),
            invocation_id="inv_1",
            model_request_id="mr_2",
            attributes={
                "sequence": 2,
                "work_category": "tool_loop",
                "input_tokens": 140,
                "output_tokens": 20,
                "cache_read_tokens": 100,
                "cache_creation_tokens": 0,
                "reasoning_tokens": 0,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "request",
                "usage_source": "fixture",
                "usage_coverage": "complete",
            },
        ),
        _event(
            "invocation.completed",
            at=start + timedelta(seconds=3),
            invocation_id="inv_1",
            attributes={"status": "success", "duration_ms": 3000, "exit_code": 0},
        ),
        _event(
            "turn.completed",
            at=start + timedelta(seconds=3),
            invocation_id="inv_1",
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
        ),
    ]
    projection = project_turn(reversed(events))
    metrics = projection["turn"]["metrics"]
    assert metrics["peak_context_tokens"] == 140
    assert metrics["total_token_work"] == 270
    assert metrics["work_amplification"] == 1.6875
    assert metrics["turn_entry_context_tokens"] == 100
    assert metrics["turn_exit_context_tokens"] == 140
    assert metrics["intra_turn_context_growth"] == 40
    assert metrics["model_request_count"] == 2
    assert metrics["cache_read_ratio"] == 0.625


def test_aggregate_usage_does_not_claim_request_count_or_peak_context():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "invocation.created",
            at=start,
            invocation_id="inv_1",
            attributes={"attempt": 1, "spawn_reason": "initial", "action": "run_oneoff"},
        ),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=1),
            invocation_id="inv_1",
            attributes={
                "input_tokens": 100,
                "output_tokens": 10,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "invocation_total",
                "usage_source": "turn.completed.usage",
                "usage_coverage": "aggregate_only",
            },
        ),
        _event(
            "turn.completed",
            at=start + timedelta(seconds=2),
            invocation_id="inv_1",
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
        ),
    ]
    metrics = project_turn(events)["turn"]["metrics"]
    assert metrics["input_tokens"] == 100
    assert metrics["total_token_work"] == 110
    assert metrics["model_request_count"] is None
    assert metrics["peak_context_tokens"] is None
    assert metrics["metric_quality"] == "aggregate_only"


def test_retry_remains_one_turn_with_two_invocations():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "invocation.created",
            at=start,
            invocation_id="inv_1",
            attributes={"attempt": 1, "spawn_reason": "initial", "action": "resume_session"},
        ),
        _event(
            "invocation.completed",
            at=start + timedelta(seconds=1),
            invocation_id="inv_1",
            attributes={
                "status": "failed",
                "duration_ms": 1000,
                "exit_code": 1,
                "error_code": "rate_limit",
            },
        ),
        _event(
            "invocation.retry_scheduled",
            at=start + timedelta(seconds=1),
            invocation_id="inv_1",
            attributes={
                "retry_reason": "rate_limit",
                "delay_ms": 1000,
                "next_attempt": 2,
                "retry_of_invocation_id": "inv_1",
            },
        ),
        _event(
            "invocation.created",
            at=start + timedelta(seconds=2),
            invocation_id="inv_2",
            attributes={
                "attempt": 2,
                "spawn_reason": "retry",
                "action": "resume_session",
                "retry_of_invocation_id": "inv_1",
            },
        ),
        _event(
            "invocation.completed",
            at=start + timedelta(seconds=3),
            invocation_id="inv_2",
            attributes={"status": "success", "duration_ms": 1000, "exit_code": 0},
        ),
        _event(
            "turn.completed",
            at=start + timedelta(seconds=3),
            invocation_id="inv_2",
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
        ),
    ]
    projection = project_turn(events)
    assert len(projection["invocations"]) == 2
    assert projection["turn"]["metrics"]["invocations_per_turn"] == 2
    assert projection["turn"]["metrics"]["retry_count"] == 1
    assert projection["turn"]["metrics"]["failed_invocation_count"] == 1
