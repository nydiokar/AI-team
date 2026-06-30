from datetime import timedelta

from src.core.telemetry import build_event, utc_now
from src.core.telemetry_projection import normalize_context_tokens, project_turn


def _event(
    name,
    *,
    at,
    invocation_id=None,
    model_request_id=None,
    tool_call_id=None,
    attributes=None,
):
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
        tool_call_id=tool_call_id,
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


def test_negative_turn_duration_is_unknown_and_flagged_as_clock_skew():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "turn.completed",
            at=start - timedelta(seconds=1),
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
        ),
    ]

    turn = project_turn(events)["turn"]

    assert turn["metrics"]["wall_time_ms"] is None
    assert "clock_skew" in turn["data_quality"]


def test_required_diagnostic_metrics_are_projected_without_fabricating_unknowns():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "telemetry.coverage",
            at=start,
            attributes={"area": "usage", "coverage": "complete"},
        ),
        _event(
            "telemetry.coverage",
            at=start,
            attributes={"area": "tools", "coverage": "complete"},
        ),
        _event(
            "telemetry.coverage",
            at=start,
            attributes={"area": "subagents", "coverage": "unsupported"},
        ),
        _event(
            "invocation.created",
            at=start,
            invocation_id="inv_metrics",
            attributes={
                "attempt": 1,
                "spawn_reason": "initial",
                "action": "resume_session",
            },
        ),
        _event(
            "invocation.started",
            at=start,
            invocation_id="inv_metrics",
            attributes={"action": "resume_session"},
        ),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=1),
            invocation_id="inv_metrics",
            model_request_id="mr_1",
            attributes={
                "sequence": 1,
                "work_category": "primary",
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 40,
                "cache_creation_tokens": 10,
                "reasoning_tokens": 5,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "request",
                "usage_source": "fixture",
                "usage_coverage": "complete",
            },
        ),
        _event(
            "tool.call.started",
            at=start + timedelta(seconds=2),
            invocation_id="inv_metrics",
            tool_call_id="tool_1",
            attributes={
                "tool_name": "command_execution",
                "tool_category": "shell",
                "sequence": 1,
            },
        ),
        _event(
            "tool.call.completed",
            at=start + timedelta(seconds=3),
            invocation_id="inv_metrics",
            tool_call_id="tool_1",
            attributes={
                "tool_name": "command_execution",
                "tool_category": "shell",
                "sequence": 1,
                "status": "success",
            },
        ),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=4),
            invocation_id="inv_metrics",
            model_request_id="mr_2",
            attributes={
                "sequence": 2,
                "work_category": "tool_loop",
                "input_tokens": 150,
                "output_tokens": 20,
                "cache_read_tokens": 80,
                "cache_creation_tokens": 0,
                "reasoning_tokens": 5,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "request",
                "usage_source": "fixture",
                "usage_coverage": "complete",
            },
        ),
        _event(
            "process.timeout_detected",
            at=start + timedelta(seconds=4),
            invocation_id="inv_metrics",
            attributes={
                "timeout_kind": "backend_http_timeout",
                "timeout_ms": 1000,
            },
        ),
        _event(
            "invocation.completed",
            at=start + timedelta(seconds=5),
            invocation_id="inv_metrics",
            attributes={"status": "success", "duration_ms": 5000, "exit_code": 0},
        ),
        _event(
            "turn.completed",
            at=start + timedelta(seconds=5),
            invocation_id="inv_metrics",
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
        ),
    ]

    metrics = project_turn(events)["turn"]["metrics"]

    assert metrics["wall_time_ms"] == 5000
    assert metrics["active_invocation_time_ms"] == 5000
    assert metrics["parallelism_factor"] == 1.0
    assert metrics["timeout_count"] == 1
    assert metrics["timeout_counts"] == {
        "gateway": 0,
        "inactivity": 0,
        "http": 1,
        "backend": 0,
    }
    assert metrics["tool_call_count"] == 1
    assert metrics["tool_loop_rounds"] == 1
    assert metrics["tokens_per_tool_call"] == 290.0
    assert metrics["cache_creation_ratio"] == 0.04
    assert metrics["unattributed_token_count"] == 0
    assert metrics["token_work_by_category"] == {
        "primary": 115,
        "tool_loop": 175,
    }
    assert metrics["coverage_score"] == 100.0
    assert metrics["subagent_count"] is None
    assert metrics["context_growth_between_turns"] is None
    assert (
        metrics["context_discontinuity_reason"]
        == "backend_session_identity_unavailable"
    )


def test_aggregate_usage_is_reported_as_unattributed_work():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=1),
            invocation_id="inv_aggregate",
            attributes={
                "input_tokens": 100,
                "output_tokens": 10,
                "reasoning_tokens": 5,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "invocation_total",
                "usage_source": "fixture",
                "usage_coverage": "aggregate_only",
                "work_category": "primary",
            },
        ),
        _event(
            "turn.completed",
            at=start + timedelta(seconds=2),
            invocation_id="inv_aggregate",
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
        ),
    ]

    metrics = project_turn(events)["turn"]["metrics"]

    assert metrics["total_token_work"] == 115
    assert metrics["unattributed_token_count"] == 115
    assert metrics["tool_loop_rounds"] is None


def test_request_usage_takes_precedence_over_codex_session_cumulative_usage():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=1),
            invocation_id="inv_codex",
            attributes={
                "input_tokens": 1000,
                "output_tokens": 20,
                "cache_read_tokens": 800,
                "reasoning_tokens": 5,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "invocation_total",
                "usage_source": "turn.completed.usage",
                "usage_coverage": "aggregate_only",
                "work_category": "primary",
            },
        ),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=2),
            invocation_id="inv_codex",
            model_request_id="mr_codex_1",
            attributes={
                "sequence": 1,
                "input_tokens": 100,
                "output_tokens": 3,
                "cache_read_tokens": 80,
                "reasoning_tokens": 1,
                "context_window_tokens": 500,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "request",
                "usage_source": "codex.rollout.token_count.last_token_usage",
                "usage_coverage": "complete",
                "work_category": "primary",
            },
        ),
        _event(
            "model.session_usage",
            at=start + timedelta(seconds=2),
            invocation_id="inv_codex",
            attributes={
                "input_tokens": 1000,
                "output_tokens": 20,
                "cache_read_tokens": 800,
                "reasoning_tokens": 5,
                "total_tokens": 1020,
                "context_window_tokens": 500,
                "rate_limit_primary_used_percent": 12.0,
            },
        ),
        _event(
            "turn.completed",
            at=start + timedelta(seconds=3),
            invocation_id="inv_codex",
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
        ),
    ]

    metrics = project_turn(events)["turn"]["metrics"]

    assert metrics["input_tokens"] == 100
    assert metrics["total_token_work"] == 104
    assert metrics["aggregate_input_tokens"] == 1000
    assert metrics["session_cumulative_input_tokens"] == 1000
    assert metrics["context_window_tokens"] == 500
    assert metrics["context_used_ratio"] == 0.2
    assert metrics["uncached_input_tokens"] == 20
    assert metrics["rate_limit_primary_used_percent"] == 12.0


def test_duplicate_invocation_exposes_raw_and_deduplicated_work():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "invocation.created",
            at=start,
            invocation_id="inv_original",
            attributes={
                "attempt": 1,
                "spawn_reason": "initial",
                "action": "resume_session",
            },
        ),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=1),
            invocation_id="inv_original",
            model_request_id="mr_original",
            attributes={
                "input_tokens": 100,
                "output_tokens": 10,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "request",
                "usage_source": "fixture",
                "usage_coverage": "complete",
                "work_category": "primary",
            },
        ),
        _event(
            "invocation.created",
            at=start + timedelta(milliseconds=100),
            invocation_id="inv_duplicate",
            attributes={
                "attempt": 1,
                "spawn_reason": "initial",
                "action": "resume_session",
            },
        ),
        _event(
            "invocation.duplicate_detected",
            at=start + timedelta(milliseconds=200),
            invocation_id="inv_duplicate",
            attributes={
                "duplicate_of_invocation_id": "inv_original",
                "confidence": "probable",
                "rule": "session_process_replacement",
            },
        ),
        _event(
            "model.request.usage",
            at=start + timedelta(seconds=2),
            invocation_id="inv_duplicate",
            model_request_id="mr_duplicate",
            attributes={
                "input_tokens": 80,
                "output_tokens": 5,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "request",
                "usage_source": "fixture",
                "usage_coverage": "complete",
                "work_category": "duplicate",
            },
        ),
        _event(
            "turn.completed",
            at=start + timedelta(seconds=3),
            invocation_id="inv_original",
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
        ),
    ]

    metrics = project_turn(events)["turn"]["metrics"]

    assert metrics["raw_total_token_work"] == 195
    assert metrics["deduplicated_total_token_work"] == 110
    assert metrics["total_token_work"] == 195
    assert metrics["duplicate_invocation_count"] == 1
    assert metrics["invocations_per_turn"] == 1


def test_timeout_and_actual_process_exit_remain_separate_facts():
    start = utc_now()
    events = [
        _event("turn.started", at=start),
        _event(
            "invocation.created",
            at=start,
            invocation_id="inv_timeout",
            attributes={
                "attempt": 1,
                "spawn_reason": "initial",
                "action": "run_oneoff",
            },
        ),
        _event(
            "process.spawned",
            at=start,
            invocation_id="inv_timeout",
            attributes={
                "process_instance_id": "proc_timeout",
                "process_role": "agent",
                "executable_name": "codex",
            },
        ),
        _event(
            "turn.timeout_requested",
            at=start + timedelta(seconds=10),
            invocation_id="inv_timeout",
            attributes={"timeout_kind": "gateway_timeout", "timeout_ms": 10000},
        ),
        _event(
            "process.termination_requested",
            at=start + timedelta(seconds=10),
            invocation_id="inv_timeout",
            attributes={"reason_code": "gateway_timeout"},
        ),
        _event(
            "turn.completed",
            at=start + timedelta(seconds=10),
            invocation_id="inv_timeout",
            attributes={
                "status": "timed_out",
                "timeout_status": "gateway_timeout",
                "exit_code": None,
            },
        ),
        _event(
            "process.exited",
            at=start + timedelta(seconds=15),
            invocation_id="inv_timeout",
            attributes={
                "process_instance_id": "proc_timeout",
                "exit_code": -15,
                "signal": 15,
                "duration_ms": 15000,
            },
        ),
    ]

    projection = project_turn(events)
    turn = projection["turn"]
    process = projection["processes"][0]

    assert turn["final_status"] == "timed_out"
    assert turn["timeout_status"] == "gateway_timeout"
    assert turn["final_exit_code"] is None
    assert turn["metrics"]["wall_time_ms"] == 10000
    assert turn["metrics"]["timeout_counts"]["gateway"] == 1
    assert process["ended_at"] > turn["ended_at"]
    assert process["exit_code"] == -15
    assert process["signal"] == 15
