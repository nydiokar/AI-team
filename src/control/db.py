"""
Mesh database — SQLite with WAL mode.

This is the canonical database layer for the agent mesh.  It is the single
place where SQL is written; everything else imports from here.

Design principles
-----------------
- stdlib only (sqlite3).  No ORM.  Schema is simple and stable; an ORM adds
  indirection without benefit and complicates the eventual Postgres migration.
  When Postgres is needed, swap the connection factory and the RETURNING clause
  syntax — everything else is standard SQL.
- WAL mode mandatory.  Multiple workers will poll and claim simultaneously.
- Thread-safe.  `check_same_thread=False` + a module-level threading.Lock for
  writes.  Reads are concurrent; writes are serialised.
- Dual-write safe.  JSON files remain authoritative.  The DB is a mirror that
  shadows every SessionStore.save() and every task dispatch/completion.  The
  `shadow_write` flag in MeshConfig controls whether writes happen at all —
  default True so the DB is always warm when we flip the read source.
- Schema versioned.  A `schema_version` table tracks applied migrations.  New
  columns are added via ALTER TABLE in numbered migration functions so the DB
  upgrades in place without a full rebuild.

Tables
------
sessions        — mirrors state/sessions/*.json exactly; one row per gateway session
mesh_tasks      — dispatch queue; one row per task turn dispatched to a worker
task_events     — append-only event log per session (mirrors logs/session_events/)
nodes           — registered worker nodes (ephemeral; rebuilt from heartbeats)

Future tables (noted, not built yet)
--------------------------------------
task_dependencies  — DAG edges for agent-to-agent autonomous flows
agent_runs         — fine-grained per-tool-call log (dashboard/audit)
"""

import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

# A82 Stage 2 — managed turn-queue typed outcomes. Import at module load (no
# cycle: turn_queue.py imports only pydantic/stdlib). The Pydantic result MODELS
# are imported lazily inside the strict helpers to keep them out of the hot
# legacy import path, but the exception TYPES are needed for `except` clauses.
from .turn_queue import (
    TurnQueueError,
    InvalidCredentialError,
    ScopeForbiddenError,
    TurnNotFoundError,
    OwnershipConflictError,
    ByteCapError,
    MalformedTurnError,
    CapacityError,
    BackingStoreError,
)
if False:  # typing-only forward refs for the strict helper signatures
    from .turn_queue import ClaimToken, StartAuthorization, CompletionResult, RecoveryResolution, TurnAdmission
    from .turn_queue import TurnCancelOutcome, SessionCloseTurns

logger = logging.getLogger(__name__)

_mesh_health_sample_lock = threading.Lock()
_mesh_health_last_sample: Dict[str, float] = {}

# ---------------------------------------------------------------------------
# Schema version — DERIVED from the migration list (see _get_migrations()).
# Defined immediately after that function so it can never drift out of sync
# with the highest migration. Do NOT hand-maintain a literal here.
# ---------------------------------------------------------------------------


class CaseCloseBlocked(Exception):
    """[A37] Raised by ``close_case`` when a Case cannot honestly close yet:
    unresolved required approval, open child work, or unmet/unwaived
    completion_criteria. A structured refusal — NOT a crash. The ``reason`` string
    is safe to surface to the caller/operator."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# FlowRun stage vocabulary (v0.4 §11) — the canonical ordered stage names for a
# dispatch flow. This is a DESCRIPTIVE constant only: nothing reads current_stage
# to decide what runs next (see the flow_runs table note). A19's legacy free-text
# values (e.g. "dispatch_start", "queued") remain valid — current_stage is not
# constrained by a CHECK, so this constant does not reject older/other values.
# ---------------------------------------------------------------------------

FLOW_STAGES = (
    "intent",
    "objective_lock",
    "plan",
    "plan_review",
    "execution",
    "impl_review",
    "closure",
)


# ---------------------------------------------------------------------------
# Work Control Substrate vocabulary (A25, WORK_CONTROL_SUBSTRATE_MILESTONE.md).
# DESCRIPTIVE constants only — like FLOW_STAGES, they document the intended
# vocabulary for callers/tests and are NOT enforced by a CHECK constraint, so
# older/other values remain valid and a bad value can never break a best-effort
# write. flow_links relate a case (flow_run) to entities the gateway already
# owns; flow_events are the append-only case audit trail. Nothing here is read
# to DRIVE execution — this is a RECORD/relationship layer only.
# ---------------------------------------------------------------------------

FLOW_LINK_ENTITY_TYPES = (
    "task",
    "session",
    "approval",
    "artifact",
    "job",
    "flow",
)

FLOW_LINK_ROLES = (
    "root_task",
    "manager",
    "worker",
    "reviewer",
    "approval",
    "artifact",
    "job",
    "child_flow",
    "evidence",
)

FLOW_EVENT_ACTORS = (
    "operator",
    "manager",
    "worker",
    "reviewer",
    "system",
)

FLOW_EVENT_TYPES = (
    "flow.created",
    "flow.stage_changed",
    "flow.status_changed",
    "flow.linked",
    "flow.unlinked",
    "task.dispatched",
    "task.dispatch_voided",
    "worker.wait_pending",
    "worker.wait_resolved",
    "session.attached",
    "approval.requested",
    "approval.resolved",
    "review.requested",
    "review.accepted",
    "review.rework_requested",
    "review.waived",
    "artifact.published",
    "spec.authored",
    "spec.review_scored",
    "case.decomposed",
    "flow.blocked",
    "flow.unblocked",
    "flow.interrupted",
    "flow.superseded",
    "flow.closed",
)


# [M3.2] Manager review verdict → canonical review.* flow_event type. The three
# target types are ALREADY reserved in FLOW_EVENT_TYPES above (no new schema). This
# single map is the source of truth the orchestrator seam + control API route agree on.
REVIEW_VERDICT_EVENT_TYPES = {
    "accepted": "review.accepted",
    "rework_requested": "review.rework_requested",
    "waived": "review.waived",
}
# The set of review.* event types the close-gate scans for the LATEST verdict.
_REVIEW_EVENT_TYPES = frozenset(REVIEW_VERDICT_EVENT_TYPES.values())

_TRUTHY_FLAG_VALUES = ("1", "true", "yes", "on")

# Write-transaction acquisition resilience. The worker daemon shares this SQLite
# file cross-process, so BEGIN IMMEDIATE can raise "database is locked" once the
# per-statement busy_timeout is exhausted. A dropped control-plane write shows up
# as lost/dishonest state (a result never recorded), so the transaction START is
# retried with bounded backoff before giving up. Only acquisition is retried —
# nothing has been written yet, so it is idempotent.
# [A82 Stage 4a] The managed open-row predicate, spelled EXACTLY like the
# `idx_mesh_turns_session_open` partial-index WHERE clause so SQLite can prove
# the index usable (partial-index use requires the index terms verbatim).
_MANAGED_OPEN_PREDICATE = (
    "queue_protocol = 1 "
    "AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')"
)

_WRITE_BEGIN_MAX_ATTEMPTS = 4
_WRITE_BEGIN_BACKOFF_SEC = 0.1
_BUSY_TIMEOUT_MS = 15000

# A65 cost read-model. Usage sources that record ``input_token_semantics =
# 'includes_cache'`` — ``input_tokens`` ALREADY CONTAINS the cached-input portion
# (codex ``last_token_usage`` / codex ``turn.completed`` aggregates), so the cost
# read-model must subtract ``cache_read_tokens`` from ``input`` or it double-counts
# the cached tokens. Claude (``claude.result.usage``) is recorded exclusive-cache
# (input excludes the cache) and is left untouched. Proven against the live DB:
# every row from the inclusive sources satisfies input >= cache_read.
_INCLUSIVE_CACHE_SOURCES: Tuple[str, ...] = (
    "codex.rollout.token_count.last_token_usage",
    "turn.completed.usage",
)

# SQL expression that yields the NON-double-counted uncached input for one
# llm_model_requests row (used by every cost aggregation).
_COST_INPUT_EXPR = (
    "CASE WHEN r.input_token_semantics = 'includes_cache'"
    " AND r.usage_source IN ('codex.rollout.token_count.last_token_usage',"
    " 'turn.completed.usage')"
    " THEN MAX(COALESCE(r.input_tokens, 0) - COALESCE(r.cache_read_tokens, 0), 0)"
    " ELSE COALESCE(r.input_tokens, 0) END"
)

# Cost-explorer GROUP BY targets. Values are evaluated against the cost query's
# aliases (s=session row, t=turn row, r=request row). ``model`` keeps the raw
# (possibly empty) model so the assembler can price and label honestly.
_COST_DIMENSIONS: Dict[str, str] = {
    "project": "COALESCE(NULLIF(s.repo_path, ''), '<no repo path>')",
    "backend": "COALESCE(NULLIF(t.backend, ''), s.backend, 'unknown')",
    "model": "COALESCE(NULLIF(r.model, ''), '')",
    "role": "COALESCE(NULLIF(s.case_role, ''), 'standalone')",
    "case": "COALESCE(NULLIF(s.current_case_id, ''), 'standalone')",
    "session": "t.session_id",
}

RUNTIME_FLAG_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "HARNESS_FLOW_DRIVE": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Work Control Substrate flow_links/flow_events write path.",
    },
    "REVIEW_EMITTER_ENABLED": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Manager review verdict emitter and unresolved-rework close gate.",
    },
    "DURABLE_RELAY_ENABLED": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Durable worker wait markers and wait reconciliation routes.",
    },
    "CASE_CONTINUATION_ENABLED": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Wake-Dispatcher autonomous Case continuation.",
    },
    "SPEC_AUTHORING_ENABLED": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "M4 spec-authoring stage, scored spec-review gate, publish_artifact, and the decomposer.",
    },
    "CASE_RESPAWN_REQUIRES_APPROVAL": {
        "default": "1",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Gate M3.4 crash-respawn (a dead Manager session on a satisfied Case) behind an operator approval instead of auto-spawning. Quota-caused deaths wait for confirmed-restored telemetry before even requesting approval.",
    },
    "CASE_QUOTA_RESUME_ENABLED": {
        "default": "1",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Record a Manager Case as quota-PAUSED when its turn dies on usage_limit, and propose a resume as soon as quota telemetry says the window reopened. OFF ⇒ no pause record, no proposal (pre-feature behaviour: the Case only ever resumes if a wait-group happens to satisfy later).",
    },
    "CASE_QUOTA_RESUME_AUTO": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "On quota restore, resume the paused Case immediately instead of asking the operator. Default OFF: resuming a fat Manager session re-writes its whole prompt cache (observed 200-300k tokens), so spending that is an operator decision. When ON, the env knob CASE_QUOTA_RESUME_AUTO_MAX_USD (0 = no ceiling) still holds back any resume whose estimated cost exceeds it.",
    },
    "TRANSIENT_PROVIDER_RESUME_ENABLED": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Self-heal a Manager Case whose turn died on a terminal transient provider 5xx (e.g. Anthropic 529 Overloaded, error_class=upstream_error) after in-process burst retries were exhausted. When ON: the session stays AWAITING_INPUT (not ERROR), the Case is recorded as transient-PAUSED with a short escalating fixed backoff (30/60/120/300s), and the Wake-Dispatcher auto-retries the failed turn once the backoff elapses — bounded (4 attempts per 15-min window, then flow.transient_pause_exhausted + escalate). Default OFF: a rare failure the operator wants to observe before enabling. OFF ⇒ transient turns fail as before (session ERROR), byte-identical.",
    },
    "CACHE_HEARTBEAT_OBSERVE": {
        "default": "1",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Observe session-cache heartbeat candidates for durable waits/jobs without sending paid heartbeat turns. Default ON so long waits can be measured immediately.",
    },
    "CACHE_HEARTBEAT_ACTIVE": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Send paid session-cache heartbeat turns for eligible observed controllers. Default OFF; switch ON to move from observe-only to acting.",
    },
    "MANAGER_ADVANCEMENT_GATE": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Refuse a Manager close_case unless the Case ledger proves the work was advanced (a second dispatch or a rework verdict) OR an explicit exhaustion_attestation is recorded. Turns 'judge contribution + continue the work' from prose into an enforced close gate.",
    },
    "COST_ALERT_ENFORCE_ENABLED": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "A65 cost-alert enforcement: when ON, alerting surfaces the existing SDK governor ceiling (sdk_max_budget_usd) as the lever. Never a new kill mechanism.",
    },
    "HARNESS_LEVEL3_GUARD": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Admission backstop requiring explicit approval for Level-3 auto-picked tasks.",
    },
    "RESTART_CONTEXT_RESTORE_DISABLED": {
        "default": "0",
        "effect_scope": "live",
        "registry_writable": "1",
        "description": "Disable bounded prior-context injection after a worker restart loses SDK state.",
    },
    "MANAGER_ROLE_ENABLED": {
        "default": "0",
        "effect_scope": "session_boot",
        "registry_writable": "1",
        "description": "Manager role path, role prompt, and scoped Manager tool grants for new sessions.",
    },
    "MANAGER_TOOLS_ENABLED": {
        "default": "0",
        "effect_scope": "session_boot",
        "registry_writable": "1",
        "description": "Legacy process-wide Manager MCP tool grant for new Claude sessions.",
    },
    "CONTROL_API_DOCS": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "1",
        "description": "Expose FastAPI docs/openapi routes when the control API is built.",
    },
    "QUOTA_COORDINATOR_ENABLED": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "1",
        "description": "Observe-only quota coordinator construction at gateway startup.",
    },
    "QUOTA_PREWARM_ENABLED": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "1",
        "description": "Keep the Claude 5-hour window ticking: when telemetry shows NO open window, spend one minimal haiku turn to start one, then verify against the provider that a window actually opened. Runs around the clock on purpose — an already-running window is only worth anything before work starts. Requires QUOTA_COORDINATOR_ENABLED (it reads and activates through the coordinator's adapter). Bounded by QUOTA_PREWARM_MAX_PER_DAY / QUOTA_PREWARM_MIN_INTERVAL_SEC and a consecutive-failure circuit breaker.",
    },
    "QUOTA_DIGEST_TELEGRAM_ENABLED": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "1",
        "description": "Temporary Telegram digest for quota coordinator observations.",
    },
    "CLAUDE_SKIP_PERMISSIONS": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "0",
        "description": "Claude CLI permission bypass mode from process environment.",
    },
    "CONTROL_API_ENABLED": {
        "default": "1",
        "effect_scope": "bootstrap",
        "registry_writable": "0",
        "description": "Start the gateway Control API. Bootstrap/env-only because disabling it removes this API.",
    },
    "GUARDED_WRITE": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "0",
        "description": "System guarded-write mode from process environment.",
    },
    "MESH_ENABLED": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "0",
        "description": "Enable mesh routing through worker nodes.",
    },
    "MESH_EMBEDDED_SERVER": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "0",
        "description": "Run the mesh task server embedded in the gateway process.",
    },
    "MESH_SHADOW_WRITE": {
        "default": "1",
        "effect_scope": "bootstrap",
        "registry_writable": "0",
        "description": "Mirror sessions/tasks into the mesh DB; disabling can make the registry unavailable.",
    },
    "OPENCODE_SERVER_ENABLED": {
        "default": "0",
        "effect_scope": "startup",
        "registry_writable": "0",
        "description": "Legacy OpenCode server mode compatibility flag.",
    },
    "TELEMETRY_ENABLED": {
        "default": "1",
        "effect_scope": "startup",
        "registry_writable": "0",
        "description": "Enable durable LLM telemetry collection.",
    },
    "TELEMETRY_DETAILED_EVENTS": {
        "default": "1",
        "effect_scope": "startup",
        "registry_writable": "0",
        "description": "Enable detailed telemetry event capture.",
    },
    "WORKER_ACCEPT_UNPINNED": {
        "default": "1",
        "effect_scope": "worker_startup",
        "registry_writable": "0",
        "description": "Worker daemon accepts tasks not pinned to a specific node.",
    },
    "WORKER_CANARY": {
        "default": "0",
        "effect_scope": "worker_startup",
        "registry_writable": "0",
        "description": "Mark a worker process as canary in its advertised live state.",
    },
    "WORKER_REAP_STALE_SESSIONS": {
        "default": "1",
        "effect_scope": "worker_startup",
        "registry_writable": "0",
        "description": "Worker boot reaps stale backend child processes from a previous incarnation.",
    },
}


def _truthy_flag(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in _TRUTHY_FLAG_VALUES


def runtime_flag_registry_writable(flag_name: str) -> bool:
    definition = RUNTIME_FLAG_DEFINITIONS.get(flag_name)
    return bool(definition) and _truthy_flag(definition.get("registry_writable"))


def _runtime_flag_row(flag_name: str, db: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    if not runtime_flag_registry_writable(flag_name):
        return None
    try:
        flag_db = db if db is not None else get_db()
        if flag_db is None:
            return None
        return flag_db.get_runtime_flag(flag_name)
    except Exception as e:
        logger.warning("event=runtime_flag_read_failed flag=%s err=%s", flag_name, e)
        return None


def runtime_flag_enabled(flag_name: str) -> bool:
    """Return a registry-over-env boolean for a known runtime flag.

    DB rows are operator/agent overrides and win immediately at call sites that
    consult this function per request/tick. Missing rows fall back to the current
    process environment, preserving existing defaults and `.env` deployments.
    """
    definition = RUNTIME_FLAG_DEFINITIONS.get(flag_name)
    if definition is None:
        raise ValueError(f"unknown runtime flag: {flag_name}")
    row = _runtime_flag_row(flag_name) if runtime_flag_registry_writable(flag_name) else None
    if row is not None:
        return _truthy_flag(str(row.get("value") or ""))
    return _truthy_flag(os.environ.get(flag_name, definition["default"]))


def render_runtime_flag(flag_name: str, db: Optional[Any] = None) -> Dict[str, Any]:
    definition = RUNTIME_FLAG_DEFINITIONS[flag_name]
    writable = runtime_flag_registry_writable(flag_name)
    row = _runtime_flag_row(flag_name, db=db) if writable else None
    env_raw = os.environ.get(flag_name)
    if row is not None:
        source = "registry"
        raw_value = str(row.get("value") or "")
    elif env_raw is not None:
        source = "env"
        raw_value = env_raw
    else:
        source = "default"
        raw_value = definition["default"]
    return {
        "flag_name": flag_name,
        "value": _truthy_flag(raw_value),
        "raw_value": "1" if _truthy_flag(raw_value) else "0",
        "source": source,
        "effect_scope": definition["effect_scope"],
        "registry_writable": writable,
        "description": definition["description"],
        "registry": row,
        "env_value": env_raw,
    }


def review_emitter_enabled() -> bool:
    """[M3.2] Whether the review.* verdict emitter is active (slice 1).

    Canonical read of ``REVIEW_EMITTER_ENABLED`` (truthy: 1/true/yes/on); default
    OFF. Mirrors ``flow_drive_enabled()``. When OFF: the /api/cases/{id}/review route
    returns 404 AND ``close_case`` skips the unresolved-rework gate ⇒ byte-identical
    to pre-M3.2 behavior.
    """
    return runtime_flag_enabled("REVIEW_EMITTER_ENABLED")


def manager_advancement_gate_enabled() -> bool:
    """Whether the Manager advancement close-gate is active.

    Canonical read of ``MANAGER_ADVANCEMENT_GATE`` (truthy: 1/true/yes/on);
    default OFF. Mirrors ``review_emitter_enabled()``. When OFF: ``close_case``
    applies no advancement check ⇒ byte-identical to the prior behaviour (a
    manager close still needs only a non-empty ``continuation_plan``). When ON:
    a ``actor='manager'`` close is refused unless the Case ledger shows the work
    was actually advanced (≥2 ``task.dispatched`` events, or any
    ``review.rework_requested``) OR the caller records an explicit
    ``exhaustion_attestation`` — making 'stop here' a deliberate, recorded
    decision rather than a momentum default on a single accepted worker.
    """
    return runtime_flag_enabled("MANAGER_ADVANCEMENT_GATE")


def durable_relay_enabled() -> bool:
    """[A46/M3.3] Whether the durable worker-wait relay is active.

    Canonical read of ``DURABLE_RELAY_ENABLED`` (truthy: 1/true/yes/on); default
    OFF. Mirrors ``review_emitter_enabled()``. When OFF: ``record_worker_wait``
    writes no pending marker and ``reconcile_worker_waits`` no-ops ⇒ byte-identical
    to pre-A46 behavior (no ``worker.wait_*`` events are ever written, and the two
    /api/cases/{id}/waits routes return 404).
    """
    return runtime_flag_enabled("DURABLE_RELAY_ENABLED")


def case_continuation_enabled() -> bool:
    """[M3.4] Whether autonomous Case continuation (the Wake-Dispatcher) is active.

    Canonical read of ``CASE_CONTINUATION_ENABLED`` (truthy: 1/true/yes/on);
    default OFF. Mirrors ``durable_relay_enabled()``. When OFF: ``arm_wait_group``
    writes nothing and the orchestrator Wake-Dispatcher tick is a no-op ⇒ no
    ``cont:*`` rows are enqueued and no proactive wake turns are delivered ⇒
    byte-identical to pre-M3.4 behavior.
    """
    return runtime_flag_enabled("CASE_CONTINUATION_ENABLED")


def case_respawn_requires_approval() -> bool:
    """[Case-respawn approval gate] Whether M3.4 crash-respawn requires an
    operator-resolved approval before spawning a fresh Manager session.

    Canonical read of ``CASE_RESPAWN_REQUIRES_APPROVAL`` (truthy: 1/true/yes/on);
    default ON (unlike most flags here — silent/inconsistent auto-respawn was the
    reported problem this gate exists to fix). When OFF, the dead-session branch
    of the Wake-Dispatcher tick spawns immediately, byte-identical to pre-gate
    M3.4 Job 3 behavior.
    """
    return runtime_flag_enabled("CASE_RESPAWN_REQUIRES_APPROVAL")


def case_quota_resume_enabled() -> bool:
    """[quota-resume] Whether a Manager Case is recorded as quota-PAUSED and gets
    a resume proposal when the window reopens.

    Canonical read of ``CASE_QUOTA_RESUME_ENABLED``; default ON — a Case silently
    stalling until some unrelated worker finish happened to satisfy a wait-group
    was the reported defect. OFF ⇒ no ``flow.quota_paused`` is written and the
    quota branch of the Wake-Dispatcher tick returns immediately, byte-identical
    to pre-feature behaviour.
    """
    return runtime_flag_enabled("CASE_QUOTA_RESUME_ENABLED")


def case_quota_resume_auto() -> bool:
    """[quota-resume] Whether a restored quota resumes the paused Case WITHOUT an
    operator approval.

    Canonical read of ``CASE_QUOTA_RESUME_AUTO``; default OFF (the resume spends
    real money re-writing the session's prompt cache — see
    ``CASE_QUOTA_RESUME_AUTO_MAX_USD``).
    """
    return runtime_flag_enabled("CASE_QUOTA_RESUME_AUTO")


def case_quota_resume_auto_max_usd() -> float:
    """[quota-resume] Ceiling (USD) under which an auto-resume may fire without
    asking. ``0`` (the default) means "no ceiling" — every restore auto-resumes
    when ``CASE_QUOTA_RESUME_AUTO`` is ON.

    An env knob rather than a registry flag because the registry is boolean-only
    (numeric knobs are A62's scope); a malformed value degrades to 0.0 rather
    than raising into a tick.
    """
    try:
        return max(0.0, float(os.environ.get("CASE_QUOTA_RESUME_AUTO_MAX_USD", "0") or 0))
    except (TypeError, ValueError):
        return 0.0


def transient_provider_resume_enabled() -> bool:
    """[transient-resume] Whether a Manager Case whose turn died on a terminal
    transient provider 5xx (e.g. Anthropic 529 Overloaded ⇒ ``error_class ==
    upstream_error``) self-heals via a short-backoff auto-retry.

    Canonical read of ``TRANSIENT_PROVIDER_RESUME_ENABLED``; default OFF. When OFF
    the session-status transient branch, ``_record_transient_pause`` and the
    Wake-Dispatcher transient branch are all no-ops ⇒ the turn fails exactly as it
    did before the feature (session ERROR), byte-identical. Distinct from the
    quota seam: a 529 carries no ``resetsAt`` and reopens in seconds-to-minutes,
    so restore is a fixed escalating backoff, not telemetry, and the retry is
    automatic and free (no cache-rewrite cost estimate, no operator approval).
    """
    return runtime_flag_enabled("TRANSIENT_PROVIDER_RESUME_ENABLED")


def cache_heartbeat_observe_enabled() -> bool:
    """Whether heartbeat producers should record durable observe-only intent."""
    return runtime_flag_enabled("CACHE_HEARTBEAT_OBSERVE")


def cache_heartbeat_active_enabled() -> bool:
    """Whether due eligible heartbeat controllers may send paid keepalive turns."""
    return runtime_flag_enabled("CACHE_HEARTBEAT_ACTIVE")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _parse_datetime_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def cache_heartbeat_ttl_sec() -> int:
    return _env_int("CACHE_HEARTBEAT_TTL_SEC", 3600, 60)


def cache_heartbeat_interval_sec() -> int:
    ttl = cache_heartbeat_ttl_sec()
    configured = _env_int("CACHE_HEARTBEAT_INTERVAL_SEC", 2700, 60)
    return min(configured, max(60, int(ttl * 0.75)))


def cache_heartbeat_max_beats_default() -> int:
    return _env_int("CACHE_HEARTBEAT_MAX_BEATS_DEFAULT", 6, 1)


def cache_heartbeat_hard_max_beats() -> int:
    return _env_int("CACHE_HEARTBEAT_MAX_BEATS_HARD", 15, 1)


def cache_heartbeat_min_cache_tokens() -> int:
    return _env_int("CACHE_HEARTBEAT_MIN_CACHE_TOKENS", 100_000, 0)


def spec_authoring_enabled() -> bool:
    """[A56/M4] Whether the spec-authoring stage + scored review gate + decomposer
    are active.

    Canonical read of ``SPEC_AUTHORING_ENABLED`` (truthy: 1/true/yes/on); default
    OFF. Mirrors ``case_continuation_enabled()``. When OFF: ``publish_artifact``,
    ``publish_spec``, ``record_spec_review`` and ``decompose_case`` all write
    nothing and return a disabled marker, and their /api routes return 404 ⇒
    byte-identical to pre-M4 behaviour (no ``artifact.*``/``spec.*``/``case.decomposed``
    events are ever written and no ``task_attached`` links are created).
    """
    return runtime_flag_enabled("SPEC_AUTHORING_ENABLED")


# [A56/M4] R1 — the scored spec-review rubric. Six dimensions, each scored 0–2 by a
# SEPARATE plan-reviewer seat (never the authoring Manager grading its own spec). A
# spec PASSES (⇒ decomposition is allowed) only when BOTH hold:
#   * the total score meets SPEC_REVIEW_PASS_THRESHOLD (≥8/12), AND
#   * neither of the two CRITICAL dimensions scored a hard zero (a spec with an
#     unclear objective or one that cannot be decomposed is unusable regardless of a
#     high total — a critical-zero BLOCKS even above threshold).
# These are tunable config constants, not magic numbers scattered at call sites.
SPEC_REVIEW_DIMENSIONS: tuple = (
    "objective_clarity",
    "scope_boundaries",
    "decomposability",
    "acceptance_testability",
    "dependency_correctness",
    "risks_and_assumptions",
)
SPEC_REVIEW_MAX_PER_DIM = 2
SPEC_REVIEW_MAX_SCORE = SPEC_REVIEW_MAX_PER_DIM * len(SPEC_REVIEW_DIMENSIONS)  # 12
SPEC_REVIEW_PASS_THRESHOLD = 8
# A hard zero on either of these BLOCKS decomposition even if the total clears the
# threshold — they are load-bearing for a decomposable, actionable spec.
SPEC_REVIEW_CRITICAL_DIMENSIONS: tuple = ("objective_clarity", "decomposability")


def _score_spec_review(scores: Dict[str, Any]) -> Dict[str, Any]:
    """[A56/M4] Grade a spec-review score-card against R1. Pure/no I/O.

    ``scores`` maps each of ``SPEC_REVIEW_DIMENSIONS`` to an int in
    [0, SPEC_REVIEW_MAX_PER_DIM]. Returns a structured verdict:
    ``{"total", "max", "threshold", "passed", "verdict", "critical_zero": [...],
    "missing": [...], "out_of_range": [...]}``. ``passed`` is True iff the total
    meets the threshold AND no critical dimension scored zero. A malformed
    score-card (missing/out-of-range dims) never passes and is reported explicitly.
    """
    missing: List[str] = []
    out_of_range: List[str] = []
    total = 0
    critical_zero: List[str] = []
    for dim in SPEC_REVIEW_DIMENSIONS:
        raw = scores.get(dim)
        if raw is None:
            missing.append(dim)
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            out_of_range.append(dim)
            continue
        if val < 0 or val > SPEC_REVIEW_MAX_PER_DIM:
            out_of_range.append(dim)
            continue
        total += val
        if val == 0 and dim in SPEC_REVIEW_CRITICAL_DIMENSIONS:
            critical_zero.append(dim)
    well_formed = not missing and not out_of_range
    passed = bool(
        well_formed
        and total >= SPEC_REVIEW_PASS_THRESHOLD
        and not critical_zero
    )
    return {
        "total": total,
        "max": SPEC_REVIEW_MAX_SCORE,
        "threshold": SPEC_REVIEW_PASS_THRESHOLD,
        "passed": passed,
        "verdict": "accepted" if passed else "rework_requested",
        "critical_zero": critical_zero,
        "missing": missing,
        "out_of_range": out_of_range,
    }


def _topological_order(
    keys: List[str],
    deps: Dict[str, List[str]],
) -> Optional[List[str]]:
    """[A56/M4] Kahn's topological sort of a task-DAG. Pure/no I/O.

    ``deps[key]`` are the task_keys ``key`` DEPENDS ON (its prerequisites, which
    must run first). Returns one valid schedule (prerequisites before dependents),
    or ``None`` if the graph contains a cycle (⇒ unschedulable). Deterministic:
    ties are broken by the original ``keys`` order so two runs agree.
    """
    indegree: Dict[str, int] = {k: 0 for k in keys}
    dependents: Dict[str, List[str]] = {k: [] for k in keys}
    for k in keys:
        for prereq in deps.get(k, []):
            indegree[k] += 1
            dependents[prereq].append(k)
    ready = [k for k in keys if indegree[k] == 0]
    order: List[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for child in dependents[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return order if len(order) == len(keys) else None


# [M3.4] The reserved ``machine_id`` sentinel that keeps a continuation row
# STRUCTURALLY invisible to every worker/embedded claim scan. The worker scan
# filters ``WHERE status='pending' AND (machine_id IS NULL OR machine_id = ?)``;
# this sentinel is neither NULL nor any real ``node_id``, so no worker can ever
# claim it. ``action`` is the Wake-Dispatcher's discriminator for the row.
CONTINUATION_MACHINE_SENTINEL = "__manager_continuation__"
CONTINUATION_ACTION = "manager_continuation"
# [A55 / M3.4 Job 3] The crash-respawn single-flight action. A respawn row rides
# the SAME reserved ``CONTINUATION_MACHINE_SENTINEL`` (so it stays invisible to
# every worker/embedded claim scan) and the SAME ``claim_task``/reaper mechanism
# as the continuation lease — NO second lock model. Only the ``action`` and the
# id namespace differ so a respawn token never collides with a ``cont:`` wake row.
RESPAWN_ACTION = "manager_respawn"
# [quota-resume] The quota-restore resume single-flight action. Same reserved
# sentinel + same ``claim_task`` lease as the two above (no third lock model);
# only the action and id namespace differ. The lease is what makes the AUTOMATIC
# resume and the operator's manual "Resume now" button structurally unable to
# start two Managers on one Case — they both claim this one row.
QUOTA_RESUME_ACTION = "manager_quota_resume"
# [transient-resume] The transient-5xx self-heal retry single-flight action. Same
# reserved sentinel + same ``claim_task`` lease as the three above (no new lock
# model); only the action and id namespace differ. The lease keeps two overlapping
# Wake-Dispatcher passes from delivering the same retry turn twice.
TRANSIENT_RESUME_ACTION = "manager_transient_resume"
# [session-cache-heartbeat] The heartbeat single-flight action. It uses a
# distinct sentinel so worker scans never claim heartbeat lease rows as normal
# work; the gateway claims the row before sending a paid heartbeat turn.
CACHE_HEARTBEAT_MACHINE_SENTINEL = "__cache_heartbeat__"
CACHE_HEARTBEAT_ACTION = "cache_heartbeat"
# [A82 Stage 4d] Producer-token rows that may be linked to a managed turn in the
# admission txn, and the sentinel owner the link stamps (claimed_at NULL ⇒ the
# legacy lease reaper never re-offers a linked token).
PRODUCER_TOKEN_SENTINELS = {
    "manager_continuation": "__manager_continuation__",
    "cache_heartbeat": "__cache_heartbeat__",
}
# Default round cap when a Case's completion_criteria does not carry an explicit
# ``round_cap`` — a backstop against a runaway continuation loop, not a tuning knob.
DEFAULT_CONTINUATION_ROUND_CAP = 50


def continuation_task_id(case_id: str, generation: int) -> str:
    """[M3.4] Deterministic id for a Case's generation-N continuation row.

    Deterministic ⇒ two racing Wake-Dispatcher ticks that both see the SAME
    satisfaction compute the SAME id, so the ``UNIQUE constraint`` on
    ``mesh_tasks.id`` collapses them to one row and the atomic ``claim_task``
    elects a single winner. The generation is parsed back off the id suffix.
    """
    return f"cont:{case_id}:{int(generation)}"


def respawn_task_id(case_id: str, generation: int) -> str:
    """[A55 / M3.4 Job 3] Deterministic id for a Case's generation-N crash-respawn
    single-flight token.

    Deterministic ⇒ two racing Wake-Dispatcher ticks that both find the SAME
    dead-session Case at the SAME satisfaction generation compute the SAME id, so
    the ``UNIQUE constraint`` on ``mesh_tasks.id`` collapses them to one row and
    the atomic ``claim_task`` elects a single respawn winner. Distinct namespace
    from :func:`continuation_task_id` so a respawn token never collides with the
    ``cont:`` wake-delivery row for the same (case, generation).
    """
    return f"respawn:{case_id}:{int(generation)}"


def producer_turn_id(
    trigger_key: str, session_id: str, attempt: int = 1, *, prefix: str = "cturn",
) -> str:
    """[A82 Stage 4c] Deterministic managed-turn id for a producer trigger.

    ``trigger_key`` is the producer's durable trigger identity (a Case
    continuation token id ``cont:{case}:{generation}``), ``session_id`` the
    recipient and ``attempt`` the token's durable attempt counter (bumped only
    when a linked turn ended WITHOUT consuming the trigger — withdrawn or
    cancelled). A crash retry after the token claim therefore rediscovers the
    SAME id instead of minting a random one (design §7). Pure.

    [A82 Stage 4d] ``prefix`` names the producer family (``jturn`` watched-job
    notification, ``hturn`` cache heartbeat); the digest is the same function."""
    digest = hashlib.sha256(
        f"{session_id}\0{trigger_key}\0{int(attempt)}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{prefix}_{digest}"


# [A82 Stage 4c] Managed turn outcomes that CONSUME a continuation trigger
# (the Manager ran the wake: round counted, presented work consumed — legacy
# `_finalize_continuation` parity, which also consumed on failure). Any other
# terminal outcome (withdrawn = obsolete/closed, cancelled = operator stop)
# re-arms the token for a fresh attempt without counting a round.
PRODUCER_CONSUMING_STATUSES = ("completed", "failed", "failed_node_offline")


def quota_resume_task_id(case_id: str, paused_task_id: str) -> str:
    """[quota-resume] Deterministic single-flight token for resuming ONE quota
    pause of a Case.

    Keyed on the PAUSED TASK, not on a generation: a quota pause is not a
    continuation round (no wait-group satisfaction happened), and the round
    generation does not advance while the Case sits paused — keying on it would
    let the same pause be resumed again and again. One pause ⇒ one resume, for
    every entry point (auto-restore, approval, operator button), because all of
    them claim this row.
    """
    return f"qresume:{case_id}:{paused_task_id}"


def transient_resume_task_id(case_id: str, paused_task_id: str, attempt: int) -> str:
    """[transient-resume] Deterministic single-flight token for auto-retrying ONE
    transient-5xx pause of a Case.

    Keyed on the paused task AND the attempt number: unlike a quota pause (one
    pause ⇒ one resume), a transient pause can legitimately retry several times in
    a row (each failed retry opens a fresh pause at the next attempt), so the
    token must be distinct per attempt or the second retry would collide with the
    first's completed row and never claim. Two overlapping ticks at the SAME
    attempt compute the SAME id ⇒ the UNIQUE constraint + atomic claim elect one
    winner.
    """
    return f"tresume:{case_id}:{paused_task_id}:{int(attempt)}"


def cache_heartbeat_task_id(session_id: str, slot_epoch: int) -> str:
    """Deterministic lease id for one session-cache heartbeat due slot."""
    return f"cachehb:{session_id}:{int(slot_epoch)}"


def flow_drive_enabled() -> bool:
    """Whether the Work Control Substrate write path is active (A22/A26/A29).

    Canonical read of ``HARNESS_FLOW_DRIVE`` (truthy: 1/true/yes/on); default OFF.
    This is the SINGLE source of truth for the flag so every substrate writer
    (orchestrator flow seams, ApprovalService approval seams) agrees byte-for-byte.
    When OFF, no flow_links/flow_events are written ⇒ behavior is byte-identical to
    A19. It never reads a substrate row to DRIVE execution — it only gates RECORDS.
    """
    return runtime_flag_enabled("HARNESS_FLOW_DRIVE")


def manager_role_enabled() -> bool:
    """Registry-over-env read of ``MANAGER_ROLE_ENABLED``.

    The API gate observes this immediately. Role prompts and scoped MCP grants
    are session-boot decisions, so existing sessions do not gain/lose tools until
    their next boot.
    """
    return runtime_flag_enabled("MANAGER_ROLE_ENABLED")


def manager_tools_enabled() -> bool:
    """Registry-over-env read of ``MANAGER_TOOLS_ENABLED``.

    This controls the legacy Claude Manager MCP tool grant when a session boots;
    changing it does not mutate an already-running SDK session's allowed tools.
    """
    return runtime_flag_enabled("MANAGER_TOOLS_ENABLED")


def harness_level3_guard_enabled() -> bool:
    """Registry-over-env read of ``HARNESS_LEVEL3_GUARD``."""
    return runtime_flag_enabled("HARNESS_LEVEL3_GUARD")


def restart_context_restore_disabled() -> bool:
    """Registry-over-env read of ``RESTART_CONTEXT_RESTORE_DISABLED``."""
    return runtime_flag_enabled("RESTART_CONTEXT_RESTORE_DISABLED")


def control_api_docs_enabled() -> bool:
    """Registry-over-env read of ``CONTROL_API_DOCS`` at API construction time."""
    return runtime_flag_enabled("CONTROL_API_DOCS")


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Gateway sessions — mirrors state/sessions/*.json
-- Kept in sync by SessionStore via shadow-write.
-- DO NOT add columns that are not also in the Session dataclass unless they
-- are mesh-layer concerns (e.g. node routing).
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    backend             TEXT NOT NULL,
    repo_path           TEXT NOT NULL,
    status              TEXT NOT NULL,       -- idle|busy|awaiting_input|error|cancelled|closed
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    machine_id          TEXT NOT NULL DEFAULT '',
    backend_session_id  TEXT NOT NULL DEFAULT '',
    -- NOTE: `model` column is added by migration 11 (not here) so fresh and
    -- existing DBs converge through the same ALTER. See _get_migrations().
    last_task_id        TEXT NOT NULL DEFAULT '',
    last_artifact_path  TEXT NOT NULL DEFAULT '',
    last_summary        TEXT NOT NULL DEFAULT '',
    last_user_message   TEXT NOT NULL DEFAULT '',
    last_result_summary TEXT NOT NULL DEFAULT '',
    last_files_modified TEXT NOT NULL DEFAULT '[]',  -- JSON array
    telegram_chat_id    INTEGER,
    telegram_thread_id  INTEGER,
    owner_user_id       INTEGER,
    task_history        TEXT NOT NULL DEFAULT '[]'   -- JSON array of task history dicts
    -- NOTE: `model` (migration 11) and `origin` (migration 12, {"channel","kind"}
    -- JSON) are added by ALTER, not here, so fresh and existing DBs converge
    -- through the same migration path. See _get_migrations().
);

-- Mesh task queue — one row per task dispatch turn.
-- status lifecycle: pending → claimed → completed | failed | failed_node_offline
-- The `payload` column holds the full dispatch context as JSON:
--   {session: <Session dict>, prompt: str, task_id: str, action: str}
-- The `result` column holds ExecutionResult as JSON on completion.
-- `parent_task_id` enables dependency chains (agent-to-agent flows).
CREATE TABLE IF NOT EXISTS mesh_tasks (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT,               -- NULL for run_oneoff tasks
    machine_id          TEXT,               -- NULL = any capable node; set on dispatch
    backend             TEXT NOT NULL,
    action              TEXT NOT NULL,      -- create_session|resume_session|run_oneoff|cancel|compact_session
    payload             TEXT NOT NULL,      -- JSON: {session?, prompt, task_id, action, metadata?}
    status              TEXT NOT NULL DEFAULT 'pending',
    claimed_by          TEXT,               -- node_id
    claimed_at          TEXT,
    completed_at        TEXT,
    result              TEXT,               -- JSON: ExecutionResult on completion
    error               TEXT,               -- error message on failure (non-JSON for readability)
    artifact_path       TEXT,               -- pointer to results/{task_id}.json
    parent_task_id      TEXT,               -- for dependency chains (future: task_dependencies table)
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_mesh_tasks_status_machine
    ON mesh_tasks(status, machine_id);

CREATE INDEX IF NOT EXISTS idx_mesh_tasks_session
    ON mesh_tasks(session_id);

CREATE INDEX IF NOT EXISTS idx_mesh_tasks_created
    ON mesh_tasks(created_at);

-- A81: composite for the transcript read (WHERE session_id=? ORDER BY created_at
-- ASC) — a covered range scan instead of filter-on-session then sort.
CREATE INDEX IF NOT EXISTS idx_mesh_tasks_session_created
    ON mesh_tasks(session_id, created_at);

-- Append-only event log per session — mirrors logs/session_events/{session_id}.log
-- Kept as a table so the dashboard can query "all events for session X" without
-- parsing NDJSON files.
CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    success     INTEGER NOT NULL,           -- 0 | 1
    execution_time REAL,
    error       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_task_events_session
    ON task_events(session_id);

-- Registered worker nodes — ephemeral; rebuilt from heartbeats on restart.
-- The VPS node_registry keeps an in-memory copy; this table is the persistent
-- backing store so /nodes Telegram command works after a VPS restart.
CREATE TABLE IF NOT EXISTS nodes (
    node_id             TEXT PRIMARY KEY,
    tailscale_ip        TEXT NOT NULL DEFAULT '',
    api_port            INTEGER NOT NULL DEFAULT 9001,
    backends            TEXT NOT NULL DEFAULT '[]',  -- JSON array of backend names
    max_concurrent      INTEGER NOT NULL DEFAULT 2,
    status              TEXT NOT NULL DEFAULT 'online',  -- online|offline
    last_heartbeat      TEXT NOT NULL,
    registered_at       TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Agent-operable feature flags. A row is an override; absence falls back to the
-- process environment and then each flag's compiled default.
CREATE TABLE IF NOT EXISTS runtime_flags (
    flag_name TEXT PRIMARY KEY,
    value     TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'api',
    set_at    TEXT NOT NULL,
    set_by    TEXT NOT NULL DEFAULT ''
);

-- Watched jobs — orthogonal to mesh_tasks/session lifecycle.
-- A job is a long-lived external process monitored by the worker's
-- _job_watcher_loop.  It does NOT hold a task semaphore slot, does NOT
-- keep a session BUSY, and does NOT enter the stale-busy reattach loop.
-- Operational mesh health history. Aggregate telemetry, not task lifecycle.
CREATE TABLE IF NOT EXISTS mesh_health_samples (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at                  TEXT NOT NULL,
    source                      TEXT NOT NULL,
    sessions_busy               INTEGER NOT NULL DEFAULT 0,
    tasks_pending               INTEGER NOT NULL DEFAULT 0,
    tasks_claimed               INTEGER NOT NULL DEFAULT 0,
    nodes_online                INTEGER NOT NULL DEFAULT 0,
    nodes_total                 INTEGER NOT NULL DEFAULT 0,
    slots_used                  INTEGER NOT NULL DEFAULT 0,
    slots_total                 INTEGER NOT NULL DEFAULT 0,
    slots_available             INTEGER NOT NULL DEFAULT 0,
    active_tasks                INTEGER NOT NULL DEFAULT 0,
    stale_busy_sessions         INTEGER NOT NULL DEFAULT 0,
    nodes_with_live_state       INTEGER NOT NULL DEFAULT 0,
    nodes_without_live_state    INTEGER NOT NULL DEFAULT 0,
    stale_live_state_nodes_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_mesh_health_samples_sampled_at
    ON mesh_health_samples(sampled_at);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    session_id      TEXT,
    node_id         TEXT NOT NULL,
    label           TEXT NOT NULL,
    command         TEXT,
    pid             INTEGER,
    pgid            INTEGER,
    started_at      TEXT NOT NULL,
    started_epoch   REAL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | lost
    exit_code       INTEGER,
    log_path        TEXT,
    tail            TEXT,
    notify          INTEGER NOT NULL DEFAULT 1,
    notify_agent    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_node_status
    ON jobs(node_id, status);

CREATE INDEX IF NOT EXISTS idx_jobs_session
    ON jobs(session_id);

CREATE TABLE IF NOT EXISTS session_cache_heartbeats (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    ttl_sec INTEGER NOT NULL,
    interval_sec INTEGER NOT NULL,
    next_due_at TEXT,
    expires_at TEXT,
    beat_count INTEGER NOT NULL DEFAULT 0,
    max_beats INTEGER NOT NULL,
    hard_max_beats INTEGER NOT NULL,
    last_beat_task_id TEXT,
    last_cache_touch_at TEXT,
    last_cache_read_tokens INTEGER,
    last_cache_creation_tokens INTEGER,
    circuit_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_cache_heartbeats_active
    ON session_cache_heartbeats(session_id)
    WHERE status IN ('observe_only', 'active');
CREATE INDEX IF NOT EXISTS idx_session_cache_heartbeats_due
    ON session_cache_heartbeats(status, next_due_at);

CREATE TABLE IF NOT EXISTS session_cache_heartbeat_owners (
    id TEXT PRIMARY KEY,
    heartbeat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_runtime_sec INTEGER,
    started_at TEXT NOT NULL,
    expires_at TEXT,
    stop_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_cache_heartbeat_owners_active
    ON session_cache_heartbeat_owners(session_id, reason, owner_type, owner_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_session_cache_heartbeat_owners_hb
    ON session_cache_heartbeat_owners(heartbeat_id, status);

-- FlowRun record (v0.4 §13 item 1) — one row per dispatch flow.
-- This is a RECORD, not a stage machine: nothing reads current_stage to decide
-- what runs next. Written best-effort by the orchestrator at dispatch-start and
-- updated at a stage transition; a write failure never affects task execution.
CREATE TABLE IF NOT EXISTS flow_runs (
    flow_run_id     TEXT PRIMARY KEY,
    task_id         TEXT,
    current_stage   TEXT,
    objective_lock  TEXT,
    created_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_flow_runs_task
    ON flow_runs(task_id);
"""

_APPROVALS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    session_id   TEXT,
    task_id      TEXT,
    action       TEXT NOT NULL,
    risk         TEXT NOT NULL DEFAULT 'medium',
    reversible   INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL DEFAULT '',
    resolved_by  TEXT,
    payload      TEXT,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id)
"""

_LLM_TELEMETRY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT,
    task_id TEXT NOT NULL,
    gateway_node_id TEXT,
    execution_node_id TEXT,
    backend TEXT,
    backend_session_id_start TEXT,
    backend_session_id_end TEXT,
    requested_model TEXT,
    observed_models TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    ended_at TEXT,
    final_status TEXT NOT NULL DEFAULT 'running',
    timeout_status TEXT NOT NULL DEFAULT 'none',
    final_exit_code INTEGER,
    final_invocation_id TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    projection_version INTEGER NOT NULL DEFAULT 1,
    events_pruned_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_invocations (
    invocation_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    parent_invocation_id TEXT,
    retry_of_invocation_id TEXT,
    duplicate_of_invocation_id TEXT,
    attempt INTEGER NOT NULL,
    spawn_reason TEXT NOT NULL,
    action TEXT NOT NULL,
    node_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    requested_model TEXT,
    observed_model TEXT,
    process_instance_id TEXT,
    pid INTEGER,
    process_started_at TEXT,
    started_at TEXT,
    ended_at TEXT,
    status TEXT NOT NULL,
    timeout_kind TEXT,
    exit_code INTEGER,
    signal INTEGER,
    retry_reason TEXT,
    model_request_count INTEGER,
    tool_call_count INTEGER,
    subagent_count INTEGER,
    usage_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(turn_id) REFERENCES llm_turns(turn_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS llm_processes (
    process_instance_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    pid INTEGER,
    parent_process_instance_id TEXT,
    process_role TEXT NOT NULL,
    backend TEXT,
    executable_name TEXT,
    started_at TEXT,
    ended_at TEXT,
    exit_code INTEGER,
    signal INTEGER,
    status TEXT NOT NULL,
    data_quality_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS llm_invocation_processes (
    invocation_id TEXT NOT NULL,
    process_instance_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    PRIMARY KEY(invocation_id, process_instance_id),
    FOREIGN KEY(invocation_id) REFERENCES llm_invocations(invocation_id) ON DELETE CASCADE,
    FOREIGN KEY(process_instance_id) REFERENCES llm_processes(process_instance_id)
);
CREATE TABLE IF NOT EXISTS llm_model_requests (
    model_request_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    provider_request_id TEXT,
    model TEXT,
    work_category TEXT NOT NULL DEFAULT 'unknown',
    started_at TEXT,
    ended_at TEXT,
    status TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    reasoning_tokens INTEGER,
    context_tokens INTEGER,
    input_token_semantics TEXT NOT NULL DEFAULT 'unknown',
    usage_granularity TEXT NOT NULL,
    usage_source TEXT,
    usage_coverage TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    data_quality_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(invocation_id) REFERENCES llm_invocations(invocation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS llm_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    event_time TEXT NOT NULL,
    observed_time TEXT NOT NULL,
    node_id TEXT NOT NULL,
    emitter_process_instance_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_sequence INTEGER,
    clock_quality TEXT NOT NULL DEFAULT 'unknown',
    session_id TEXT,
    turn_id TEXT NOT NULL,
    invocation_id TEXT,
    model_request_id TEXT,
    tool_call_id TEXT,
    subagent_id TEXT,
    backend TEXT,
    model TEXT,
    pid INTEGER,
    attributes TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_events_turn
    ON llm_events(turn_id, event_time, source_sequence);
CREATE INDEX IF NOT EXISTS idx_llm_events_invocation
    ON llm_events(invocation_id, event_time);
CREATE INDEX IF NOT EXISTS idx_llm_events_session
    ON llm_events(session_id, event_time);
CREATE INDEX IF NOT EXISTS idx_llm_events_name
    ON llm_events(event_name, event_time);
CREATE INDEX IF NOT EXISTS idx_llm_invocations_turn
    ON llm_invocations(turn_id, attempt);
CREATE INDEX IF NOT EXISTS idx_llm_model_requests_turn
    ON llm_model_requests(turn_id, invocation_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_model_provider_request
    ON llm_model_requests(invocation_id, provider_request_id)
    WHERE provider_request_id IS NOT NULL;
-- Per-session turn lookup. Without this, any "this session's turns" read
-- (recent_cache_write on the UI-polled resume-state path, cost reads, timeline)
-- has no way into llm_turns by session_id and SQLite falls back to a full scan
-- of llm_model_requests. With it, the resume-cost estimate is an index-driven
-- read over just this session's turns, never the whole telemetry table.
CREATE INDEX IF NOT EXISTS idx_llm_turns_session
    ON llm_turns(session_id, ended_at)
"""


# ---------------------------------------------------------------------------
# MeshDB — the public interface
# ---------------------------------------------------------------------------

class MeshDB:
    """SQLite-backed mesh database.

    Thread safety: reads are fully concurrent; writes acquire `_write_lock`.
    All public methods are synchronous and safe to call from any thread
    (including asyncio.to_thread wrappers).

    Usage::

        db = MeshDB("state/mesh.db")
        db.upsert_session(session)
        db.enqueue_task(task_id, session_id, backend, action, payload_dict)
        rows = db.get_pending_tasks(node_id="main-pc", backends=["claude"])
    """

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._local = threading.local()   # per-thread connection cache
        self._init_schema()
        # [A82 Stage 4a rework] Process-level "any session enrolled" presence so
        # the legacy path does no enrollment-marker read while none exists.
        self._any_enrolled: Optional[bool] = None
        self._enroll_generation = 0
        self._enrolls_in_flight = 0
        self._presence_lock = threading.Lock()
        self.refresh_enrollment_presence()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return a per-thread cached connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                isolation_level=None,   # autocommit; we manage transactions explicitly
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS};")
            conn.execute("PRAGMA foreign_keys=ON;")
            # I/O tuning: in WAL, synchronous=NORMAL is the recommended durable
            # setting (only a power/OS crash can lose the last txn, not an app
            # crash) and removes an fsync per commit — the main write-stall source.
            # A negative cache_size is KiB; mmap lets reads hit the OS page cache
            # without per-page syscalls, which is what cold reads on the large
            # file were paying for.
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA cache_size=-8000;")       # ~8 MB page cache / connection
            conn.execute("PRAGMA mmap_size=268435456;")    # 256 MB memory-mapped reads
            conn.execute("PRAGMA wal_autocheckpoint=400;")  # ~1.6 MB WAL before auto-checkpoint
            self._local.conn = conn
        return conn

    def _begin_immediate(self, conn: sqlite3.Connection) -> None:
        """Acquire a write transaction, retrying transient cross-process locks.

        Retries only the BEGIN (nothing is written yet, so it is idempotent) on
        "database is locked"/"busy" with escalating backoff, then re-raises so a
        genuine, sustained lock still surfaces instead of hanging forever.
        """
        last_exc: Optional[sqlite3.OperationalError] = None
        for attempt in range(1, _WRITE_BEGIN_MAX_ATTEMPTS + 1):
            try:
                conn.execute("BEGIN IMMEDIATE;")
                return
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                last_exc = exc
                if attempt < _WRITE_BEGIN_MAX_ATTEMPTS:
                    logger.warning(
                        "event=db_write_begin_retry attempt=%d/%d err=%s",
                        attempt, _WRITE_BEGIN_MAX_ATTEMPTS, exc,
                    )
                    time.sleep(_WRITE_BEGIN_BACKOFF_SEC * attempt)
        logger.error(
            "event=db_write_begin_exhausted attempts=%d err=%s",
            _WRITE_BEGIN_MAX_ATTEMPTS, last_exc,
        )
        assert last_exc is not None
        raise last_exc

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection, None, None]:
        """Serialised write context. Yields a connection inside a transaction."""
        conn = self._conn()
        with self._write_lock:
            self._begin_immediate(conn)
            try:
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    @contextmanager
    def _managed_write(
        self, op: str, deadline_sec: float = 5.0,
    ) -> Generator[sqlite3.Connection, None, None]:
        """[A82 Stage 4a] Bounded write transaction for queue mutations (design §8
        "Time"): ONE monotonic deadline covers the in-process write lock AND the
        SQLite lock (busy_timeout = remaining time, single BEGIN, none of the
        legacy 4x15 s retry path). Exhausting it raises a typed 503
        ``BackingStoreError`` — nothing was written, so a caller can never be
        told "accepted". The thread-local busy_timeout is restored afterwards.
        COMMIT runs before control returns to the caller, so an acknowledgement
        built after this block always refers to a committed row."""
        end = time.monotonic() + max(0.0, deadline_sec)
        if not self._write_lock.acquire(timeout=max(0.0, deadline_sec)):
            raise BackingStoreError(
                f"managed {op} deadline exceeded waiting for the write lock", op=op,
            )
        try:
            conn = self._conn()
            remaining_ms = max(1, int((end - time.monotonic()) * 1000))
            conn.execute(f"PRAGMA busy_timeout={remaining_ms};")
            try:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                except sqlite3.OperationalError as exc:
                    raise BackingStoreError(
                        f"managed {op} deadline exceeded acquiring the database lock: {exc}",
                        op=op,
                    )
                try:
                    yield conn
                    conn.execute("COMMIT;")
                except BaseException:
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
            finally:
                try:
                    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS};")
                except Exception:
                    pass
        finally:
            self._write_lock.release()

    def checkpoint_wal(self, mode: str = "PASSIVE") -> Optional[tuple]:
        """Checkpoint the WAL to bound its on-disk growth. Best-effort.

        PASSIVE never blocks readers/writers — it checkpoints what it can and
        returns immediately, so a maintenance tick can't stall the hot path
        (TRUNCATE, by contrast, waits for readers and can add periodic stalls).
        autocheckpoint keeps the WAL bounded between ticks.
        """
        try:
            row = self._conn().execute(f"PRAGMA wal_checkpoint({mode});").fetchone()
            return tuple(row) if row is not None else None
        except Exception:
            logger.debug("event=wal_checkpoint_failed mode=%s", mode, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Schema init + migrations
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        # executescript() issues a COMMIT before running, so we cannot wrap
        # it in our BEGIN IMMEDIATE context manager.  Run DDL directly then
        # handle migrations (which use plain execute) under the write lock.
        conn = self._conn()
        conn.executescript(_DDL)
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._run_migrations(conn)
                self._ensure_merged_schema(conn)
                self._ensure_substrate_columns(conn)
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise

    def _ensure_substrate_columns(self, conn: sqlite3.Connection) -> None:
        """Add the A25 optional convenience `flow_run_id` columns idempotently.

        These are pure conveniences (they remove repeated joins on hot paths);
        they are NOT replacements for flow_links/flow_events. Done here rather
        than in the raw migration so a DB that legitimately lacks an optional
        table (or a partial/hand-built one) can never abort the migration — the
        add is skipped if the table is absent or the column already exists.
        Additive + NULLable; existing writers are unaffected.
        """
        for table in ("mesh_tasks", "approvals"):
            try:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if "flow_run_id" not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN flow_run_id TEXT")
            except Exception as e:
                logger.warning("event=substrate_column_add_failed table=%s err=%s", table, e)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply any pending numbered migrations in order."""
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0

        migrations = _get_migrations()
        for version, sql in migrations:
            if version > current:
                # executescript() issues an implicit COMMIT before running —
                # even for an empty/no-op script — which would terminate the
                # BEGIN IMMEDIATE transaction this method runs inside. Skip it
                # for baseline markers (empty SQL) and use plain execute()
                # for real migrations instead so the transaction stays intact.
                if sql.strip():
                    for statement in filter(None, (s.strip() for s in sql.split(";"))):
                        conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )
                logger.info("event=db_migration_applied version=%d", version)

    def _ensure_merged_schema(self, conn: sqlite3.Connection) -> None:
        """Repair the merged Web UI/main schema across divergent migration 13s.

        Web UI used migration 13 for approvals; main used migration 13 for LLM
        telemetry and then 14/15 for telemetry columns. This idempotent pass
        preserves both lineages without relying on a DB having taken only one
        exact branch path.
        """
        for sql in (_APPROVALS_SCHEMA_SQL, _LLM_TELEMETRY_SCHEMA_SQL):
            for statement in filter(None, (s.strip() for s in sql.split(";"))):
                conn.execute(statement)
        self._add_column_if_missing(
            conn,
            "llm_events",
            "clock_quality",
            "TEXT NOT NULL DEFAULT 'unknown'",
        )
        self._add_column_if_missing(conn, "llm_turns", "events_pruned_at", "TEXT")

    def _add_column_if_missing(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    # ------------------------------------------------------------------
    # Runtime flags
    # ------------------------------------------------------------------

    def get_runtime_flag(self, flag_name: str) -> Optional[Dict[str, Any]]:
        name = (flag_name or "").strip().upper()
        if name not in RUNTIME_FLAG_DEFINITIONS:
            return None
        row = self._conn().execute(
            """
            SELECT flag_name, value, source, set_at, set_by
            FROM runtime_flags
            WHERE flag_name = ?
            """,
            (name,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_runtime_flags(self) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT flag_name, value, source, set_at, set_by
            FROM runtime_flags
            ORDER BY flag_name
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def set_runtime_flag(
        self,
        flag_name: str,
        enabled: bool,
        *,
        source: str = "api",
        set_by: str = "",
    ) -> Dict[str, Any]:
        name = (flag_name or "").strip().upper()
        if name not in RUNTIME_FLAG_DEFINITIONS:
            raise ValueError(f"unknown runtime flag: {flag_name}")
        if not runtime_flag_registry_writable(name):
            raise ValueError(f"runtime flag is not registry-writable: {flag_name}")
        val = "1" if bool(enabled) else "0"
        src = (source or "api").strip()[:32] or "api"
        actor = (set_by or "").strip()[:128]
        now = _now()
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO runtime_flags(flag_name, value, source, set_at, set_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(flag_name) DO UPDATE SET
                    value = excluded.value,
                    source = excluded.source,
                    set_at = excluded.set_at,
                    set_by = excluded.set_by
                """,
                (name, val, src, now, actor),
            )
        row = self.get_runtime_flag(name)
        if row is None:
            raise RuntimeError(f"runtime flag write did not persist: {name}")
        return row

    def delete_runtime_flag(self, flag_name: str) -> bool:
        name = (flag_name or "").strip().upper()
        if name not in RUNTIME_FLAG_DEFINITIONS:
            raise ValueError(f"unknown runtime flag: {flag_name}")
        if not runtime_flag_registry_writable(name):
            raise ValueError(f"runtime flag is not registry-writable: {flag_name}")
        with self._write() as conn:
            cur = conn.execute("DELETE FROM runtime_flags WHERE flag_name = ?", (name,))
            return int(cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def upsert_session(self, session: Any) -> None:
        """Mirror a Session dataclass into the sessions table."""
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, backend, repo_path, status,
                        created_at, updated_at, machine_id, backend_session_id, model,
                        effort,
                        last_task_id, last_artifact_path, last_summary,
                        last_user_message, last_result_summary, last_files_modified,
                        telegram_chat_id, telegram_thread_id, owner_user_id, task_history,
                        origin,
                        driver_type, driver_status, cache_health, cache_unhealthy_count,
                        previous_backend_session_ids,
                        current_case_id, case_role, role_boot, continued_from,
                        keep_pinned, keep_note
                    ) VALUES (
                        :session_id, :backend, :repo_path, :status,
                        :created_at, :updated_at, :machine_id, :backend_session_id, :model,
                        :effort,
                        :last_task_id, :last_artifact_path, :last_summary,
                        :last_user_message, :last_result_summary, :last_files_modified,
                        :telegram_chat_id, :telegram_thread_id, :owner_user_id, :task_history,
                        :origin,
                        :driver_type, :driver_status, :cache_health, :cache_unhealthy_count,
                        :previous_backend_session_ids,
                        :current_case_id, :case_role, :role_boot, :continued_from,
                        :keep_pinned, :keep_note
                    )
                    ON CONFLICT(session_id) DO UPDATE SET
                        backend             = excluded.backend,
                        repo_path           = excluded.repo_path,
                        status              = excluded.status,
                        updated_at          = excluded.updated_at,
                        machine_id          = excluded.machine_id,
                        backend_session_id  = excluded.backend_session_id,
                        model               = excluded.model,
                        effort              = excluded.effort,
                        last_task_id        = excluded.last_task_id,
                        last_artifact_path  = excluded.last_artifact_path,
                        last_summary        = excluded.last_summary,
                        last_user_message   = excluded.last_user_message,
                        last_result_summary = excluded.last_result_summary,
                        last_files_modified = excluded.last_files_modified,
                        telegram_chat_id    = excluded.telegram_chat_id,
                        telegram_thread_id  = excluded.telegram_thread_id,
                        owner_user_id       = excluded.owner_user_id,
                        task_history        = excluded.task_history,
                        origin              = excluded.origin,
                        driver_type                  = excluded.driver_type,
                        driver_status                = excluded.driver_status,
                        cache_health                 = excluded.cache_health,
                        cache_unhealthy_count        = excluded.cache_unhealthy_count,
                        previous_backend_session_ids = excluded.previous_backend_session_ids,
                        keep_pinned                 = excluded.keep_pinned,
                        keep_note                   = excluded.keep_note
                        -- NB: current_case_id / case_role are DELIBERATELY NOT updated
                        -- here. A generic full-session save (e.g. a Manager's own
                        -- turn-end persist of a stale in-memory object) must NEVER
                        -- clobber Case affiliation — that is a read-modify-write race
                        -- that silently re-attached a session to a closed Case. These
                        -- two columns are owned exclusively by the authoritative
                        -- affiliation seam `set_session_case` (open_case sets, close_case
                        -- clears). The INSERT above still seeds them for a brand-new row.
                        -- role_boot is likewise INSERT-seeded ONLY: it is a fixed boot
                        -- decision stamped at session-create time and read-only after, so
                        -- a later full-upsert of a stale object cannot flip a session's
                        -- tier. Absent ⇒ NULL ⇒ tier-0 default (byte-identical).
                        -- continued_from is the same: session lineage is fixed at fork
                        -- time, so it is INSERT-seeded ONLY and never updated here.
                    """,
                    {
                        "session_id":          session.session_id,
                        "backend":             session.backend,
                        "repo_path":           session.repo_path,
                        "status":              session.status.value if hasattr(session.status, "value") else session.status,
                        "created_at":          session.created_at,
                        "updated_at":          session.updated_at,
                        "machine_id":          session.machine_id or "",
                        "backend_session_id":  session.backend_session_id or "",
                        "model":               getattr(session, "model", None),
                        "effort":              getattr(session, "effort", None),
                        "last_task_id":        session.last_task_id or "",
                        "last_artifact_path":  session.last_artifact_path or "",
                        "last_summary":        session.last_summary or "",
                        "last_user_message":   session.last_user_message or "",
                        "last_result_summary": session.last_result_summary or "",
                        "last_files_modified": json.dumps(session.last_files_modified or []),
                        "telegram_chat_id":    session.telegram_chat_id,
                        "telegram_thread_id":  session.telegram_thread_id,
                        "owner_user_id":       session.owner_user_id,
                        "task_history":        json.dumps(session.task_history or []),
                        "origin":              _origin_json(getattr(session, "origin", None)),
                        "driver_type":           getattr(session, "driver_type", "") or "",
                        "driver_status":         getattr(session, "driver_status", "") or "",
                        "cache_health":          getattr(session, "cache_health", "unknown") or "unknown",
                        "cache_unhealthy_count": int(getattr(session, "cache_unhealthy_count", 0) or 0),
                        "previous_backend_session_ids": json.dumps(getattr(session, "previous_backend_session_ids", None) or []),
                        "current_case_id":       getattr(session, "current_case_id", None) or None,
                        "case_role":             getattr(session, "case_role", None) or None,
                        "role_boot":             getattr(session, "role_boot", None) or None,
                        "continued_from":        getattr(session, "continued_from", None) or None,
                        "keep_pinned":           1 if bool(getattr(session, "keep_pinned", False)) else 0,
                        "keep_note":             getattr(session, "keep_note", "") or "",
                    },
                )
        except Exception as e:
            logger.warning("event=db_upsert_session_failed session_id=%s err=%s", session.session_id, e)

    def set_session_case(
        self,
        session_id: str,
        case_id: Optional[str],
        role: Optional[str] = None,
    ) -> None:
        """Authoritative, targeted write of a session's Case affiliation.

        The ONLY sanctioned path that mutates ``sessions.current_case_id`` /
        ``case_role`` — a scoped UPDATE of exactly those two columns (plus
        ``updated_at``), so it cannot be undone by, and cannot disturb, a
        concurrent full-session ``upsert_session`` (which no longer touches these
        columns on conflict). ``case_id=None`` clears the affiliation (Case close);
        a non-empty ``case_id`` sets it (Case open / join). No-op for a blank
        session_id or an unknown row. Never raises."""
        sid = (session_id or "").strip()
        if not sid:
            return
        cid = (case_id or None) or None
        rol = (role or None) if cid else None  # role is meaningless without a Case
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE sessions SET current_case_id = ?, case_role = ?, "
                    "updated_at = ? WHERE session_id = ?",
                    (cid, rol, _now(), sid),
                )
        except Exception as e:
            logger.warning(
                "event=set_session_case_failed session_id=%s case_id=%s err=%s",
                sid, cid, e,
            )

    def mark_driver_sessions_lost_for_node(self, node_id: str, *, backend: str = "claude") -> int:
        """Mark idle live SDK-backed sessions on a restarted worker as lost."""
        try:
            with self._write() as conn:
                cur = conn.execute(
                    """
                    UPDATE sessions
                    SET driver_status = 'lost', updated_at = ?
                    WHERE machine_id = ?
                      AND backend = ?
                      AND driver_type = 'sdk'
                      AND driver_status = 'live'
                      AND status IN ('idle', 'awaiting_input')
                    """,
                    (_now(), node_id, backend),
                )
                return int(cur.rowcount or 0)
        except Exception as e:
            logger.warning("event=db_mark_driver_sessions_lost_failed node_id=%s err=%s", node_id, e)
            return 0

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(
        self,
        status: Optional[str] = None,
        backend: Optional[str] = None,
        machine_id: Optional[str] = None,
        keep_pinned: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if backend:
            clauses.append("backend = ?")
            params.append(backend)
        if machine_id:
            clauses.append("machine_id = ?")
            params.append(machine_id)
        if keep_pinned is not None:
            clauses.append("COALESCE(keep_pinned, 0) = ?")
            params.append(1 if keep_pinned else 0)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM sessions {where} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    _EMPTY_CLOSED_SESSION_PREDICATE = """
        s.status = 'closed'
        AND COALESCE(s.backend_session_id, '') = ''
        AND COALESCE(s.last_task_id, '') = ''
        AND COALESCE(s.last_artifact_path, '') = ''
        AND COALESCE(s.last_summary, '') = ''
        AND COALESCE(s.last_user_message, '') = ''
        AND COALESCE(s.last_result_summary, '') = ''
        AND COALESCE(s.last_files_modified, '[]') IN ('', '[]')
        AND COALESCE(s.task_history, '[]') IN ('', '[]')
        AND COALESCE(s.current_case_id, '') = ''
        AND COALESCE(s.keep_pinned, 0) = 0
        AND NOT EXISTS (SELECT 1 FROM mesh_tasks t WHERE t.session_id = s.session_id)
        AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.session_id = s.session_id)
        AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.session_id = s.session_id)
        AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.session_id = s.session_id)
        AND NOT EXISTS (SELECT 1 FROM llm_turns lt WHERE lt.session_id = s.session_id)
        AND NOT EXISTS (SELECT 1 FROM llm_events le WHERE le.session_id = s.session_id)
        AND NOT EXISTS (
            SELECT 1 FROM flow_links fl
            WHERE fl.entity_type = 'session' AND fl.entity_id = s.session_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM flow_events fe
            WHERE fe.entity_type = 'session' AND fe.entity_id = s.session_id
        )
    """

    def list_empty_closed_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Closed sessions that have no conversation/evidence rows anywhere.

        This is intentionally stricter than "closed with no task_id": any
        historical reference in the task/event/job/approval/telemetry/Case ledgers
        makes the session ineligible for pruning.
        """
        rows = self._conn().execute(
            f"""
            SELECT s.*
            FROM sessions s
            WHERE {self._EMPTY_CLOSED_SESSION_PREDICATE}
            ORDER BY s.updated_at ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_empty_closed_sessions(self, session_ids: List[str]) -> List[str]:
        """Delete still-empty closed session rows, rechecking eligibility."""
        ids: list[str] = [sid.strip() for sid in session_ids if sid and sid.strip()]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._write() as conn:
            rows = conn.execute(
                f"""
                SELECT s.session_id
                FROM sessions s
                WHERE s.session_id IN ({placeholders})
                  AND {self._EMPTY_CLOSED_SESSION_PREDICATE}
                """,
                ids,
            ).fetchall()
            eligible: list[str] = [str(r["session_id"]) for r in rows]
            if not eligible:
                return []
            delete_placeholders = ", ".join("?" for _ in eligible)
            conn.execute(
                f"DELETE FROM sessions WHERE session_id IN ({delete_placeholders})",
                eligible,
            )
            return eligible

    def list_stale_busy_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return BUSY sessions with no pending or claimed mesh task.

        This is the gateway-side M3 reconciliation query. A session is considered
        stale-busy when the gateway still marks it busy but the dispatch ledger has
        no active task for that session. Completed/failed historical rows do not
        count as active work.
        """
        rows = self._conn().execute(
            """
            SELECT s.*
            FROM sessions s
            WHERE s.status = 'busy'
              AND NOT EXISTS (
                SELECT 1
                FROM mesh_tasks t
                WHERE t.session_id = s.session_id
                  AND t.status IN ('pending', 'claimed')
              )
            ORDER BY s.updated_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_idle_warm_workers(self, idle_before_iso: str, limit: int = 100) -> List[Dict[str, Any]]:
        """[A60] Return warm WORKER sessions eligible for the idle-reaper.

        A session qualifies when ALL hold: ``case_role = 'worker'`` · status is
        ``idle`` or ``awaiting_input`` (never ``busy`` — a mid-turn worker is
        never reaped) · ``updated_at`` is older than ``idle_before_iso`` · its
        Case is either unset (``current_case_id IS NULL``) or closed
        (``flow_runs.status`` in ``_CLOSED_STATUSES``). A worker still joined to
        an OPEN Case is excluded by the join predicate, not filtered after the
        fact — mirrors ``list_stale_busy_sessions``' shape.
        """
        placeholders = ",".join("?" for _ in self._CLOSED_STATUSES)
        rows = self._conn().execute(
            f"""
            SELECT s.*
            FROM sessions s
            LEFT JOIN flow_runs fr ON fr.flow_run_id = s.current_case_id
            WHERE s.case_role = 'worker'
              AND s.status IN ('idle', 'awaiting_input')
              AND s.updated_at < ?
              AND (s.current_case_id IS NULL OR fr.status IN ({placeholders}))
            ORDER BY s.updated_at ASC
            LIMIT ?
            """,
            (idle_before_iso, *self._CLOSED_STATUSES, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Mesh tasks
    # ------------------------------------------------------------------

    def enqueue_task(
        self,
        task_id: str,
        session_id: Optional[str],
        machine_id: Optional[str],
        backend: str,
        action: str,
        payload: Dict[str, Any],
        artifact_path: Optional[str] = None,
        parent_task_id: Optional[str] = None,
    ) -> None:
        """Insert a new pending task into the dispatch queue."""
        now = _now()
        prompt: str | None = payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO mesh_tasks (
                        id, session_id, machine_id, backend, action,
                        payload, prompt, status, artifact_path, parent_task_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        session_id,
                        machine_id,
                        backend,
                        action,
                        json.dumps(payload),
                        prompt,
                        artifact_path,
                        parent_task_id,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: mesh_tasks.id" in str(e):
                # Idempotent — task already exists (e.g. duplicate dispatch on retry)
                logger.debug("event=db_enqueue_task_duplicate task_id=%s", task_id)
            else:
                logger.warning("event=db_enqueue_task_integrity_failed task_id=%s err=%s", task_id, e)
        except Exception as e:
            logger.warning("event=db_enqueue_task_failed task_id=%s err=%s", task_id, e)

    def record_proactive_turn(
        self,
        task_id: str,
        session_id: str,
        backend: str,
        machine_id: Optional[str],
        reply_text: str,
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist an autonomous (background-job continuation) turn as a first-
        class, already-completed conversation turn.

        Unlike a normal turn there is no user prompt — the agent produced this on
        its own after a run_in_background job finished. It is stored with
        ``action='proactive_turn'`` and an empty prompt so the transcript renders
        it as an assistant-only message. Everything lives in the DB (no artifact
        file), the same as an enriched normal turn.
        """
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO mesh_tasks (
                        id, session_id, machine_id, backend, action,
                        payload, status, prompt, reply_text, usage_json,
                        files_modified_json, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, 'proactive_turn', '{}', 'completed',
                              '', ?, ?, '[]', ?, ?, ?)
                    """,
                    (
                        task_id,
                        session_id,
                        machine_id,
                        backend,
                        reply_text,
                        json.dumps(usage) if usage else None,
                        now,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: mesh_tasks.id" in str(e):
                logger.debug("event=db_record_proactive_duplicate task_id=%s", task_id)
            else:
                logger.warning("event=db_record_proactive_integrity_failed task_id=%s err=%s", task_id, e)
        except Exception as e:
            logger.warning("event=db_record_proactive_failed task_id=%s err=%s", task_id, e)

    def record_audit_turn(
        self,
        task_id: str,
        session_id: str,
        machine_id: Optional[str],
        backend: str,
        action: str,
        payload: Dict[str, Any],
        prompt: str,
        result: Dict[str, Any],
        *,
        success: bool,
        error: str = "",
    ) -> None:
        """[A82 Stage 4d] Persist an ALREADY-TERMINAL protocol-0 audit turn
        (e.g. a watched-job notification record into an enrolled session) in ONE
        insert — never a claimable ``pending`` row, so no legacy carrier can pick
        it up as execution. Idempotent on the id; best-effort like the legacy
        record it replaces (an audit write, not an execution)."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO mesh_tasks (
                        id, session_id, machine_id, backend, action, payload,
                        prompt, status, result, error, created_at, updated_at,
                        completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id, session_id, machine_id, backend, action,
                        json.dumps(payload), prompt,
                        "completed" if success else "failed",
                        json.dumps(result), None if success else error,
                        now, now, now,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_record_audit_turn_failed task_id=%s err=%s", task_id, e)

    def claim_task(self, task_id: str, node_id: str) -> bool:
        """Atomically claim a pending task. Returns True if claim succeeded."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'claimed', claimed_by = ?, claimed_at = ?, updated_at = ?,
                        claimer_incarnation = (SELECT incarnation_id FROM nodes WHERE node_id = ?)
                    WHERE id = ? AND status = 'pending' AND COALESCE(queue_protocol, 0) = 0
                    """,
                    (node_id, now, now, node_id, task_id),
                )
                return conn.execute(
                    "SELECT changes()"
                ).fetchone()[0] > 0
        except Exception as e:
            logger.warning("event=db_claim_task_failed task_id=%s err=%s", task_id, e)
            return False

    def release_task(self, task_id: str, node_id: str) -> bool:
        """Release a claimed task back to pending. Only succeeds if claimed_by matches.

        Returns True if the task was released. Used by workers on graceful shutdown
        and by the stale-claim reaper to reclaim orphaned tasks.
        """
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                        claimer_incarnation = NULL, updated_at = ?
                    WHERE id = ? AND claimed_by = ? AND status = 'claimed' AND COALESCE(queue_protocol, 0) = 0
                    """,
                    (now, task_id, node_id),
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception as e:
            logger.warning("event=db_release_task_failed task_id=%s err=%s", task_id, e)
            return False

    def release_node_claims(self, node_id: str) -> List[str]:
        """Release all claimed tasks for node_id back to pending. Returns released task ids.

        Called when a node re-registers (startup sweep). A re-registering node
        means a new process started — any claims from the previous process are
        orphaned and must be returned to the queue so another worker can pick
        them up. This is the fast-path complement to list_stale_claims: it fires
        immediately on restart rather than waiting for the reaper lease to expire.
        """
        now = _now()
        try:
            with self._write() as conn:
                rows = conn.execute(
                    "SELECT id FROM mesh_tasks WHERE claimed_by = ? AND status = 'claimed' AND COALESCE(queue_protocol, 0) = 0",
                    (node_id,),
                ).fetchall()
                task_ids = [r[0] for r in rows]
                if task_ids:
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                            claimer_incarnation = NULL, updated_at = ?
                        WHERE claimed_by = ? AND status = 'claimed' AND COALESCE(queue_protocol, 0) = 0
                        """,
                        (now, node_id),
                    )
                return task_ids
        except Exception as e:
            logger.warning("event=db_release_node_claims_failed node_id=%s err=%s", node_id, e)
            return []

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            try:
                dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S.%f")
            except Exception:
                return None
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    @classmethod
    def _stale_online_claim_reason(
        cls,
        row: sqlite3.Row,
        now: datetime,
        live_state_max_age_sec: int,
        active_task_max_runtime_sec: int,
    ) -> Optional[str]:
        live_raw = row["live_state"] if "live_state" in row.keys() else None
        updated_raw = row["live_state_updated_at"] if "live_state_updated_at" in row.keys() else None
        if not live_raw or not updated_raw:
            # Compatibility for old workers: without live_state, online still
            # means unknown. Offline/incarnation checks remain active.
            return None

        updated = cls._parse_dt(updated_raw)
        if updated is None or (now - updated).total_seconds() > live_state_max_age_sec:
            return None

        try:
            live = json.loads(live_raw) if isinstance(live_raw, str) else (live_raw or {})
        except Exception:
            return None
        if not isinstance(live, dict):
            return None

        task_id = row["id"]
        active_ids = set(str(t) for t in (live.get("active_tasks") or []))
        details = live.get("active_task_details") or {}
        if isinstance(details, list):
            details = {
                str(item.get("task_id")): item
                for item in details
                if isinstance(item, dict) and item.get("task_id")
            }
        elif not isinstance(details, dict):
            details = {}

        if str(task_id) not in active_ids and str(task_id) not in details:
            return "missing_from_live_state"

        max_runtime = max(0, int(active_task_max_runtime_sec or 0))
        if max_runtime <= 0:
            return None

        detail = details.get(str(task_id)) if isinstance(details, dict) else None
        started_raw = detail.get("started_at") if isinstance(detail, dict) else None
        started = cls._parse_dt(started_raw) or cls._parse_dt(row["claimed_at"])
        if started is not None and (now - started).total_seconds() > max_runtime:
            return "active_task_over_max_runtime"
        return None

    def list_stale_claims(
        self,
        lease_sec: int = 300,
        *,
        live_state_max_age_sec: int = 90,
        active_task_max_runtime_sec: int = 1800,
    ) -> List[Dict[str, Any]]:
        """Return claimed tasks whose claim has expired.

        A claim is stale when:
        - `claimed_at` is older than `lease_sec` seconds ago, AND one of:
          - the claiming node no longer exists in the nodes table, OR
          - the claiming node is offline (missed heartbeats), OR
          - the claiming node is online but its current incarnation_id differs
            from claimer_incarnation (node restarted in-place — new process,
            same node_id, same online status — the dead process's claim is
            orphaned and will never complete).
          - the claiming node has fresh live_state and the task is not in that
            live_state's active task set.
          - the task is still active in fresh live_state but has exceeded the
            active-task hard runtime cap.

        The incarnation mismatch condition catches the PM2-restart gap that the
        offline-only check misses: the old process is SIGKILLed, the new process
        re-registers within seconds (online again), so the orphaned claim never
        becomes offline → was stuck forever before this fix.
        """
        try:
            # Open a fresh connection for stale-claim queries to avoid potential
            # stale cache issues when SQLite connections are reused across tests.
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(str(self._path))
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """
                SELECT t.*, n.status AS node_status, n.incarnation_id AS node_incarnation_id,
                       n.live_state, n.live_state_updated_at
                FROM mesh_tasks t
                LEFT JOIN nodes n ON t.claimed_by = n.node_id
                WHERE t.status = 'claimed'
                  AND t.claimed_at IS NOT NULL
                  AND COALESCE(t.queue_protocol, 0) = 0
                """,
            ).fetchall()
            conn.close()
            now = datetime.utcnow()
            cutoff = lease_sec
            result = []
            for r in rows:
                claimed_dt = self._parse_dt(r["claimed_at"])
                if claimed_dt is None:
                    continue
                age = (now - claimed_dt).total_seconds()
                if age <= cutoff:
                    continue

                node_status = r["node_status"]
                reason = None
                if node_status is None:
                    reason = "node_missing"
                elif node_status == "offline":
                    reason = "node_offline"
                elif (
                    node_status == "online"
                    and r["claimer_incarnation"] is not None
                    and r["node_incarnation_id"] is not None
                    and r["node_incarnation_id"] != r["claimer_incarnation"]
                ):
                    reason = "incarnation_mismatch"
                elif node_status == "online":
                    reason = self._stale_online_claim_reason(
                        r,
                        now,
                        live_state_max_age_sec,
                        active_task_max_runtime_sec,
                    )

                if reason:
                    d = dict(r)
                    d.pop("node_status", None)
                    d["_stale_reason"] = reason
                    result.append(d)
            return result
        except Exception as e:
            logger.warning("event=db_list_stale_claims_failed err=%s", e)
            return []

    def complete_task(
        self,
        task_id: str,
        result: Dict[str, Any],
        artifact_path: Optional[str] = None,
    ) -> None:
        """Mark a claimed task as completed and store the ExecutionResult."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'completed', result = ?, artifact_path = COALESCE(?, artifact_path),
                        completed_at = ?, updated_at = ?
                    WHERE id = ? AND COALESCE(queue_protocol, 0) = 0
                    """,
                    (json.dumps(result), artifact_path, now, now, task_id),
                )
        except Exception as e:
            logger.warning("event=db_complete_task_failed task_id=%s err=%s", task_id, e)

    def fail_task(
        self,
        task_id: str,
        error: str,
        status: str = "failed",
        result: Optional[Dict[str, Any]] = None,
        artifact_path: Optional[str] = None,
    ) -> None:
        """Mark a task as failed. status can be 'failed' or 'failed_node_offline'."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = ?, error = ?, result = COALESCE(?, result),
                        artifact_path = COALESCE(?, artifact_path),
                        completed_at = ?, updated_at = ?
                    WHERE id = ? AND COALESCE(queue_protocol, 0) = 0
                    """,
                    (
                        status,
                        error,
                        json.dumps(result) if result is not None else None,
                        artifact_path,
                        now,
                        now,
                        task_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_fail_task_failed task_id=%s err=%s", task_id, e)

    # ================================================================== #
    # A82 Stage 2 — STRICT managed (protocol-1) turn helpers.
    #
    # These are a DISTINCT path from the legacy protocol-0 helpers above
    # (A82 §15 decision 2). They:
    #   * operate ONLY on `queue_protocol = 1` rows;
    #   * carry ownership / claim-token / status COMPARE-AND-SWAP predicates;
    #   * RAISE typed `TurnQueueError`s (they do NOT swallow a losing write the
    #     way legacy complete_task/fail_task do);
    #   * commit terminal status + native-id + active-identity ATOMICALLY in one
    #     transaction (design §6).
    # Legacy `complete_task` / `fail_task` above are UNCHANGED — their callers
    # keep the existing swallowing behavior.
    # ================================================================== #

    def enqueue_turn(
        self,
        task_id: Optional[str] = None,
        session_id: str = "",
        backend: Optional[str] = None,
        action: str = "resume_session",
        payload: Optional[Dict[str, Any]] = None,
        *,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
        turn_source: str = "system",
        turn_kind: str = "instruction",
        sender_session_id: Optional[str] = None,
        idempotency_scope: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        admission_hash: Optional[str] = None,
        coalesce_key: Optional[str] = None,
        flow_run_id: Optional[str] = None,
        machine_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        not_before: Optional[str] = None,
        expires_at: Optional[str] = None,
        require_enrolled: bool = False,
        lineage_token: Optional[str] = None,
        lineage_lease_sec: float = 30.0,
        producer_token: Optional[str] = None,
        producer_meta: Optional[Dict[str, Any]] = None,
        idle_only: bool = False,
        external_waiting: int = 0,
        fleet_cap: Optional[int] = None,
        per_session_cap: Optional[int] = None,
        deadline_sec: Optional[float] = None,
    ) -> "TurnAdmission":
        """Admit a MANAGED (protocol-1) turn in ONE bounded transaction
        (design §4 + §8; A82 Stage 4a).

        Order inside the transaction:
          1. Durable idempotency — a matching (scope, key) with the SAME
             admission hash returns the EXISTING row's id + current status/
             revision (replay, even after edit/withdraw/close or a full queue);
             the same key with a DIFFERENT hash raises `OwnershipConflictError`
             (409). An absent hash is derived from the canonical request.
          2. Active coalesce — an internal producer's `coalesce_key` that
             already names an open row returns that row (never for human turns).
          3. Recipient validation — with `require_enrolled` the session row must
             exist, carry the durable enrollment marker and not be closed.
          4. Capacity — fleet queued+pending managed rows + `external_waiting`
             (legacy occupancy of the SAME shared allowance) < `fleet_cap`
             (`config.system.max_queue_size`), per-session queued+pending <
             `per_session_cap` (20), stored intent bytes <= 2 MiB/row and the
             fleet persisted `intent_bytes` sum <= 100 MiB.
          5. Per-session monotonic sequence + insert (`queue_protocol=1`,
             `status='queued'`, explicit Case `flow_run_id`, `intent_bytes`).
        The acknowledgement (`TurnAdmission`, a str equal to the turn id) is
        built only after COMMIT; any failure raises a typed error (429/413/409/
        422/503) and nothing is acknowledged.

        [A82 Stage 4c] ``producer_token`` (a Case continuation token id) is
        linked to the admitted turn in the SAME transaction (every branch —
        fresh, replay, coalesce); a token that cannot link rolls the admission
        back (`_link_producer_token`).

        [A82 Stage 4d] ``idle_only`` (optional automation — cache heartbeat)
        refuses a fresh insert with 409 unless the session is idle by the
        ledger (`_session_idle_for_optional_turn`), inside the same txn.

        Convenience form (producers/tests): `body=` alone builds the payload,
        `operation_id=` is the idempotency key, `task_id`/`backend` default to a
        fresh id / the session's backend. `queue_protocol` is SERVER-OWNED."""
        from .turn_queue import (
            ADMISSION_DEADLINE_SEC, MAX_INTENT_BYTES_FLEET, MAX_INTENT_BYTES_PER_ROW,
            PER_SESSION_WAITING_CAP, TurnAdmission,
        )

        sid = (session_id or "").strip()
        if not sid:
            raise MalformedTurnError("managed turn requires a session_id")
        if payload is None:
            if body is None:
                raise MalformedTurnError("managed turn requires a body or payload", session_id=sid)
            payload = {"prompt": body}
        if not isinstance(payload, dict):
            raise MalformedTurnError("managed turn payload must be an object", session_id=sid)
        prompt: Optional[str] = body if body is not None else (
            payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
        )
        if coalesce_key is not None and turn_source == "human":
            # design §3: a coalesce key is never used to merge human instructions.
            raise MalformedTurnError("human turns cannot carry a coalesce_key", session_id=sid)
        if idempotency_key is None and operation_id is not None:
            idempotency_key = operation_id
        if idempotency_key is not None and idempotency_scope is None:
            idempotency_scope = f"{turn_source}:{sid}"
        if admission_hash is None:
            admission_hash = _canonical_admission_hash({
                "session_id": sid, "action": action, "turn_kind": turn_kind,
                "prompt": prompt, "flow_run_id": flow_run_id,
                "coalesce_key": coalesce_key,
            })
        try:
            payload_json = json.dumps(payload)
        except (TypeError, ValueError) as e:
            raise MalformedTurnError(f"managed turn payload is not JSON: {e}", session_id=sid)
        intent_bytes = len(payload_json.encode("utf-8")) + (
            len(prompt.encode("utf-8")) if prompt is not None else 0
        )
        if intent_bytes > MAX_INTENT_BYTES_PER_ROW:
            raise ByteCapError(
                "stored intent exceeds the per-row cap", session_id=sid,
                intent_bytes=intent_bytes, cap=MAX_INTENT_BYTES_PER_ROW,
            )
        if fleet_cap is None:
            from config import config as _cfg
            fleet_cap = int(_cfg.system.max_queue_size)
        per_cap = PER_SESSION_WAITING_CAP if per_session_cap is None else int(per_session_cap)
        deadline = ADMISSION_DEADLINE_SEC if deadline_sec is None else float(deadline_sec)
        new_id = task_id or f"turn_{uuid.uuid4().hex[:12]}"
        now = _now()
        admitted: Optional[Dict[str, Any]] = None
        try:
            with self._managed_write("enqueue_turn", deadline) as conn:
                # 1. Idempotency resolution (design §4 step 1).
                if idempotency_key is not None:
                    existing = conn.execute(
                        """
                        SELECT id, status, revision, admission_hash, queue_sequence,
                               lineage_state
                        FROM mesh_tasks
                        WHERE queue_protocol = 1 AND idempotency_scope IS ?
                          AND idempotency_key = ?
                        """,
                        (idempotency_scope, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if existing["admission_hash"] not in (None, admission_hash):
                            raise OwnershipConflictError(
                                "idempotency key reused with a different original "
                                "request (design §4)",
                                task_id=existing["id"],
                            )
                        admitted = {
                            "task_id": existing["id"], "status": existing["status"],
                            "revision": existing["revision"],
                            "queue_sequence": existing["queue_sequence"],
                            "idempotent_replay": True, "coalesced": False,
                            "lineage_pending": existing["lineage_state"] == "pending",
                        }
                # 2. Active coalesce (internal producers only, design §3/§7).
                if admitted is None and coalesce_key is not None:
                    existing = conn.execute(
                        """
                        SELECT id, status, revision, queue_sequence FROM mesh_tasks
                        WHERE queue_protocol = 1 AND coalesce_key = ?
                          AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')
                        """,
                        (coalesce_key,),
                    ).fetchone()
                    if existing is not None:
                        admitted = {
                            "task_id": existing["id"], "status": existing["status"],
                            "revision": existing["revision"],
                            "queue_sequence": existing["queue_sequence"],
                            "idempotent_replay": True, "coalesced": True,
                        }
                        if turn_source in ("human", "operator"):
                            # [A82 Stage 4b final] A coalesced operator action
                            # is still an operator action: release the stop
                            # hold (same statement as a fresh admission).
                            _release_stop_hold(conn, sid, now)
                if admitted is None:
                    # 3. Canonical recipient + durable enrollment marker.
                    srow = conn.execute(
                        "SELECT backend, status, turn_queue_enrolled FROM sessions "
                        "WHERE session_id = ?",
                        (sid,),
                    ).fetchone()
                    if require_enrolled:
                        if srow is None:
                            raise TurnNotFoundError("unknown session", session_id=sid)
                        if not srow["turn_queue_enrolled"]:
                            raise OwnershipConflictError(
                                "session is not enrolled in the managed turn queue",
                                session_id=sid,
                            )
                        if (srow["status"] or "") == "closed":
                            raise OwnershipConflictError(
                                "session is closed; admission refused", session_id=sid,
                            )
                    if idle_only and not _session_idle_for_optional_turn(conn, sid):
                        # [A82 Stage 4d] Optional automation (heartbeat) is
                        # idle-only: never queued behind (or ahead of) real
                        # work, never into a held/closed session.
                        raise OwnershipConflictError(
                            "session is not idle; optional automation refused",
                            session_id=sid, reason="not_idle",
                        )
                    row_backend = backend or (srow["backend"] if srow is not None else None)
                    if not row_backend:
                        raise MalformedTurnError("managed turn has no backend", session_id=sid)
                    # 4. Capacity, counted INSIDE the transaction (design §8).
                    fleet = conn.execute(
                        f"""
                        SELECT COUNT(*) AS n, COALESCE(SUM(intent_bytes), 0) AS b
                        FROM mesh_tasks INDEXED BY idx_mesh_turns_session_open
                        WHERE {_MANAGED_OPEN_PREDICATE}
                          AND status IN ('queued', 'pending')
                        """
                    ).fetchone()
                    if int(fleet["n"]) + max(0, int(external_waiting)) + 1 > fleet_cap:
                        raise CapacityError(
                            "fleet waiting capacity reached", session_id=sid,
                            managed_waiting=int(fleet["n"]),
                            legacy_waiting=int(external_waiting), cap=fleet_cap,
                            retry_after=1,
                        )
                    if int(fleet["b"]) + intent_bytes > MAX_INTENT_BYTES_FLEET:
                        raise CapacityError(
                            "fleet stored-intent byte budget reached", session_id=sid,
                            stored_bytes=int(fleet["b"]), cap=MAX_INTENT_BYTES_FLEET,
                            retry_after=1,
                        )
                    per_session = conn.execute(
                        f"""
                        SELECT COUNT(*) FROM mesh_tasks INDEXED BY idx_mesh_turns_session_open
                        WHERE session_id = ? AND {_MANAGED_OPEN_PREDICATE}
                          AND status IN ('queued', 'pending')
                        """,
                        (sid,),
                    ).fetchone()[0]
                    if int(per_session) + 1 > per_cap:
                        raise CapacityError(
                            "per-session waiting capacity reached", session_id=sid,
                            session_waiting=int(per_session), cap=per_cap,
                            retry_after=1,
                        )
                    # 5. Allocate per-session monotonic sequence via the
                    # session-sequence index (ordered LIMIT 1, design §3).
                    seq_row = conn.execute(
                        """
                        SELECT queue_sequence FROM mesh_tasks
                        WHERE queue_protocol = 1 AND session_id = ?
                          AND queue_sequence IS NOT NULL
                        ORDER BY queue_sequence DESC LIMIT 1
                        """,
                        (sid,),
                    ).fetchone()
                    sequence = (int(seq_row[0]) + 1) if seq_row and seq_row[0] is not None else 1
                    if turn_source in ("human", "operator"):
                        # [A82 Stage 4b rework] An operator action releases the
                        # stop hold (legacy parity: the next send clears
                        # CANCELLED); automation never does.
                        _release_stop_hold(conn, sid, now)
                    conn.execute(
                        """
                        INSERT INTO mesh_tasks (
                            id, session_id, machine_id, backend, action, payload, prompt,
                            status, parent_task_id, created_at, updated_at,
                            queue_protocol, queue_sequence, turn_source, sender_session_id,
                            turn_kind, idempotency_scope, idempotency_key, admission_hash,
                            revision, not_before, expires_at, coalesce_key, flow_run_id,
                            intent_bytes, lineage_state, lineage_token, lineage_lease_until
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?,
                                  1, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id, sid, machine_id, row_backend, action,
                            payload_json, prompt, parent_task_id, now, now,
                            sequence, turn_source, sender_session_id, turn_kind,
                            idempotency_scope, idempotency_key, admission_hash,
                            not_before, expires_at, coalesce_key, flow_run_id,
                            intent_bytes,
                            "pending" if lineage_token else None,
                            lineage_token,
                            _iso_in(lineage_lease_sec) if lineage_token else None,
                        ),
                    )
                    admitted = {
                        "task_id": new_id, "status": "queued", "revision": 1,
                        "queue_sequence": sequence, "idempotent_replay": False,
                        "coalesced": False, "lineage_pending": bool(lineage_token),
                    }
                if producer_token is not None:
                    _link_producer_token(
                        conn, producer_token, admitted["task_id"],
                        str(admitted["status"]), producer_meta, now,
                    )
        except TurnQueueError:
            raise
        except sqlite3.IntegrityError as e:
            raise OwnershipConflictError(
                f"managed enqueue integrity conflict: {e}", task_id=new_id,
            )
        except Exception as e:
            raise _turn_backing_error("enqueue_turn", task_id=new_id, err=e)
        # COMMITTED — only now build the acknowledgement (design §3.4).
        assert admitted is not None
        return TurnAdmission(
            admitted["task_id"],
            status=admitted["status"],
            revision=admitted["revision"],
            queue_sequence=admitted["queue_sequence"],
            idempotent_replay=admitted["idempotent_replay"],
            coalesced=admitted["coalesced"],
            lineage_pending=bool(admitted.get("lineage_pending")),
        )

    def claim_turn_lineage(self, task_id: str, token: str, lease_sec: float = 30.0) -> bool:
        """[A82 Stage 4a rework 2] CAS-claim the durable "lineage pending" state
        of a QUEUED row for a recovery writer: only when its lease expired (the
        admitting writer died or stalled). Returns True if this token owns it."""
        now = _now()
        try:
            with self._managed_write("claim_turn_lineage") as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET lineage_token = ?, lineage_lease_until = ?, updated_at = ?
                    WHERE id = ? AND queue_protocol = 1 AND status = 'queued'
                      AND lineage_state = 'pending'
                      AND (lineage_lease_until IS NULL OR lineage_lease_until <= ?)
                    """,
                    (token, _iso_in(lease_sec), now, task_id, now),
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("claim_turn_lineage", task_id=task_id, err=e)

    def finalize_turn_lineage(
        self, task_id: str, token: str, flow_run_id: Optional[str], metadata: Dict[str, Any],
    ) -> bool:
        """[A82 Stage 4a rework 2] Commit Case membership + lineage metadata and
        clear the durable "lineage pending" state — a CAS on
        `status='queued' AND lineage_state='pending' AND lineage_token=token`.
        A lineage-pending row can never be activated, and an activated row is
        never lineage-pending, so a finalize can never write into an activated
        (frozen) payload. Returns True on commit; False ⇒ the caller lost the
        lease (a recovery writer owns it now) and must not assume success."""
        try:
            with self._managed_write("finalize_turn_lineage") as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET flow_run_id = ?, lineage_state = 'done', lineage_token = NULL,
                        lineage_lease_until = NULL, updated_at = ?,
                        payload = json_set(COALESCE(payload, '{}'), '$.metadata', json(?))
                    WHERE id = ? AND queue_protocol = 1 AND status = 'queued'
                      AND lineage_state = 'pending' AND lineage_token = ?
                    """,
                    (flow_run_id, _now(), json.dumps(metadata, default=str), task_id, token),
                )
                changed = conn.execute("SELECT changes()").fetchone()[0] > 0
                if changed:
                    conn.execute(
                        "UPDATE mesh_tasks SET intent_bytes = "
                        "COALESCE(length(CAST(payload AS BLOB)), 0) + "
                        "COALESCE(length(CAST(prompt AS BLOB)), 0) WHERE id = ?",
                        (task_id,),
                    )
                return changed
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("finalize_turn_lineage", task_id=task_id, err=e)

    def turn_lineage_owner(self, task_id: str) -> Optional[Dict[str, Any]]:
        """[A82 Stage 4a rework 3] (status, lineage_state, lineage_token) for the
        lease fence of the managed-lineage procedure."""
        row = self._conn().execute(
            "SELECT status, lineage_state, lineage_token FROM mesh_tasks "
            "WHERE id = ? AND queue_protocol = 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def turn_lineage_state(self, task_id: str) -> Optional[str]:
        row = self._conn().execute(
            "SELECT lineage_state FROM mesh_tasks WHERE id = ? AND queue_protocol = 1",
            (task_id,),
        ).fetchone()
        return row[0] if row else None

    def list_lineage_recovery(self, limit: int = 25) -> List[Dict[str, Any]]:
        """[A82 Stage 4a rework 2] Queued lineage-pending rows whose writer lease
        expired (crash / stall). Bounded: waiting subset, LIMIT."""
        rows = self._conn().execute(
            """
            SELECT * FROM mesh_tasks
            WHERE queue_protocol = 1 AND status = 'queued' AND lineage_state = 'pending'
              AND (lineage_lease_until IS NULL OR lineage_lease_until <= ?)
            ORDER BY created_at ASC, id ASC LIMIT ?
            """,
            (_now(), max(0, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]

    def existing_task_lineage(self, task_id: str) -> Optional[Dict[str, str]]:
        """[A82 Stage 4a rework 2] Lineage already written for ``task_id`` by an
        earlier (crashed) writer, so recovery never births a second Case:
        a flow_runs row created FOR the task (birth / dispatch record), else a
        Case membership link (join / attach). Index-served."""
        row = self._conn().execute(
            "SELECT flow_run_id FROM flow_runs WHERE task_id = ? ORDER BY created_at LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is not None:
            return {"kind": "own_flow", "flow_run_id": row[0]}
        row = self._conn().execute(
            "SELECT flow_run_id FROM flow_links WHERE entity_type = 'task' AND entity_id = ? "
            "AND role = 'task' LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is not None:
            return {"kind": "member", "flow_run_id": row[0]}
        return None

    def managed_waiting_totals(self) -> Dict[str, int]:
        """[A82 Stage 4a] Fleet managed queued+pending count and persisted
        stored-intent bytes (design §8). Bounded: served by the open-row partial
        index; the waiting subset itself is capped (count + bytes)."""
        row = self._conn().execute(
            f"""
            SELECT COUNT(*) AS n, COALESCE(SUM(intent_bytes), 0) AS b,
                   COALESCE(SUM(status = 'queued'), 0) AS q
            FROM mesh_tasks INDEXED BY idx_mesh_turns_session_open
            WHERE {_MANAGED_OPEN_PREDICATE} AND status IN ('queued', 'pending')
            """
        ).fetchone()
        return {"count": int(row["n"]), "bytes": int(row["b"]), "queued": int(row["q"])}

    def managed_queued_bytes(self) -> int:
        """[A82 Stage 4a] Persisted stored-intent byte accounting for the waiting
        (queued+pending) managed subset — the figure admission enforces against
        `MAX_INTENT_BYTES_FLEET` inside its transaction (design §8)."""
        return self.managed_waiting_totals()["bytes"]

    def find_turn_by_idempotency(
        self, idempotency_scope: Optional[str], idempotency_key: str,
    ) -> Optional[Dict[str, Any]]:
        """[A82 Stage 4a] Read-only durable idempotency probe (index-served) so a
        producer can short-circuit a replay BEFORE side effects such as Case
        lineage writes. The admission transaction re-checks authoritatively."""
        row = self._conn().execute(
            """
            SELECT id, status, revision, admission_hash, queue_sequence
            FROM mesh_tasks
            WHERE queue_protocol = 1 AND idempotency_scope IS ? AND idempotency_key = ?
            """,
            (idempotency_scope, idempotency_key),
        ).fetchone()
        return dict(row) if row else None

    def select_eligible_turn_heads(
        self, limit: int = 25, now: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """[A82 Stage 4a] Fair-scheduler head selection (design §5 step 1-2).

        Reads ONLY the bounded waiting subset (`idx_mesh_turns_waiting`; the
        whole subset is capped by the fleet waiting cap) and applies EVERY
        eligibility filter BEFORE the LIMIT: the row is its session's head (no
        earlier open managed row — a delayed/blocked head holds only its own
        session and a later request can never overtake it), no active slot
        holder, `not_before` reached, session enrolled / not paused / not
        closed. Ordered by acceptance time + stable id. Returns small summaries
        (no prompt/payload bodies); the caller fetches one full row at a time."""
        ts = now or _now()
        rows = self._conn().execute(
            f"""
            SELECT t.id, t.session_id, t.revision, t.queue_sequence, t.created_at,
                   t.expires_at, t.turn_source, s.config_revision
            FROM mesh_tasks t
            JOIN sessions s ON s.session_id = t.session_id
            WHERE t.queue_protocol = 1 AND t.status = 'queued'
              AND (t.not_before IS NULL OR t.not_before <= ?)
              AND (t.blocked_until IS NULL OR t.blocked_until <= ?)
              AND (t.lineage_state IS NULL OR t.lineage_state != 'pending')
              AND s.turn_queue_enrolled = 1 AND s.turn_queue_paused = 0
              AND COALESCE(s.status, '') NOT IN ('closed', 'cancelled')
              AND s.turn_queue_hold IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM mesh_tasks e
                  WHERE e.session_id = t.session_id
                    AND e.queue_protocol = 1
                    AND e.status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')
                    AND e.queue_sequence < t.queue_sequence
              )
              AND NOT EXISTS (
                  SELECT 1 FROM mesh_tasks a
                  WHERE a.session_id = t.session_id
                    AND a.queue_protocol = 1 AND a.session_id IS NOT NULL
                    AND a.status IN ('pending', 'claimed', 'running', 'recovery_required')
              )
            ORDER BY t.created_at ASC, t.id ASC
            LIMIT ?
            """,
            (ts, ts, max(0, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]

    def activate_prepared_turn(
        self,
        task_id: str,
        *,
        expected_revision: int,
        expected_config_revision: int,
        action: str,
        payload: Dict[str, Any],
        machine_id: Optional[str],
        deadline_sec: Optional[float] = None,
    ) -> str:
        """[A82 Stage 4a] Conditionally commit `queued -> pending` with the
        IMMUTABLE prepared execution payload + carrier assignment (design §5
        step 4). Preparation happened OUTSIDE this transaction; here only
        predicates are re-checked (no file/network/model work):

          * the row is still queued at `expected_revision` (else ``"stale"`` —
            re-prepare against the new revision; ``"gone"`` if no longer queued);
          * the session is enrolled, not paused, not closed, and its
            `config_revision` is unchanged (else ``"stale"`` / ``"ineligible"``);
          * the row is still its session's head and no slot holder exists
            (else ``"ineligible"``; the one-active index is the backstop);
          * the prepared payload fits the per-row cap (else ``"oversize"``,
            recorded as a bounded `blocked_reason`, row stays queued).
        Returns ``"activated"`` on commit."""
        from .turn_queue import ADMISSION_DEADLINE_SEC, MAX_INTENT_BYTES_PER_ROW

        payload_json = json.dumps(payload)
        prepared_bytes = len(payload_json.encode("utf-8"))
        now = _now()
        deadline = ADMISSION_DEADLINE_SEC if deadline_sec is None else float(deadline_sec)
        try:
            with self._managed_write("activate_turn", deadline) as conn:
                row = conn.execute(
                    "SELECT session_id, status, revision, prompt, lineage_state FROM mesh_tasks "
                    "WHERE id = ? AND queue_protocol = 1",
                    (task_id,),
                ).fetchone()
                if row is None or row["status"] != "queued":
                    return "gone"
                if row["lineage_state"] == "pending":
                    return "ineligible"  # never activate before Case lineage
                if int(row["revision"]) != int(expected_revision):
                    return "stale"
                srow = conn.execute(
                    "SELECT status, turn_queue_enrolled, turn_queue_paused, config_revision, "
                    "turn_queue_hold FROM sessions WHERE session_id = ?",
                    (row["session_id"],),
                ).fetchone()
                if (
                    srow is None or not srow["turn_queue_enrolled"]
                    or srow["turn_queue_paused"]
                    or (srow["status"] or "") in ("closed", "cancelled")
                    or srow["turn_queue_hold"]
                ):
                    # [A82 Stage 4b rework] `cancelled` = operator stop hold:
                    # nothing activates until an operator action releases it.
                    return "ineligible"
                if int(srow["config_revision"]) != int(expected_config_revision):
                    return "stale"
                blocker = conn.execute(
                    f"""
                    SELECT 1 FROM mesh_tasks e
                    WHERE e.session_id = ? AND e.queue_protocol = 1
                    AND e.status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')
                      AND (e.queue_sequence < (SELECT queue_sequence FROM mesh_tasks WHERE id = ?)
                           OR e.status IN ('pending', 'claimed', 'running', 'recovery_required'))
                    LIMIT 1
                    """,
                    (row["session_id"], task_id),
                ).fetchone()
                if blocker is not None:
                    return "ineligible"
                prompt_bytes = len((row["prompt"] or "").encode("utf-8"))
                if prepared_bytes + prompt_bytes > MAX_INTENT_BYTES_PER_ROW:
                    _apply_turn_block(
                        conn, task_id, f"prepared_payload_oversize bytes={prepared_bytes}",
                    )
                    return "oversize"
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'pending', activated_at = ?, updated_at = ?,
                        action = ?, payload = ?, machine_id = ?,
                        intent_bytes = ?, blocked_reason = NULL,
                        blocked_until = NULL, blocked_attempts = 0
                    WHERE id = ? AND queue_protocol = 1 AND status = 'queued'
                      AND (lineage_state IS NULL OR lineage_state != 'pending')
                      AND revision = ?
                    """,
                    (now, now, action, payload_json, machine_id,
                     prepared_bytes + prompt_bytes, task_id, int(expected_revision)),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    return "stale"
                return "activated"
        except TurnQueueError:
            raise
        except sqlite3.IntegrityError:
            return "ineligible"
        except Exception as e:
            raise _turn_backing_error("activate_prepared_turn", task_id=task_id, err=e)

    def mark_turn_blocked(self, task_id: str, reason: str) -> bool:
        """[A82 Stage 4a rework] A still-queued head that could not be activated
        gets a bounded reason AND an exponential, capped retry backoff
        (`blocked_until`), so it drops out of head selection until then and
        cannot monopolize the per-pass LIMIT (design §5.2: a blocked head holds
        only its own session). Returns True when the reason CHANGED (callers log
        only on a state change)."""
        try:
            with self._managed_write("mark_turn_blocked") as conn:
                return _apply_turn_block(conn, task_id, reason)
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("mark_turn_blocked", task_id=task_id, err=e)

    def count_slot_waiting_sessions(self) -> int:
        """[A82 Stage 4a rework] Sessions whose queued work waits ONLY on their
        own active slot holder (enrolled, unpaused, open). Their progress comes
        from a completion, which is hinted in-process but may commit in another
        process (out-of-process task server), so the scheduler rechecks them on
        a bounded backoff. Bounded by the waiting subset."""
        row = self._conn().execute(
            """
            SELECT COUNT(DISTINCT t.session_id)
            FROM mesh_tasks t JOIN sessions s ON s.session_id = t.session_id
            WHERE t.queue_protocol = 1 AND t.status = 'queued'
              AND s.turn_queue_enrolled = 1 AND s.turn_queue_paused = 0
              AND COALESCE(s.status, '') NOT IN ('closed', 'cancelled')
              AND s.turn_queue_hold IS NULL
              AND EXISTS (
                  SELECT 1 FROM mesh_tasks a
                  WHERE a.session_id = t.session_id
                    AND a.queue_protocol = 1 AND a.session_id IS NOT NULL
                    AND a.status IN ('pending', 'claimed', 'running', 'recovery_required')
              )
            """
        ).fetchone()
        return int(row[0] or 0)

    def next_turn_wake_at(self, now: Optional[str] = None) -> Optional[str]:
        """[A82 Stage 4a rework] Earliest future time a queued row becomes
        time-eligible again (`not_before` / `blocked_until`), or None. Bounded:
        reads only the waiting subset (`idx_mesh_turns_waiting`)."""
        ts_now = now or _now()
        row = self._conn().execute(
            """
            SELECT MIN(w) FROM (
                SELECT CASE WHEN lineage_state = 'pending'
                            THEN COALESCE(lineage_lease_until, '')
                            ELSE max(COALESCE(not_before, ''), COALESCE(blocked_until, ''))
                       END AS w
                FROM mesh_tasks WHERE queue_protocol = 1 AND status = 'queued'
            ) WHERE w > ?
            """,
            (ts_now,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def activate_turn(self, task_id: str) -> bool:
        """Transition a managed head `queued -> pending` (design §5 activation).

        Stage 2 provides the atomic `queued -> pending` transition + activation
        timestamp only; the fair scheduler (eligible-head selection, config
        revalidation, expensive context preparation OUTSIDE the transaction) is
        Stage 4. The one-active-session partial unique index is the backstop:
        activating a second turn while a slot-holder exists raises the integrity
        conflict, surfaced as `OwnershipConflictError`. Returns True on
        transition."""
        now = _now()
        try:
            with self._write() as conn:
                # [A82 Stage 4a rework] Activation is also carrier assignment
                # (design §5): an unassigned row takes its session's pin, since
                # claim now requires an exact assignment match.
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'pending', activated_at = ?, updated_at = ?,
                        machine_id = COALESCE(machine_id, (
                            SELECT NULLIF(s.machine_id, '') FROM sessions s
                            WHERE s.session_id = mesh_tasks.session_id))
                    WHERE id = ? AND queue_protocol = 1 AND status = 'queued'
                      AND (lineage_state IS NULL OR lineage_state != 'pending')
                    """,
                    (now, now, task_id),
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.IntegrityError as e:
            raise OwnershipConflictError(
                f"activation would create a second active slot: {e}", task_id=task_id,
            )
        except Exception as e:
            raise _turn_backing_error("activate_turn", task_id=task_id, err=e)

    def revise_turn(
        self,
        task_id: str,
        expected_revision: int,
        *,
        body: Optional[str] = None,
        attachments: Optional[List[Any]] = None,
        actor: str = "",
        max_edits: int = 20,
    ) -> Dict[str, Any]:
        """Conditional edit of a QUEUED managed turn + bounded audit, in ONE
        transaction (design §4/§3 revision).

        Compare-and-swap on `id + queue_protocol=1 + status='queued' +
        revision=expected_revision`; a stale revision or a non-queued
        (consumed/withdrawn) turn raises `OwnershipConflictError` (409). The new
        revision row is inserted into `mesh_turn_revisions` ATOMICALLY with the
        bump — both commit or both roll back. Caps edits at `max_edits`
        (design §8). Cannot change recipient/source/Case/sequence."""
        now = _now()
        try:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT status, queue_protocol, revision FROM mesh_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["queue_protocol"] != 1:
                    raise TurnNotFoundError("no managed turn to revise", task_id=task_id)
                if row["status"] != "queued":
                    raise OwnershipConflictError(
                        "only a queued turn is editable (consumption race)",
                        task_id=task_id, status=row["status"],
                    )
                if row["revision"] != expected_revision:
                    raise OwnershipConflictError(
                        "stale revision", task_id=task_id,
                        expected=expected_revision, current=row["revision"],
                    )
                if expected_revision >= max_edits:
                    raise CapacityError(
                        "edit cap reached for this turn", task_id=task_id,
                        max_edits=max_edits,
                    )
                new_rev = expected_revision + 1
                # An edit re-arms a blocked head (its backoff is cleared).
                sets = ["revision = ?", "updated_at = ?", "blocked_until = NULL",
                        "blocked_attempts = 0"]
                params: List[Any] = [new_rev, now]
                if body is not None:
                    sets.append("prompt = ?")
                    params.append(body)
                    # Keep the payload prompt mirror consistent for the edited body.
                    sets.append(
                        "payload = json_set(COALESCE(payload, '{}'), '$.prompt', ?)"
                    )
                    params.append(body)
                params.append(task_id)
                conn.execute(
                    f"UPDATE mesh_tasks SET {', '.join(sets)} "
                    f"WHERE id = ? AND queue_protocol = 1 AND status = 'queued' "
                    f"AND revision = {expected_revision}",
                    params,
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise OwnershipConflictError(
                        "revise lost the state race", task_id=task_id,
                    )
                if body is not None:
                    # [A82 Stage 4a] Re-account the stored intent and hold the
                    # per-row + fleet byte caps (design §8: an edit cannot grow
                    # the budget beyond its cap). Raising rolls the edit back.
                    from .turn_queue import MAX_INTENT_BYTES_FLEET, MAX_INTENT_BYTES_PER_ROW
                    conn.execute(
                        "UPDATE mesh_tasks SET intent_bytes = "
                        "COALESCE(length(CAST(payload AS BLOB)), 0) + "
                        "COALESCE(length(CAST(prompt AS BLOB)), 0) WHERE id = ?",
                        (task_id,),
                    )
                    own = conn.execute(
                        "SELECT intent_bytes FROM mesh_tasks WHERE id = ?", (task_id,),
                    ).fetchone()[0]
                    if int(own) > MAX_INTENT_BYTES_PER_ROW:
                        raise ByteCapError(
                            "edited intent exceeds the per-row cap", task_id=task_id,
                            intent_bytes=int(own), cap=MAX_INTENT_BYTES_PER_ROW,
                        )
                    total = conn.execute(
                        f"SELECT COALESCE(SUM(intent_bytes), 0) FROM mesh_tasks "
                        f"INDEXED BY idx_mesh_turns_session_open "
                        f"WHERE {_MANAGED_OPEN_PREDICATE} AND status IN ('queued', 'pending')"
                    ).fetchone()[0]
                    if int(total) > MAX_INTENT_BYTES_FLEET:
                        raise CapacityError(
                            "edit would exceed the fleet stored-intent budget",
                            task_id=task_id, stored_bytes=int(total),
                            cap=MAX_INTENT_BYTES_FLEET,
                        )
                # Atomic audit row (design §3: inserted with the queued revision).
                conn.execute(
                    """
                    INSERT INTO mesh_turn_revisions
                        (task_id, revision, actor, change_kind, body,
                         attachments_json, created_at)
                    VALUES (?, ?, ?, 'edit', ?, ?, ?)
                    """,
                    (task_id, new_rev, actor, body,
                     json.dumps(attachments) if attachments is not None else None, now),
                )
                return {"id": task_id, "revision": new_rev, "status": "queued"}
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("revise_turn", task_id=task_id, err=e)

    def withdraw_turn(
        self,
        task_id: str,
        expected_revision: Optional[int] = None,
        *,
        actor: str = "",
    ) -> bool:
        """Conditional withdrawal of a QUEUED managed turn + audit, ONE
        transaction (design §4). A queued withdrawal is a terminal
        `withdrawn` outcome — it NEVER becomes an execution failure (design §3).
        Only a queued row is withdrawable this way; a consumed turn requires
        cancellation/close (Stage 4). Returns True on withdrawal."""
        now = _now()
        try:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT status, queue_protocol, revision FROM mesh_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["queue_protocol"] != 1:
                    raise TurnNotFoundError("no managed turn to withdraw", task_id=task_id)
                if row["status"] != "queued":
                    raise OwnershipConflictError(
                        "only a queued turn is withdrawable here",
                        task_id=task_id, status=row["status"],
                    )
                if expected_revision is not None and row["revision"] != expected_revision:
                    raise OwnershipConflictError(
                        "stale revision on withdraw", task_id=task_id,
                        expected=expected_revision, current=row["revision"],
                    )
                new_rev = int(row["revision"]) + 1
                # [A82 Stage 4a rework 3] A withdrawal resolves a pending
                # lineage (`void`): the in-flight writer's fence/finalize then
                # reports withdrawn, and replays see the withdrawn status.
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'withdrawn', revision = ?, completed_at = ?, updated_at = ?,
                        lineage_state = CASE WHEN lineage_state = 'pending'
                                             THEN 'void' ELSE lineage_state END,
                        lineage_token = NULL, lineage_lease_until = NULL
                    WHERE id = ? AND queue_protocol = 1 AND status = 'queued'
                    """,
                    (new_rev, now, now, task_id),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise OwnershipConflictError(
                        "withdraw lost the state race", task_id=task_id,
                    )
                conn.execute(
                    """
                    INSERT INTO mesh_turn_revisions
                        (task_id, revision, actor, change_kind, body,
                         attachments_json, created_at)
                    VALUES (?, ?, ?, 'withdraw', NULL, NULL, ?)
                    """,
                    (task_id, new_rev, actor, now),
                )
                return True
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("withdraw_turn", task_id=task_id, err=e)

    # ------------------------------------------------------------------ #
    # [A82 Stage 4b] Producer 2 — operator cancel of the ACTIVE managed turn
    # and session close with managed rows. Strict: one bounded transaction
    # each, typed errors, never swallowed.
    # ------------------------------------------------------------------ #
    def request_turn_cancel(
        self, task_id: str, *, actor: str = "operator", hold_session: bool = False,
    ) -> "TurnCancelOutcome":
        """Cancel ONE managed turn, token-fenced, in ONE transaction.

        * ``pending`` (never claimed) / ``claimed`` never started: nothing ran,
          so the row becomes terminal ``cancelled`` here (the slot frees; a
          late ``/start-managed`` for the old token is refused 409 and the
          carrier drops the attempt).
        * ``running`` / ``recovery_required``: the backend may be executing.
          Record the cancel against the CURRENT claim token (``cancel_token``)
          and deliver ONE protocol-0 ``cancel_managed`` control row, pinned to
          the claiming carrier, keyed on (task, token) so a repeat is a no-op.
          The row stays owned by that attempt; its own result/recovery
          resolution commits ``cancelled`` (``complete_turn``). Queued rows of
          the session are untouched.
        * terminal ⇒ ``already_terminal``; ``queued`` ⇒ ``not_active``.
        Idempotent: a repeat converges on the same state/control row.

        ``hold_session`` (operator STOP, Stage 4b rework): in the SAME
        transaction the session enters the legacy ``cancelled`` status — the
        stop hold every Case automation consumer already honours (wake
        dispatcher, transient/quota resume, resume-mode choice, orphan sweep),
        and which activation honours for managed turns: nothing queued starts
        until an operator action (a new human/operator admission) releases it."""
        from .turn_queue import CANCEL_MANAGED_ACTION, TurnCancelOutcome

        now = _now()
        try:
            with self._managed_write("request_turn_cancel") as conn:
                row = conn.execute(
                    "SELECT id, session_id, backend, status, queue_protocol, claim_token, "
                    "claimed_by, started_at FROM mesh_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["queue_protocol"] != 1:
                    raise TurnNotFoundError("no managed turn to cancel", task_id=task_id)
                status = row["status"]
                if hold_session and status not in (
                    "completed", "failed", "cancelled", "failed_node_offline", "withdrawn", "queued",
                ):
                    conn.execute(
                        "UPDATE sessions SET status = 'cancelled', "
                        "turn_queue_hold = 'operator_stop', updated_at = ? "
                        "WHERE session_id = ? AND COALESCE(status, '') != 'closed'",
                        (now, row["session_id"]),
                    )
                if status in ("completed", "failed", "cancelled", "failed_node_offline", "withdrawn"):
                    return TurnCancelOutcome(task_id=task_id, outcome="already_terminal", status=status)
                if status == "queued":
                    return TurnCancelOutcome(task_id=task_id, outcome="not_active", status=status)
                note = f"cancelled by {actor or 'operator'} before start"[:200]
                if status == "pending" and row["claim_token"] is None:
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'cancelled', error = ?, cancel_requested_at = ?,
                            completed_at = ?, updated_at = ?
                        WHERE id = ? AND queue_protocol = 1 AND status = 'pending'
                          AND claim_token IS NULL
                        """,
                        (note, now, now, now, task_id),
                    )
                elif status == "claimed" and row["started_at"] is None:
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'cancelled', error = ?, cancel_token = claim_token,
                            cancel_requested_at = ?, completed_at = ?, updated_at = ?
                        WHERE id = ? AND queue_protocol = 1 AND status = 'claimed'
                          AND started_at IS NULL AND claim_token = ?
                        """,
                        (note, now, now, now, task_id, row["claim_token"]),
                    )
                elif status in ("running", "recovery_required", "claimed") and row["claim_token"]:
                    token = str(row["claim_token"])
                    node = str(row["claimed_by"] or "")
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET cancel_token = claim_token,
                            cancel_requested_at = COALESCE(cancel_requested_at, ?),
                            updated_at = ?
                        WHERE id = ? AND queue_protocol = 1 AND claim_token = ?
                          AND status IN ('claimed', 'running', 'recovery_required')
                        """,
                        (now, now, task_id, token),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0] == 0:
                        raise OwnershipConflictError("cancel lost the state race", task_id=task_id)
                    control_id = (
                        f"cancelm-{task_id}-"
                        f"{hashlib.sha256(token.encode()).hexdigest()[:12]}"
                    )
                    payload = {
                        "target_task_id": task_id,
                        "session": {"session_id": row["session_id"], "backend": row["backend"]},
                    }
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO mesh_tasks (
                            id, session_id, machine_id, backend, action, payload,
                            status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (control_id, row["session_id"], node or None, row["backend"],
                         CANCEL_MANAGED_ACTION, json.dumps(payload), now, now),
                    )
                    return TurnCancelOutcome(
                        task_id=task_id, outcome="requested", status=status,
                        node_id=node or None, control_task_id=control_id,
                    )
                else:
                    raise OwnershipConflictError(
                        "managed turn in an uncancellable shape", task_id=task_id, status=status,
                    )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise OwnershipConflictError("cancel lost the state race", task_id=task_id)
                return TurnCancelOutcome(task_id=task_id, outcome="cancelled", status="cancelled")
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("request_turn_cancel", task_id=task_id, err=e)

    def close_session_turns(self, session_id: str, *, actor: str = "operator") -> "SessionCloseTurns":
        """Durably close an ENROLLED session and withdraw ALL its queued managed
        rows in ONE transaction (design §7: close persists the authoritative
        closed state + the withdrawal, checked by admission and activation,
        which both refuse a closed session inside their own transactions).

        A withdrawn row whose Case lineage was written or is being written is
        marked ``lineage_state='void'`` (the caller/scheduler then voids any
        child Case born for it — ``list_void_lineage``). A still-leased
        lineage writer keeps its lease expiry so the void cleanup waits for it.
        Returns the withdrawn ids and the active slot holder (cancelled by the
        caller through ``request_turn_cancel``). Idempotent."""
        from .turn_queue import SessionCloseTurns

        sid = (session_id or "").strip()
        now = _now()
        try:
            with self._managed_write("close_session_turns") as conn:
                srow = conn.execute(
                    "SELECT status FROM sessions WHERE session_id = ?", (sid,),
                ).fetchone()
                if srow is None:
                    raise TurnNotFoundError("unknown session", session_id=sid)
                conn.execute(
                    "UPDATE sessions SET status = 'closed', turn_queue_hold = NULL, "
                    "updated_at = ? WHERE session_id = ?",
                    (now, sid),
                )
                queued = conn.execute(
                    f"""
                    SELECT id, revision FROM mesh_tasks INDEXED BY idx_mesh_turns_session_open
                    WHERE session_id = ? AND {_MANAGED_OPEN_PREDICATE} AND status = 'queued'
                    ORDER BY queue_sequence ASC
                    """,
                    (sid,),
                ).fetchall()
                withdrawn: List[str] = []
                for q in queued:
                    new_rev = int(q["revision"]) + 1
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'withdrawn', revision = ?, completed_at = ?, updated_at = ?,
                            lineage_lease_until = CASE WHEN lineage_state = 'pending'
                                                       THEN lineage_lease_until ELSE NULL END,
                            lineage_state = CASE WHEN lineage_state IN ('pending', 'done')
                                                 THEN 'void' ELSE lineage_state END,
                            lineage_token = NULL
                        WHERE id = ? AND queue_protocol = 1 AND status = 'queued'
                        """,
                        (new_rev, now, now, q["id"]),
                    )
                    conn.execute(
                        """
                        INSERT INTO mesh_turn_revisions
                            (task_id, revision, actor, change_kind, body,
                             attachments_json, created_at)
                        VALUES (?, ?, ?, 'withdraw', NULL, NULL, ?)
                        """,
                        (q["id"], new_rev, f"session_close:{actor or 'operator'}"[:128], now),
                    )
                    withdrawn.append(str(q["id"]))
                active = conn.execute(
                    """
                    SELECT id FROM mesh_tasks
                    WHERE session_id = ? AND queue_protocol = 1
                      AND status IN ('pending', 'claimed', 'running', 'recovery_required')
                    LIMIT 1
                    """,
                    (sid,),
                ).fetchone()
                return SessionCloseTurns(
                    session_id=sid, withdrawn=withdrawn,
                    active_task_id=str(active["id"]) if active else None,
                )
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("close_session_turns", session_id=sid, err=e)

    def list_void_lineage(self, limit: int = 25) -> List[Dict[str, Any]]:
        """[A82 Stage 4b] Withdrawn managed rows whose Case lineage still has to
        be voided (``lineage_state='void'``), oldest first, bounded. Served by
        the ``idx_mesh_turns_lineage_void`` partial index (rows leave it once
        voided). Includes each row's inherited writer lease expiry."""
        rows = self._conn().execute(
            """
            SELECT id, session_id, status, lineage_state, lineage_lease_until
            FROM mesh_tasks INDEXED BY idx_mesh_turns_lineage_void
            WHERE queue_protocol = 1 AND lineage_state = 'void'
            ORDER BY created_at ASC, id ASC LIMIT ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_lineage_voided(self, task_id: str) -> bool:
        """[A82 Stage 4b] CAS ``void`` → ``voided`` once the void cleanup has
        converged (the row leaves the void index). Raises on DB error."""
        try:
            with self._managed_write("mark_lineage_voided") as conn:
                conn.execute(
                    "UPDATE mesh_tasks SET lineage_state = 'voided', lineage_lease_until = NULL, "
                    "updated_at = ? WHERE id = ? AND queue_protocol = 1 "
                    "AND status = 'withdrawn' AND lineage_state = 'void'",
                    (_now(), task_id),
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("mark_lineage_voided", task_id=task_id, err=e)

    def clear_session_case_if(self, session_id: str, case_id: str) -> bool:
        """[A82 Stage 4b] Strict conditional affiliation clear: only when the
        session is still affiliated to ``case_id`` (a newer affiliation is never
        touched). Raises on DB error (unlike ``set_session_case``)."""
        try:
            with self._managed_write("clear_session_case_if") as conn:
                conn.execute(
                    "UPDATE sessions SET current_case_id = NULL, case_role = NULL, updated_at = ? "
                    "WHERE session_id = ? AND current_case_id = ?",
                    (_now(), session_id, case_id),
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("clear_session_case_if", session_id=session_id, err=e)

    def get_turn_revisions(self, task_id: str) -> List[Dict[str, Any]]:
        """Return the append-only edit audit for a managed turn (design §3)."""
        rows = self._conn().execute(
            "SELECT * FROM mesh_turn_revisions WHERE task_id = ? ORDER BY revision ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def enroll_session(self, session_id: str) -> None:
        """Mark a session as protocol-1 (managed) enrolled — a scoped UPDATE of
        exactly the enrollment marker (design §3 per-session marker).

        Enrollment eligibility (no legacy in-flight work, backend/carrier
        suitability, no racing legacy admission) is enforced by the caller
        (Stage 4); this is the durable persisted marker only. Byte-identical to
        today for un-enrolled sessions (flag OFF ⇒ default 0)."""
        sid = (session_id or "").strip()
        if not sid:
            return
        # Raised BEFORE the marker commits: a legacy admission racing the
        # enrollment then reads the marker instead of skipping it.
        with self._presence_lock:
            self._enroll_generation += 1
            self._enrolls_in_flight += 1
            self._any_enrolled = True
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE sessions SET turn_queue_enrolled = 1, updated_at = ? "
                    "WHERE session_id = ?",
                    (_now(), sid),
                )
        except Exception as e:
            raise _turn_backing_error("enroll_session", session_id=sid, err=e)
        finally:
            # AFTER the commit: re-raise and bump again, so any refresh that read
            # before the commit can never lower the flag afterwards.
            with self._presence_lock:
                self._enroll_generation += 1
                self._enrolls_in_flight -= 1
                self._any_enrolled = True

    def node_managed_backends(self, node_id: str) -> List[str]:
        """[A82 Stage 4a rework] Backends the node REGISTERED as managed-capable
        (persisted at registration, so any gateway process can resolve a carrier
        assignment even when the task server runs out of process). Only a LIVE
        carrier counts (rework 2): status online AND a heartbeat within
        `mesh.node_heartbeat_timeout_sec`; an offline/stale node ⇒ []."""
        if not node_id:
            return []
        row = self._conn().execute(
            "SELECT managed_backends FROM nodes WHERE node_id = ? "
            "AND status = 'online' AND last_heartbeat >= ?",
            (node_id, _carrier_fresh_cutoff()),
        ).fetchone()
        if row is None:
            return []
        try:
            vals = json.loads(row[0] or "[]")
        except (TypeError, ValueError):
            return []
        return [v for v in vals if isinstance(v, str)] if isinstance(vals, list) else []

    def requeue_turns_on_dead_carriers(self, limit: int = 25) -> List[str]:
        """[A82 Stage 4a rework 2] A managed row activated to a carrier that then
        went offline / stopped heart-beating must not wedge silently. An
        UNCLAIMED `pending` row (nothing started, no token) on such a carrier is
        returned to `queued` with an operator-visible `blocked_reason`
        (`carrier_offline: <node>`) and the blocked-head backoff; activation
        re-resolves the assignment when a live carrier exists again (a pinned
        session is never relocated). Claimed/running rows are the Stage-3
        recovery machinery's, untouched. Bounded (LIMIT)."""
        cutoff = _carrier_fresh_cutoff()
        moved: List[str] = []
        # Read first (no write lock): the common case — nothing to requeue — never
        # takes BEGIN IMMEDIATE on every scheduler pass.
        if self._conn().execute(
            """
            SELECT 1 FROM mesh_tasks t LEFT JOIN nodes n ON n.node_id = t.machine_id
            WHERE t.queue_protocol = 1 AND t.status = 'pending'
              AND (n.node_id IS NULL OR n.status != 'online' OR n.last_heartbeat < ?)
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone() is None:
            return moved
        try:
            with self._managed_write("requeue_turns_on_dead_carriers") as conn:
                rows = conn.execute(
                    """
                    SELECT t.id, t.machine_id FROM mesh_tasks t
                    LEFT JOIN nodes n ON n.node_id = t.machine_id
                    WHERE t.queue_protocol = 1 AND t.status = 'pending'
                      AND (n.node_id IS NULL OR n.status != 'online' OR n.last_heartbeat < ?)
                    LIMIT ?
                    """,
                    (cutoff, max(0, int(limit))),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "UPDATE mesh_tasks SET status = 'queued', activated_at = NULL, "
                        "updated_at = ? WHERE id = ? AND queue_protocol = 1 AND status = 'pending'",
                        (_now(), r["id"]),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        _apply_turn_block(conn, r["id"], f"carrier_offline: {r['machine_id']}")
                        moved.append(r["id"])
            return moved
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("requeue_turns_on_dead_carriers", err=e)

    def refresh_enrollment_presence(self) -> Optional[bool]:
        """[A82 Stage 4a rework] Reload the process-level presence flag with one
        bounded query. On failure the previous value is kept (None = unknown ⇒
        callers treat enrollment as possibly present and fail closed)."""
        with self._presence_lock:
            generation = self._enroll_generation
        try:
            row = self._conn().execute(
                "SELECT 1 FROM sessions WHERE turn_queue_enrolled = 1 LIMIT 1"
            ).fetchone()
            present = row is not None
            with self._presence_lock:
                # Never lower the flag while an enrollment is in flight or one
                # started/committed since our read began (its marker may not
                # have been visible to the read).
                if present or (
                    generation == self._enroll_generation and not self._enrolls_in_flight
                ):
                    self._any_enrolled = present
        except Exception as e:
            logger.warning("event=turn_queue_enrollment_presence_failed err=%s", e)
        return self._any_enrolled

    def any_session_enrolled(self) -> Optional[bool]:
        """True / False, or None when it could never be determined. Unknown is
        retried once per call (a single bounded read)."""
        if self._any_enrolled is None:
            return self.refresh_enrollment_presence()
        return self._any_enrolled

    def operator_stop_hold(self, session_id: str) -> Optional[str]:
        """[A82 Stage 4b rework 2] The durable operator-stop hold record of a
        session ('operator_stop') or None. One PK read."""
        row = self._conn().execute(
            "SELECT turn_queue_hold FROM sessions WHERE session_id = ?",
            ((session_id or "").strip(),),
        ).fetchone()
        return (row[0] or None) if row else None

    def heartbeat_eligible(
        self, session_id: str, *, exclude_turn_id: Optional[str] = None,
    ) -> bool:
        """[A82 Stage 4d] Is the session truly idle for OPTIONAL automation (a
        cache heartbeat)? See ``_session_idle_for_optional_turn``. Admission
        re-checks the same predicate inside its transaction (``idle_only``)
        and activation re-checks it excluding the heartbeat itself."""
        sid = (session_id or "").strip()
        if not sid:
            return False
        return _session_idle_for_optional_turn(self._conn(), sid, exclude_turn_id)

    def is_session_enrolled(self, session_id: str) -> bool:
        row = self._conn().execute(
            "SELECT turn_queue_enrolled FROM sessions WHERE session_id = ?",
            ((session_id or "").strip(),),
        ).fetchone()
        return bool(row and row[0])

    def get_active_turn(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the single managed row that OWNS the session's active slot
        (design §4: stop must resolve the ACTIVE ledger row, not the newest
        submitted id).

        Reads the one non-terminal slot-holding protocol-1 row via the
        `idx_mesh_turns_one_active_session` partial unique index. Returns None if
        the session has no active managed turn (it may still have queued rows —
        those do NOT own the slot)."""
        sid = (session_id or "").strip()
        if not sid:
            return None
        row = self._conn().execute(
            """
            SELECT * FROM mesh_tasks
            WHERE session_id = ? AND queue_protocol = 1
              AND status IN ('pending', 'claimed', 'running', 'recovery_required')
            LIMIT 1
            """,
            (sid,),
        ).fetchone()
        return dict(row) if row else None

    def claim_turn(
        self,
        task_id: str,
        node_id: str,
        carrier_kind: str,
        incarnation_id: Optional[str] = None,
    ) -> "ClaimToken":
        """Claim a managed pending/claimed turn, minting a FRESH opaque claim
        token bound to task + carrier kind + process incarnation + session
        (design §6).

        Compare-and-swap semantics:
          * `pending` → `claimed`: mints a new token, stamps carrier identity.
          * `claimed` by the SAME carrier process/incarnation that is NOT yet
            started: RE-mints a fresh token (a reoffer/reclaim SUPERSEDES the old
            token — OWN05). A `running`/terminal row cannot be re-claimed.
        Raises `TurnNotFoundError` (404) if the row is absent or not protocol-1,
        `OwnershipConflictError` (409) if it is already running/terminal."""
        from .turn_queue import ClaimToken  # local import: avoid cycle at load

        now = _now()
        token = secrets.token_hex(16)
        expired = False
        try:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT id, session_id, status, queue_protocol, payload, machine_id, "
                    "expires_at, turn_source, revision "
                    "FROM mesh_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["queue_protocol"] != 1:
                    raise TurnNotFoundError(
                        "no managed turn for claim", task_id=task_id
                    )
                status = row["status"]
                if status not in ("pending", "claimed"):
                    raise OwnershipConflictError(
                        "turn not claimable in its current state",
                        task_id=task_id, status=status,
                    )
                # [A82 Stage 4a rework] Claim independently verifies the carrier
                # assignment (design §5 step 5) — the poll filter is not a
                # guard. An unassigned managed row is claimable by nobody.
                if not node_id or row["machine_id"] != node_id:
                    raise OwnershipConflictError(
                        "turn is assigned to a different carrier",
                        task_id=task_id, assigned=row["machine_id"], claimant=node_id,
                    )
                if (
                    status == "pending" and row["expires_at"]
                    and str(row["expires_at"]) <= now and row["turn_source"] != "human"
                ):
                    # [A82 Stage 4d] Optional automation past its deadline
                    # (a heartbeat activated in time but released not-invoked
                    # while the backend was busy) is never started late: it is
                    # withdrawn here, never invoked, and frees the slot.
                    new_rev = int(row["revision"] or 1) + 1
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'withdrawn', revision = ?, completed_at = ?,
                            updated_at = ?
                        WHERE id = ? AND queue_protocol = 1 AND status = 'pending'
                        """,
                        (new_rev, now, now, task_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO mesh_turn_revisions
                            (task_id, revision, actor, change_kind, body,
                             attachments_json, created_at)
                        VALUES (?, ?, 'claim:expired', 'withdraw', NULL, NULL, ?)
                        """,
                        (task_id, new_rev, now),
                    )
                    expired = True
                if not expired:
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'claimed', claim_token = ?, claimed_by = ?,
                            claim_carrier_kind = ?, claim_incarnation = ?,
                            claimer_incarnation = ?, claimed_at = ?, updated_at = ?,
                            blocked_reason = NULL
                        WHERE id = ? AND queue_protocol = 1
                          AND status IN ('pending', 'claimed')
                          AND machine_id = ?
                        """,
                        (token, node_id, carrier_kind, incarnation_id,
                         incarnation_id, now, now, task_id, node_id),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0] == 0:
                        # Lost the compare-and-swap race (status moved under us).
                        raise OwnershipConflictError(
                            "claim lost the state race", task_id=task_id,
                        )
                    payload: Dict[str, Any] = {}
                    try:
                        payload = json.loads(row["payload"]) if row["payload"] else {}
                    except Exception:
                        payload = {}
                    return ClaimToken(
                        token,
                        task_id=task_id,
                        session_id=row["session_id"],
                        node_id=node_id,
                        carrier_kind=carrier_kind,
                        incarnation_id=incarnation_id,
                        status="claimed",
                        payload=payload,
                    )
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("claim_turn", task_id=task_id, err=e)
        # Committed withdrawal (outside the txn so it is not rolled back).
        raise OwnershipConflictError(
            "optional turn expired before claim; withdrawn", task_id=task_id,
            reason="expired",
        )

    def start_turn(
        self,
        task_id: str,
        claim_token: str,
        incarnation_id: Optional[str] = None,
    ) -> "StartAuthorization":
        """Authorize `claimed -> running` for a SPECIFIC claim token, once only
        (design §6).

        Idempotent for the SAME live token: an exact repeated start returns an
        EQUAL `StartAuthorization` (already-running with the same token), never a
        second executor (OWN02). A foreign/superseded token, a restarted-carrier
        incarnation mismatch, or a non-claimed/-running state raises
        `OwnershipConflictError` (OWN03/OWN04). `started_at` is recorded as start
        AUTHORIZED, not proof the model saw the prompt."""
        from .turn_queue import StartAuthorization

        now = _now()
        try:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT id, status, queue_protocol, claim_token, "
                    "claim_incarnation, started_at FROM mesh_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["queue_protocol"] != 1:
                    raise TurnNotFoundError("no managed turn for start", task_id=task_id)
                if row["claim_token"] != claim_token:
                    raise OwnershipConflictError(
                        "start presented a foreign/superseded token", task_id=task_id,
                    )
                if incarnation_id is not None and row["claim_incarnation"] not in (None, incarnation_id):
                    # A restarted carrier (new incarnation) cannot start an
                    # authorization minted by the old incarnation (OWN04).
                    raise OwnershipConflictError(
                        "start incarnation mismatch (carrier restarted)",
                        task_id=task_id,
                    )
                if row["status"] == "running":
                    # Idempotent repeated start for the SAME token (OWN02).
                    return StartAuthorization(
                        task_id=task_id, claim_token=claim_token,
                        started_at=row["started_at"] or now, status="running",
                    )
                if row["status"] != "claimed":
                    raise OwnershipConflictError(
                        "turn not in claimed state for start",
                        task_id=task_id, status=row["status"],
                    )
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'running', started_at = ?, updated_at = ?
                    WHERE id = ? AND queue_protocol = 1 AND status = 'claimed'
                      AND claim_token = ?
                    """,
                    (now, now, task_id, claim_token),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise OwnershipConflictError(
                        "start lost the state race", task_id=task_id,
                    )
                return StartAuthorization(
                    task_id=task_id, claim_token=claim_token,
                    started_at=now, status="running",
                )
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("start_turn", task_id=task_id, err=e)

    def release_turn(
        self,
        task_id: str,
        claim_token: str,
        *,
        backend_not_invoked: bool = False,
        node_id: Optional[str] = None,
    ) -> bool:
        """Release a CLAIMED-but-not-started managed turn back to `pending` for
        the current token only (design §6: release before start is safe only for
        the current token; release AFTER start is forbidden without quiescence).

        Returns True if released; a started/running or foreign-token row is left
        untouched and returns False (the caller must go through recovery).

        [A82 Stage 3 rework] ``backend_not_invoked=True`` is the carrier's
        write-ahead attestation that the backend was NEVER invoked for this
        attempt (persisted before invocation) — the strongest quiescence evidence
        — so a ``running``/``recovery_required`` row of the claiming node + token
        may also return to pending (start response lost, or conflict detected
        before submit). The prompt is preserved; token/carrier/incarnation/start
        are cleared so the old attempt can do nothing further."""
        now = _now()
        try:
            with self._write() as conn:
                # [A82 Stage 4b] An attempt the operator cancelled is never
                # re-offered: releasing it (nothing ran) ends it `cancelled`.
                cancelled = conn.execute(
                    "SELECT 1 FROM mesh_tasks WHERE id = ? AND queue_protocol = 1 "
                    "AND cancel_token IS NOT NULL AND cancel_token = ? AND claim_token = ?",
                    (task_id, claim_token, claim_token),
                ).fetchone() is not None
                if cancelled:
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'cancelled', completed_at = ?, updated_at = ?,
                            error = COALESCE(error, 'cancelled by operator (backend not invoked)')
                        WHERE id = ? AND queue_protocol = 1 AND claim_token = ?
                          AND (claimed_by = ? OR ? = 0)
                          AND status IN ('claimed', 'running', 'recovery_required')
                          AND (? = 1 OR (status = 'claimed' AND started_at IS NULL))
                        """,
                        (now, now, task_id, claim_token, node_id or "",
                         1 if backend_not_invoked else 0, 1 if backend_not_invoked else 0),
                    )
                    return conn.execute("SELECT changes()").fetchone()[0] > 0
                if backend_not_invoked:
                    conn.execute(
                        """
                        UPDATE mesh_tasks
                        SET status = 'pending', claim_token = NULL, claimed_by = NULL,
                            claim_carrier_kind = NULL, claim_incarnation = NULL,
                            claimer_incarnation = NULL, claimed_at = NULL,
                            started_at = NULL, blocked_reason = NULL, updated_at = ?
                        WHERE id = ? AND queue_protocol = 1 AND claim_token = ?
                          AND claimed_by = ?
                          AND status IN ('claimed', 'running', 'recovery_required')
                        """,
                        (now, task_id, claim_token, node_id or ""),
                    )
                    return conn.execute("SELECT changes()").fetchone()[0] > 0
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'pending', claim_token = NULL, claimed_by = NULL,
                        claim_carrier_kind = NULL, claim_incarnation = NULL,
                        claimer_incarnation = NULL, claimed_at = NULL, updated_at = ?
                    WHERE id = ? AND queue_protocol = 1 AND status = 'claimed'
                      AND claim_token = ? AND started_at IS NULL
                    """,
                    (now, task_id, claim_token),
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception as e:
            raise _turn_backing_error("release_turn", task_id=task_id, err=e)

    def complete_turn(
        self,
        task_id: str,
        claim_token: str,
        result: Dict[str, Any],
        *,
        status: str = "completed",
        native_session_id: Optional[str] = None,
        error: Optional[str] = None,
        artifact_path: Optional[str] = None,
    ) -> "CompletionResult":
        """ATOMIC managed completion (design §6, A82 §15 decision 2).

        In ONE transaction: verify current token + allowed state, write the
        canonical outcome, commit the native session id + active identity onto
        the session row (field-scoped — does NOT clobber concurrent model/pin/
        close changes), and transition the task terminal + release the slot.

        Idempotency & fencing:
          * Identical repeated completion for the current token, already
            terminal ⇒ returns an EQUAL `CompletionResult` (OWN06) — no re-write,
            no duplicated session side effects.
          * A superseded/foreign token ⇒ `OwnershipConflictError` (OWN05).
          * A never-started (queued/pending) turn ⇒ `OwnershipConflictError`
            (OWN08b: managed completion refuses a result for a non-running turn).
        Raises typed failures instead of swallowing — a losing predicate NEVER
        returns silently as success (unlike legacy `complete_task`)."""
        from .turn_queue import CompletionResult, TERMINAL_STATUSES

        if status not in TERMINAL_STATUSES:
            raise MalformedTurnError(
                "complete_turn given a non-terminal status", status=status,
            )
        now = _now()
        try:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT id, session_id, status, queue_protocol, claim_token, cancel_token "
                    "FROM mesh_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["queue_protocol"] != 1:
                    raise TurnNotFoundError("no managed turn for complete", task_id=task_id)
                if row["claim_token"] != claim_token:
                    # Superseded / foreign token, or a never-claimed turn (token
                    # is NULL) ⇒ refuse (OWN05, OWN08b).
                    raise OwnershipConflictError(
                        "complete presented a foreign/superseded token or the "
                        "turn was never claimed",
                        task_id=task_id,
                    )
                cur_status = row["status"]
                if cur_status in TERMINAL_STATUSES:
                    # Already terminal for the SAME token ⇒ idempotent replay
                    # (OWN06). No re-write, no duplicated session side effects.
                    return CompletionResult(
                        task_id=task_id, status=cur_status,
                        native_session_id=native_session_id,
                    )
                if cur_status not in ("running", "recovery_required"):
                    # A claimed-but-never-started turn cannot be completed —
                    # only a started/held turn produces a result (OWN08b).
                    raise OwnershipConflictError(
                        "turn not in a completable state (never started)",
                        task_id=task_id, status=cur_status,
                    )
                # [A82 Stage 4b] The attempt was cancelled by the operator
                # (recorded against THIS token): its failed/interrupted result
                # is truthfully `cancelled`. A turn that finished successfully
                # before the interrupt landed stays `completed`.
                if status == "failed" and _cancel_requested_for(row, claim_token):
                    status = "cancelled"
                # Write the canonical outcome + terminal transition.
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = ?, result = ?, error = COALESCE(?, error),
                        artifact_path = COALESCE(?, artifact_path),
                        completed_at = ?, updated_at = ?
                    WHERE id = ? AND queue_protocol = 1 AND claim_token = ?
                      AND status IN ('running', 'recovery_required')
                    """,
                    (status, json.dumps(result), error, artifact_path,
                     now, now, task_id, claim_token),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise OwnershipConflictError(
                        "completion lost the state race", task_id=task_id,
                    )
                # ATOMICALLY commit the native session id + active identity onto
                # the session row — field-scoped so a stale full-session save
                # cannot revert it (design §6 / OWN08). Only touch columns this
                # completion OWNS; concurrent model/effort/pin/close writers use
                # their own scoped seams and are preserved.
                sid = row["session_id"]
                if sid:
                    self._commit_completion_identity(
                        conn, sid, native_session_id=native_session_id,
                        last_task_id=task_id,
                    )
                return CompletionResult(
                    task_id=task_id, status=status,
                    native_session_id=native_session_id,
                )
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("complete_turn", task_id=task_id, err=e)

    def _commit_completion_identity(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        native_session_id: Optional[str] = None,
        last_task_id: Optional[str] = None,
    ) -> None:
        """Field-scoped write of the completion-owned session identity columns,
        executed INSIDE the caller's transaction (design §6 atomicity).

        Sets ONLY the native/backend session id + last_task_id + a bumped
        config_revision — never the whole row — so a concurrent stale
        whole-session upsert cannot revert it and a queued model/pin/close change
        is not clobbered. Mirrors the `set_session_case` field-ownership pattern."""
        sets = ["updated_at = ?", "config_revision = config_revision + 1"]
        params: List[Any] = [_now()]
        if native_session_id is not None:
            # [A82 Stage 4b] A session closed meanwhile keeps its cleared native
            # id (legacy close parity: no resume path may pick up a closed
            # backend session), even when its cancelled turn reports later.
            sets.insert(
                0, "backend_session_id = CASE WHEN status = 'closed' "
                   "THEN backend_session_id ELSE ? END",
            )
            params.insert(0, native_session_id)
        if last_task_id is not None:
            sets.insert(0, "last_task_id = ?")
            params.insert(0, last_task_id)
        params.append(session_id)
        conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?",
            params,
        )

    def update_session_fields(
        self,
        session_id: str,
        *,
        native_session_id: Optional[str] = None,
        last_task_id: Optional[str] = None,
    ) -> bool:
        """Standalone field-scoped session identity update (design §6 / OWN09).

        A completion-owned, versioned write of exactly the identity columns — the
        sanctioned defence against a stale whole-session save reverting canonical
        completion. Bumps `config_revision`. Returns True on a matched row."""
        sid = (session_id or "").strip()
        if not sid:
            return False
        try:
            with self._write() as conn:
                self._commit_completion_identity(
                    conn, sid, native_session_id=native_session_id,
                    last_task_id=last_task_id,
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception as e:
            raise _turn_backing_error("update_session_fields", session_id=sid, err=e)

    def enter_recovery(
        self,
        task_id: str,
        claim_token: str,
        reason: str = "",
    ) -> bool:
        """Transition a claimed/running managed turn to `recovery_required`
        (design §6: after start uncertainty, HOLD the slot).

        recovery_required is NOT terminal — it retains the session's active slot
        until quiescence/result is established. Only the current token can move
        its own attempt into recovery. Returns True on transition."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'recovery_required', blocked_reason = ?, updated_at = ?
                    WHERE id = ? AND queue_protocol = 1 AND claim_token = ?
                      AND status IN ('claimed', 'running')
                    """,
                    ((reason or "")[:500], now, task_id, claim_token),
                )
                return conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception as e:
            raise _turn_backing_error("enter_recovery", task_id=task_id, err=e)

    def resolve_recovery(
        self,
        task_id: str,
        claim_token: str,
        quiescence_evidence: Optional[Dict[str, Any]] = None,
        *,
        resolved_status: str = "cancelled",
    ) -> "RecoveryResolution":
        """Operator recovery resolution (design §6).

        Requires the CURRENT token PLUS a recorded authenticated quiescence
        observation (or a durable terminal result). A bare boolean / None / free
        text / offline label is INSUFFICIENT ⇒ `OwnershipConflictError` (409,
        missing evidence — OWN10). Resolves conditionally to the observed terminal
        outcome; it NEVER implicitly replays the prompt (an explicit retry is a
        separate linked request, handled by Stage 4)."""
        from .turn_queue import RecoveryResolution, TERMINAL_STATUSES

        if not _is_quiescence_evidence(quiescence_evidence):
            raise OwnershipConflictError(
                "recovery resolution requires recorded quiescence evidence or a "
                "durable terminal result (a bare boolean/offline label is "
                "insufficient)",
                task_id=task_id,
            )
        if resolved_status not in TERMINAL_STATUSES:
            raise MalformedTurnError(
                "resolve_recovery given a non-terminal status",
                status=resolved_status,
            )
        now = _now()
        try:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT id, status, queue_protocol, claim_token, cancel_token "
                    "FROM mesh_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None or row["queue_protocol"] != 1:
                    raise TurnNotFoundError("no managed turn for recovery", task_id=task_id)
                if row["claim_token"] != claim_token:
                    raise OwnershipConflictError(
                        "recovery presented a foreign/superseded token",
                        task_id=task_id,
                    )
                if row["status"] != "recovery_required":
                    raise OwnershipConflictError(
                        "turn is not in recovery_required",
                        task_id=task_id, status=row["status"],
                    )
                # [A82 Stage 4b] An operator cancel recorded against THIS
                # attempt makes a result-less failed resolution truthful as
                # `cancelled`.
                if resolved_status == "failed" and _cancel_requested_for(row, claim_token):
                    resolved_status = "cancelled"
                # [A82 Stage 3 rework] Record the evidence/decision that
                # resolved the hold (bounded) on the row itself.
                evidence_note = "recovery_resolved: " + json.dumps(
                    {k: v for k, v in (quiescence_evidence or {}).items()
                     if k not in ("claim_token", "result")},
                    default=str, sort_keys=True,
                )[:1800]
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = ?, completed_at = ?, updated_at = ?,
                        blocked_reason = NULL, error = ?
                    WHERE id = ? AND queue_protocol = 1 AND claim_token = ?
                      AND status = 'recovery_required'
                    """,
                    (resolved_status, now, now, evidence_note, task_id, claim_token),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise OwnershipConflictError(
                        "recovery resolution lost the state race", task_id=task_id,
                    )
                return RecoveryResolution(
                    task_id=task_id, resolved_status=resolved_status,
                )
        except TurnQueueError:
            raise
        except Exception as e:
            raise _turn_backing_error("resolve_recovery", task_id=task_id, err=e)

    def get_pending_tasks(
        self,
        node_id: Optional[str] = None,
        backends: Optional[List[str]] = None,
        accept_unpinned: bool = True,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Return pending tasks routable to this node.

        machine_id=NULL means any node can claim it.
        machine_id=<node_id> means only that node can claim it (session affinity).
        """
        params: List[Any] = []
        machine_clause = ""
        if node_id:
            if accept_unpinned:
                machine_clause = "AND (machine_id IS NULL OR machine_id = ?)"
            else:
                machine_clause = "AND machine_id = ?"
            params.append(node_id)

        backend_clause = ""
        if backends:
            placeholders = ",".join("?" * len(backends))
            backend_clause = f"AND backend IN ({placeholders})"
            params.extend(backends)

        params.append(limit)
        rows = self._conn().execute(
            f"""
            SELECT * FROM mesh_tasks
            WHERE status = 'pending'
            AND queue_protocol = 0
            {machine_clause}
            {backend_clause}
            ORDER BY created_at ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_managed_turns(
        self,
        node_id: Optional[str] = None,
        backends: Optional[List[str]] = None,
        accept_unpinned: bool = True,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """[A82 Stage 3] Return protocol-1 (managed) PENDING turns routable to a
        carrier that negotiated managed-protocol support (design §6/§7).

        Distinct from :meth:`get_pending_tasks` (which is protocol-0 ONLY) so a
        legacy carrier that never advertised protocol-1 can never see a managed
        row. The execution credential is STRIPPED from this poll view — the claim
        RESPONSE is the only channel that carries the token (design §3.13). The
        carrier executes the claim response's frozen payload, not this snapshot.
        """
        params: List[Any] = []
        machine_clause = ""
        if node_id:
            if accept_unpinned:
                machine_clause = "AND (machine_id IS NULL OR machine_id = ?)"
            else:
                machine_clause = "AND machine_id = ?"
            params.append(node_id)
        backend_clause = ""
        if backends:
            placeholders = ",".join("?" * len(backends))
            backend_clause = f"AND backend IN ({placeholders})"
            params.extend(backends)
        params.append(limit)
        rows = self._conn().execute(
            f"""
            SELECT * FROM mesh_tasks
            WHERE status = 'pending'
            AND queue_protocol = 1
            {machine_clause}
            {backend_clause}
            ORDER BY queue_sequence ASC, created_at ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # Never serialize the execution credential / admission material
            # through a poll view (design §3.13 — the claim response is the only
            # place the token may appear).
            for _secret in ("claim_token", "idempotency_key", "admission_hash"):
                d.pop(_secret, None)
            if isinstance(d.get("payload"), str):
                try:
                    d["payload"] = json.loads(d["payload"])
                except Exception:
                    pass
            out.append(d)
        return out

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM mesh_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_task_by_session(self, session_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Return a task row matching both session_id and task_id."""
        row = self._conn().execute(
            "SELECT * FROM mesh_tasks WHERE session_id = ? AND id = ?",
            (session_id, task_id),
        ).fetchone()
        return dict(row) if row else None

    def enrich_task(
        self,
        task_id: str,
        *,
        prompt: Optional[str] = None,
        reply_text: Optional[str] = None,
        parsed_output: Any = None,
        file_changes: Any = None,
        files_modified: Any = None,
        usage: Any = None,
        error_class: Optional[str] = None,
        return_code: Optional[int] = None,
    ) -> None:
        """Populate the artifact-complete columns on a task row.

        This is the DB side of the file-free conversation/artifact store: the
        orchestrator calls this at turn completion (and the backfill calls it for
        historical tasks) so ``mesh_tasks`` holds everything ``results/task_*.json``
        used to hold — full untruncated ``reply_text``, ``parsed_output``,
        per-file ``file_changes``, token ``usage`` — without the 264 MB of raw
        NDJSON. COALESCE keeps existing values when a field isn't supplied so this
        is safe to call repeatedly / partially (idempotent backfill).
        """
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks SET
                        prompt              = COALESCE(?, prompt),
                        reply_text          = COALESCE(?, reply_text),
                        parsed_output_json  = COALESCE(?, parsed_output_json),
                        file_changes_json   = COALESCE(?, file_changes_json),
                        files_modified_json = COALESCE(?, files_modified_json),
                        usage_json          = COALESCE(?, usage_json),
                        error_class         = COALESCE(?, error_class),
                        return_code         = COALESCE(?, return_code),
                        updated_at          = ?
                    WHERE id = ?
                    """,
                    (
                        prompt,
                        reply_text,
                        json.dumps(parsed_output) if parsed_output is not None else None,
                        json.dumps(file_changes) if file_changes is not None else None,
                        json.dumps(files_modified) if files_modified is not None else None,
                        json.dumps(usage) if usage is not None else None,
                        error_class,
                        return_code,
                        _now(),
                        task_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_enrich_task_failed task_id=%s err=%s", task_id, e)

    def enrich_tasks_batch(self, rows: List[Dict[str, Any]]) -> int:
        """Enrich many task rows in ONE transaction (fast backfill path).

        Each dict: {task_id, prompt?, reply_text?, parsed_output?, file_changes?,
        files_modified?, usage?, error_class?, return_code?}. Same COALESCE
        semantics as enrich_task. Returns the count attempted. Wrapping all
        UPDATEs in a single BEGIN IMMEDIATE avoids 900+ fsync/commit cycles —
        the difference between seconds and minutes on a server with WAL.
        """
        if not rows:
            return 0
        now = _now()
        try:
            with self._write() as conn:
                for r in rows:
                    conn.execute(
                        """
                        UPDATE mesh_tasks SET
                            prompt              = COALESCE(?, prompt),
                            reply_text          = COALESCE(?, reply_text),
                            parsed_output_json  = COALESCE(?, parsed_output_json),
                            file_changes_json   = COALESCE(?, file_changes_json),
                            files_modified_json = COALESCE(?, files_modified_json),
                            usage_json          = COALESCE(?, usage_json),
                            error_class         = COALESCE(?, error_class),
                            return_code         = COALESCE(?, return_code),
                            updated_at          = ?
                        WHERE id = ?
                        """,
                        (
                            r.get("prompt"),
                            r.get("reply_text"),
                            json.dumps(r["parsed_output"]) if r.get("parsed_output") is not None else None,
                            json.dumps(r["file_changes"]) if r.get("file_changes") is not None else None,
                            json.dumps(r["files_modified"]) if r.get("files_modified") is not None else None,
                            json.dumps(r["usage"]) if r.get("usage") is not None else None,
                            r.get("error_class"),
                            r.get("return_code"),
                            now,
                            r["task_id"],
                        ),
                    )
        except Exception as e:
            logger.warning("event=db_enrich_tasks_batch_failed err=%s", e)
        return len(rows)

    def existing_task_ids(self) -> set:
        """All task ids present in mesh_tasks (one query — backfill membership test)."""
        try:
            rows = self._conn().execute("SELECT id FROM mesh_tasks").fetchall()
            return {r[0] for r in rows}
        except Exception:
            return set()

    def get_session_turns(self, session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Return the session's tasks as conversation turns, oldest→newest.

        The conversation is a projection of the task ledger: each task row yields
        one user turn (``prompt``) and one assistant turn (``reply_text``). This is
        the file-free replacement for ``transcript.get_transcript`` — no artifact
        files, no NDJSON parsing. Rows lacking ``reply_text`` (not yet backfilled)
        are returned with ``reply_text=None`` so the caller can fall back.
        """
        # Slim list projection (A81): only the columns the transcript LIST view
        # actually renders. The large `parsed_output_json`/`file_changes_json`
        # blobs (and unused `error_class`/`return_code`/`session_id`) are NOT read
        # by either caller (`transcript._turns_from_db`, backfill script), so they
        # are omitted to keep this per-poll read cheap. `reply_text` (full chat
        # text), `result` (legacy fallback), `files_modified_json` (file_count) and
        # `usage_json` (summary) are the fields that are consumed.
        rows = self._conn().execute(
            """
            SELECT id AS task_id, prompt, reply_text,
                   files_modified_json, usage_json, status, result, action,
                   created_at, completed_at
            FROM mesh_tasks
            WHERE session_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session_turns_tail(self, session_id: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Return the session's most-recent ``limit`` *completed* turns, oldest→newest.

        Used by the restart-recovery context injector: when a session's driver was
        lost on a worker restart and the next task starts a fresh SDK session, the
        injector calls this to build a bounded ``<prior_context>`` block so the new
        agent knows where it left off.

        Only returns rows where ``reply_text IS NOT NULL AND status = 'completed'``
        — incomplete or failed turns are excluded so the injector never injects a
        broken half-turn. Fetches DESC then reverses so the slice is oldest→newest
        (the existing ``get_session_turns`` orders ASC from the front and would
        return the *oldest* N rows, not the most recent).
        """
        rows = self._conn().execute(
            """
            SELECT id AS task_id, prompt, reply_text, created_at
            FROM mesh_tasks
            WHERE session_id = ?
              AND reply_text IS NOT NULL
              AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def list_tasks(
        self,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM mesh_tasks {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        # [A82] Never serialize the managed-turn execution credential (or the
        # idempotency/admission material that could aid forgery/replay) through
        # this operator list surface (/api/tasks). The claim token is an
        # execution credential (design §6/§3.13); the carrier claim response is
        # the only place it may appear.
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            for _secret in ("claim_token", "idempotency_key", "admission_hash"):
                d.pop(_secret, None)
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # FlowRun record (v0.4 §13 item 1, A19) — one row per dispatch flow.
    #
    # This is a RECORD, not a driver/stage-machine. Nothing in the codebase
    # reads current_stage to decide what runs next; these methods only persist
    # and query the flow-state row. The orchestrator write hook is best-effort
    # (try/except) so a failure here can never fail or delay a real task.
    # ------------------------------------------------------------------

    # Full v0.4 §11 field set + dispatch lineage (A21). All optional and
    # NULLable — an A19-style 5-arg write leaves every one of these NULL.
    # Structured fields (plan_review, burn_down_items, execution_result,
    # implementation_review, waived_findings, role_assignments, artifact_links)
    # are stored as caller-supplied JSON-encoded TEXT; db.py does not interpret
    # them. These are RECORD columns: nothing reads them to drive execution.
    _FLOW_EXTRA_FIELDS = (
        "approved_plan",
        "plan_review",
        "burn_down_items",
        "execution_result",
        "implementation_review",
        "waived_findings",
        "closure_summary",
        "role_assignments",
        "artifact_links",
        "status",
        "parent_flow_run_id",
        "dispatched_by",
        "dispatch_file",
        "completion_criteria",
    )

    # [A36] A Case (flow_run) is considered CLOSED — and therefore ineligible for
    # a new turn to attach to — only in these terminal statuses. A NULL status or
    # 'blocked' (needs-attention) is still OPEN: a follow-up turn on a blocked Case
    # legitimately attaches to the SAME Case rather than minting a replacement.
    _CLOSED_STATUSES = ("closed", "cancelled")

    def create_flow_run(
        self,
        task_id: str,
        current_stage: str,
        objective_lock: Optional[str] = None,
        **fields: Optional[str],
    ) -> str:
        """Insert a new flow_runs row. Returns the generated flow_run_id.

        The A19 3-arg form (task_id, current_stage, objective_lock) is
        unchanged. Any of the §11/lineage columns in _FLOW_EXTRA_FIELDS may be
        passed as keyword args; absent ones stay NULL. updated_at is left NULL
        on create (it marks a later update).
        """
        unknown = set(fields) - set(self._FLOW_EXTRA_FIELDS)
        if unknown:
            raise ValueError(f"unknown flow_run field(s): {sorted(unknown)}")

        flow_run_id = uuid.uuid4().hex
        cols = ["flow_run_id", "task_id", "current_stage", "objective_lock", "created_at"]
        vals = [flow_run_id, task_id, current_stage, objective_lock, _now()]
        for name in self._FLOW_EXTRA_FIELDS:
            if name in fields:
                cols.append(name)
                vals.append(fields[name])
        placeholders = ", ".join("?" for _ in cols)
        with self._write() as conn:
            conn.execute(
                f"INSERT INTO flow_runs ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
        return flow_run_id

    def get_or_create_task_flow_run(
        self,
        task_id: str,
        current_stage: str,
        objective_lock: Optional[str] = None,
        **fields: Optional[str],
    ) -> str:
        """[A82 Stage 4a rework 3] Convergent flow_run birth keyed on the task:
        return the flow_run already created FOR ``task_id``, else create it — in
        ONE write transaction, so two concurrent/stalled managed-lineage writers
        converge on the SAME Case and never birth a second one. Raises on DB
        error (the managed lineage path must not swallow)."""
        unknown = set(fields) - set(self._FLOW_EXTRA_FIELDS)
        if unknown:
            raise ValueError(f"unknown flow_run field(s): {sorted(unknown)}")
        with self._write() as conn:
            row = conn.execute(
                "SELECT flow_run_id FROM flow_runs WHERE task_id = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row is not None:
                return str(row[0])
            flow_run_id = uuid.uuid4().hex
            cols = ["flow_run_id", "task_id", "current_stage", "objective_lock", "created_at"]
            vals: List[Any] = [flow_run_id, task_id, current_stage, objective_lock, _now()]
            for name in self._FLOW_EXTRA_FIELDS:
                if name in fields:
                    cols.append(name)
                    vals.append(fields[name])
            conn.execute(
                f"INSERT INTO flow_runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                vals,
            )
            return flow_run_id

    def append_flow_event_once(
        self,
        flow_run_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """[A82 Stage 4a rework 3] Idempotent append for the managed lineage:
        the event keyed by (flow_run_id, event_type, entity_type, entity_id) is
        written at most once (existence check + insert in one transaction).
        Returns the existing or new id. Raises on DB error."""
        payload_json = json.dumps(payload) if payload is not None else None
        with self._write() as conn:
            row = conn.execute(
                "SELECT id FROM flow_events WHERE flow_run_id = ? AND event_type = ? "
                "AND entity_type IS ? AND entity_id IS ? LIMIT 1",
                (flow_run_id, event_type, entity_type, entity_id),
            ).fetchone()
            if row is not None:
                return int(row[0])
            cur = conn.execute(
                """
                INSERT INTO flow_events (
                    flow_run_id, event_type, actor, from_state, to_state,
                    entity_type, entity_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (flow_run_id, event_type, actor, from_state, to_state,
                 entity_type, entity_id, payload_json, _now()),
            )
            return int(cur.lastrowid)

    def update_flow_stage(self, flow_run_id: str, current_stage: str) -> None:
        """Update the current_stage of an existing flow_runs row (A19 path).

        Also stamps updated_at so a stage transition is timestamped.
        """
        with self._write() as conn:
            conn.execute(
                "UPDATE flow_runs SET current_stage = ?, updated_at = ? WHERE flow_run_id = ?",
                (current_stage, _now(), flow_run_id),
            )

    def update_flow_run(self, flow_run_id: str, **fields: Optional[str]) -> None:
        """Update any subset of the §11/lineage columns (and/or current_stage).

        Only the passed fields are written; each update stamps updated_at. A
        RECORD write only — nothing reads these fields to drive execution.
        """
        allowed = set(self._FLOW_EXTRA_FIELDS) | {"current_stage", "objective_lock"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown flow_run field(s): {sorted(unknown)}")
        if not fields:
            return
        set_cols = list(fields.keys())
        assignments = ", ".join(f"{c} = ?" for c in set_cols) + ", updated_at = ?"
        params = [fields[c] for c in set_cols] + [_now(), flow_run_id]
        with self._write() as conn:
            conn.execute(
                f"UPDATE flow_runs SET {assignments} WHERE flow_run_id = ?",
                params,
            )

    def get_flow_run(self, flow_run_id: str) -> Optional[Dict[str, Any]]:
        """Read a single flow_runs row by id, or None if absent."""
        row = self._conn().execute(
            "SELECT * FROM flow_runs WHERE flow_run_id = ?",
            (flow_run_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_flow_runs(
        self,
        task_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Read path for flow_runs. Optional task_id filter; newest first."""
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM flow_runs {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_child_flow_runs(
        self,
        parent_flow_run_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Reverse-lookup: flow_runs whose parent_flow_run_id is the given id.

        This is the child→parent recovery path (M2 dispatch lineage): given a
        Manager/parent flow_run, list the child flows it dispatched. Read-only —
        nothing reads these rows to drive execution. Oldest-first (dispatch order).
        """
        rows = self._conn().execute(
            "SELECT * FROM flow_runs WHERE parent_flow_run_id = ? "
            "ORDER BY created_at ASC LIMIT ?",
            (parent_flow_run_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Work Control Substrate (A25) — authoritative case relationships +
    # append-only case audit trail. flow_links relate a case (flow_run) to
    # EXISTING gateway entities (task/session/approval/artifact/job/flow); it is
    # NOT a second task ledger. flow_events are append-only lifecycle evidence.
    # A RECORD/relationship layer only: nothing here is read to DRIVE execution.
    # ------------------------------------------------------------------

    def create_flow_link(
        self,
        flow_run_id: str,
        entity_type: str,
        entity_id: str,
        role: str,
        created_by: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Create (or reuse) an authoritative case↔entity link. Idempotent.

        The unique index on (flow_run_id, entity_type, entity_id, role) makes a
        repeat call a no-op — the existing row's id is returned rather than
        raising or duplicating. Returns the link id, or None on unexpected error.
        `metadata` is JSON-encoded verbatim; db.py does not interpret it.
        """
        payload = json.dumps(metadata) if metadata is not None else None
        with self._write() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO flow_links (
                    flow_run_id, entity_type, entity_id, role,
                    created_at, created_by, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (flow_run_id, entity_type, entity_id, role,
                 _now(), created_by, payload),
            )
            if cur.rowcount:
                return int(cur.lastrowid)
            # Already existed (unique conflict ignored) — return the existing id.
            row = conn.execute(
                """
                SELECT id FROM flow_links
                WHERE flow_run_id = ? AND entity_type = ? AND entity_id = ? AND role = ?
                """,
                (flow_run_id, entity_type, entity_id, role),
            ).fetchone()
            return int(row["id"]) if row is not None else None

    def list_flow_links(
        self,
        flow_run_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        role: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List authoritative case links, optionally filtered. Oldest-first.

        With `flow_run_id` this is the forward lookup (all entities linked to a
        case); with `entity_type`+`entity_id` it is the reverse lookup (which
        cases reference this entity). Read-only.
        """
        clauses, params = [], []
        if flow_run_id:
            clauses.append("flow_run_id = ?")
            params.append(flow_run_id)
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if role:
            clauses.append("role = ?")
            params.append(role)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM flow_links {where} ORDER BY id ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_session_case_links(
        self,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Reverse index: EVERY session→case link joined to its case summary.

        This is the authoritative, whole-substrate session affiliation source.
        A single JOIN of ``flow_links`` (entity_type='session') to ``flow_runs``
        replaces the frontend's old N+1 per-case fanout AND its 100-case cap — so
        a session linked to a case anywhere in the backlog is resolved, never
        rendered a false Standalone (milestone authority rule 7). Read-only.

        ``limit`` is None by default (unbounded — no artificial cap); pass an int
        only for a defensive ceiling. Ordered by link id DESC (NEWEST link first):
        a long-lived session that worked several cases resolves to its MOST RECENT
        one (the builder keeps the first per session) — the useful "what is it on
        now?" answer, and deterministic.

        [A37] The "most-recent" resolution is no longer a SHATTER MASK: A36 stopped
        the per-turn Case mint, so a session now carries ONE authoritative session
        link per Case (from ``open_case``/birth), never one per turn. The durable
        ``sessions.current_case_id`` is the canonical "current Case"; this derived
        index remains for the whole-substrate Sessions surface and history.
        """
        sql = (
            "SELECT fl.flow_run_id AS flow_run_id, fl.entity_id AS session_id, "
            "       fl.role AS role, fl.created_at AS created_at, "
            "       fr.objective_lock AS objective_lock, fr.status AS status, "
            "       fr.current_stage AS current_stage "
            "FROM flow_links fl "
            "LEFT JOIN flow_runs fr ON fr.flow_run_id = fl.flow_run_id "
            "WHERE fl.entity_type = 'session' "
            "ORDER BY fl.id DESC"
        )
        params: List[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def find_open_case_for_session(self, session_id: str) -> Optional[str]:
        """[A36] The session's newest still-OPEN Case, or None.

        Returns the ``flow_run_id`` of the most recent Case (flow_run) linked to
        this session via ``flow_links`` (entity_type='session') whose status is
        NOT in ``_CLOSED_STATUSES`` (NULL/'blocked' count as open — see the
        constant). This is the lookup-or-reuse the admission path (A36) uses to
        ATTACH a turn to an existing Case instead of minting one per turn.

        Read-only; served by ``idx_flow_links_entity``. A blank session_id ⇒ None
        (⇒ the caller takes the Case-less standalone path). Never raises: any DB
        error is swallowed to None so admission can always fall back to standalone.
        """
        sid = (session_id or "").strip()
        if not sid:
            return None
        placeholders = ", ".join("?" for _ in self._CLOSED_STATUSES)
        try:
            row = self._conn().execute(
                f"""
                SELECT fl.flow_run_id AS flow_run_id
                FROM flow_links fl
                JOIN flow_runs fr ON fr.flow_run_id = fl.flow_run_id
                WHERE fl.entity_type = 'session' AND fl.entity_id = ?
                  AND (fr.status IS NULL OR fr.status NOT IN ({placeholders}))
                ORDER BY fl.id DESC
                LIMIT 1
                """,
                (sid, *self._CLOSED_STATUSES),
            ).fetchone()
            return str(row["flow_run_id"]) if row is not None else None
        except Exception as e:
            logger.warning(
                "event=find_open_case_failed session_id=%s err=%s", sid, e,
            )
            return None

    def open_case(
        self,
        objective: str,
        session_id: str,
        role: str = "manager",
        completion_criteria: Optional[str] = None,
        round_cap: Optional[int] = None,
    ) -> str:
        """[A36] The ONLY sanctioned Case-birth path.

        Creates exactly one Case (flow_run) for an explicit managed objective and
        attaches ``session_id`` to it in ``role`` (manager|worker|reviewer). Unlike
        the retired per-turn mint, this is called deliberately by a managed
        entrypoint (the Manager role, M3.1) — never unconditionally inside the
        enqueue path. The optional ``completion_criteria`` (MAX salvage — the
        checkable "done" condition) is persisted on the Case and later demanded by
        ``close_case`` in A37. Returns the new flow_run_id.

        [M3.4/A52] The optional ``round_cap`` is the autonomous-continuation
        backstop (:func:`case_round_cap`). When given it is folded INTO
        ``completion_criteria`` as the JSON object ``{"round_cap": N, "criteria": …}``
        — no new column — so the human done-gate and the machine cap ride the same
        field. When ``round_cap`` is None the criteria is stored verbatim
        (byte-identical to the pre-A52 behaviour).

        Writes the Case row + the session link + a ``flow.created`` event as one
        logical birth. current_stage starts at 'objective_lock' (the objective is
        locked at open); status stays NULL (open). task_id is NULL — a Case is an
        objective, not a task.
        """
        stored_criteria = _compose_completion_criteria(completion_criteria, round_cap)
        flow_run_id = self.create_flow_run(
            None,
            "objective_lock",
            objective_lock=objective,
            completion_criteria=stored_criteria,
        )
        self.create_flow_link(
            flow_run_id, "session", session_id, role, created_by="manager",
        )
        self.append_flow_event(
            flow_run_id, "flow.created", "manager",
            to_state="objective_lock",
            entity_type="session", entity_id=session_id,
            payload={"role": role, "objective": objective},
        )
        return flow_run_id

    def _case_has_unresolved_approval(self, flow_run_id: str) -> bool:
        """[A37] Whether the Case has an approval linked to it still 'pending'.

        Uses the authoritative approval→case links (entity_type='approval'; written
        by the approval service) joined to the approvals row in a SINGLE indexed
        query (no per-link fanout — CLAUDE.md §8). Read-only; swallows errors to
        False so a lookup glitch never falsely blocks a close (the caller's other
        guards still apply)."""
        try:
            # An approval whose expires_at has passed no longer blocks a close: an
            # ignored proposal must not wedge a Case forever (the expires_at column
            # existed but nothing enforced it). NULL expiry still blocks (a genuine
            # open-ended gate); expire_stale_approvals() flips past-due rows to
            # 'expired' so the queue is honest, not just silently ignored here.
            row = self._conn().execute(
                """
                SELECT 1 FROM flow_links fl
                JOIN approvals a ON a.id = fl.entity_id
                WHERE fl.flow_run_id = ? AND fl.entity_type = 'approval'
                  AND a.status = 'pending'
                  AND (a.expires_at IS NULL OR a.expires_at > ?)
                LIMIT 1
                """,
                (flow_run_id, _now()),
            ).fetchone()
            return row is not None
        except Exception as e:
            logger.warning(
                "event=case_approval_guard_failed flow_run_id=%s err=%s",
                flow_run_id, e,
            )
        return False

    def cancel_case_pending_approvals(
        self, flow_run_id: str, *, resolved_by: str = "case_closed",
    ) -> int:
        """Cancel every still-pending approval linked to a Case. Returns the count.

        An explicit operator close/interrupt of a Case makes any dangling proposal
        (respawn/resume) moot — cancelling them is the honest resolution, and it is
        what unwedges a Case whose ignored proposal would otherwise block the
        criteria-gated close. Terminal transition only (pending → 'cancelled'); an
        already-resolved approval is untouched. Appends one audit event per Case."""
        now = _now()
        with self._write() as conn:
            ids = [
                str(r["entity_id"]) for r in conn.execute(
                    """
                    SELECT fl.entity_id FROM flow_links fl
                    JOIN approvals a ON a.id = fl.entity_id
                    WHERE fl.flow_run_id = ? AND fl.entity_type = 'approval'
                      AND a.status = 'pending'
                    """,
                    (flow_run_id,),
                ).fetchall()
            ]
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"""
                UPDATE approvals
                SET status = 'cancelled', resolved_by = ?, resolved_at = ?
                WHERE id IN ({placeholders}) AND status = 'pending'
                """,
                (resolved_by, now, *ids),
            )
        self.append_flow_event(
            flow_run_id, "approval.cancelled", resolved_by,
            entity_type="approval", to_state="cancelled",
            payload={"cancelled_ids": ids, "reason": "case_terminal"},
        )
        return len(ids)

    def expire_stale_approvals(self, *, limit: int = 500) -> int:
        """Flip past-``expires_at`` pending approvals to 'expired'. Returns the count.

        Enforces the approval TTL the schema always carried but nothing swept: an
        ignored proposal auto-resolves instead of blocking forever. Bounded (SQLite
        has no UPDATE ... LIMIT), NULL-expiry rows are never touched (open-ended by
        design). Idempotent; safe to call on any cadence."""
        now = _now()
        capped = max(1, min(int(limit or 500), 1000))
        with self._write() as conn:
            ids = [
                str(r["id"]) for r in conn.execute(
                    """
                    SELECT id FROM approvals
                    WHERE status = 'pending'
                      AND expires_at IS NOT NULL AND expires_at <= ?
                    ORDER BY expires_at ASC LIMIT ?
                    """,
                    (now, capped),
                ).fetchall()
            ]
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"""
                UPDATE approvals
                SET status = 'expired', resolved_by = 'system_expiry', resolved_at = ?
                WHERE id IN ({placeholders}) AND status = 'pending'
                """,
                (now, *ids),
            )
        return len(ids)

    def close_case(
        self,
        flow_run_id: str,
        *,
        outcome: str = "closed",
        actor: str = "operator",
        criteria_reconciliation: Optional[List[Dict[str, Any]]] = None,
        resolve_pending_approvals: bool = False,
        force: bool = False,
    ) -> bool:
        """[A37] Authoritatively close a Case — the ONLY status→terminal write path.

        Sets ``flow_runs.status`` to a terminal value and appends the matching
        ``flow.closed`` / ``flow.status_changed`` event, but ONLY when the Case can
        honestly close. Refuses (``CaseCloseBlocked``, a structured error — never a
        crash) while:
          * an approval linked to the Case is still pending;
          * a child flow of the Case is still open;
          * the Case's ``completion_criteria`` are not each recorded met or
            explicitly waived-with-reason in ``criteria_reconciliation``.

        Idempotent: closing an already-terminal Case returns False (no-op, no
        duplicate event). ``outcome`` must be a terminal status (see
        ``_CLOSED_STATUSES``). Returns True iff this call performed the close.
        Distinct from a task ending — ``Task finished != Case completed``; a task's
        terminal outcome NEVER reaches here."""
        if outcome not in self._CLOSED_STATUSES:
            raise ValueError(
                f"close outcome must be terminal {self._CLOSED_STATUSES}, got {outcome!r}"
            )
        row = self.get_flow_run(flow_run_id)
        if row is None:
            raise ValueError(f"unknown case: {flow_run_id}")
        if (row.get("status") or "") in self._CLOSED_STATUSES:
            return False  # already terminal — idempotent no-op

        open_children = [
            c for c in self.list_child_flow_runs(flow_run_id)
            if (c.get("status") or "") not in self._CLOSED_STATUSES
        ]
        if open_children:
            raise CaseCloseBlocked(
                f"case has {len(open_children)} open child flow(s)"
            )
        # An explicit operator close (or a force/orphan cleanup) supersedes a
        # dangling proposal: cancel the Case's pending approvals so the guard below
        # passes, instead of refusing on a decision nobody is going to make. The
        # guard still blocks a NON-operator (auto/Manager) close on a live approval.
        if resolve_pending_approvals or force:
            self.cancel_case_pending_approvals(flow_run_id, resolved_by=actor)
        if self._case_has_unresolved_approval(flow_run_id):
            raise CaseCloseBlocked("case has an unresolved required approval")

        # force = orphan cleanup (Manager session is terminal): the completion
        # criteria and any rework verdict are moot — there is no agent left to meet
        # or supersede them — so a forced close waives both. A normal close still
        # honours every gate.
        if not force:
            unresolved = _unreconciled_criteria(
                row.get("completion_criteria"), criteria_reconciliation,
            )
            if unresolved:
                raise CaseCloseBlocked(
                    f"completion_criteria not reconciled: {unresolved}"
                )

        # [M3.2] Unresolved-rework gate (flag-gated ⇒ OFF is byte-identical). The
        # LATEST review.* event is authoritative: if it is 'review.rework_requested'
        # it has NOT been superseded by a later accept/waive (else that later event
        # would be the latest), so the Case cannot honestly close.
        if not force and review_emitter_enabled():
            review_events = [
                e for e in self.list_flow_events(flow_run_id)
                if e.get("event_type") in _REVIEW_EVENT_TYPES
            ]
            if review_events and review_events[-1].get("event_type") == "review.rework_requested":
                raise CaseCloseBlocked(
                    "case has an unresolved rework request (latest review verdict is "
                    "rework_requested)"
                )

        self.update_flow_run(flow_run_id, status=outcome)
        self.append_flow_event(
            flow_run_id,
            "flow.closed" if outcome == "closed" else "flow.status_changed",
            actor, to_state=outcome,
            payload={
                "outcome": outcome,
                "reconciliation": criteria_reconciliation or None,
            },
        )
        return True

    def append_flow_event(
        self,
        flow_run_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Append one case lifecycle event. Append-only (never updated/deleted).

        Returns the new event id. `payload` is JSON-encoded verbatim and should
        stay a COMPACT reference + short reason — bulk evidence lives in
        artifacts/timelines/task results, not here.
        """
        payload_json = json.dumps(payload) if payload is not None else None
        with self._write() as conn:
            cur = conn.execute(
                """
                INSERT INTO flow_events (
                    flow_run_id, event_type, actor, from_state, to_state,
                    entity_type, entity_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (flow_run_id, event_type, actor, from_state, to_state,
                 entity_type, entity_id, payload_json, _now()),
            )
            return int(cur.lastrowid)

    def list_flow_events(
        self,
        flow_run_id: str,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """List a case's events in insertion order (the audit trail). Read-only."""
        rows = self._conn().execute(
            "SELECT * FROM flow_events WHERE flow_run_id = ? ORDER BY id ASC LIMIT ?",
            (flow_run_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def max_flow_event_ids(self, flow_run_ids: List[str]) -> Dict[str, int]:
        """The newest flow_event id per Case, in ONE batched query. Read-only.

        The Wake-Dispatcher uses this as an event-driven change signal: a Case's
        continuation state (``compute_continuation_tick``) is a pure function of
        its flow_events, so a Case whose MAX(id) has not advanced since the last
        evaluation cannot have changed — the dispatcher can skip the expensive
        per-Case 500-row read + recompute entirely. ``MAX(id)`` is served straight
        from ``idx_flow_events_flow(flow_run_id, id)`` (no row scan). Cases with no
        events are absent from the result (the caller treats that as 'compute')."""
        ids = [str(x) for x in flow_run_ids if x]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._conn().execute(
            f"""
            SELECT flow_run_id, MAX(id) AS max_id
            FROM flow_events
            WHERE flow_run_id IN ({placeholders})
            GROUP BY flow_run_id
            """,
            (*ids,),
        ).fetchall()
        return {str(r["flow_run_id"]): int(r["max_id"]) for r in rows}

    # ------------------------------------------------------------------
    # [A56 / M4] Spec authoring → scored review gate → decomposer-as-DAG.
    #
    # For a feature-sized intent a Manager authors a spec (durable evidence via an
    # ``artifact`` flow_link + ``spec.authored`` event), a SEPARATE plan-reviewer
    # seat scores it against R1 (``_score_spec_review``), and ONLY an accepted score
    # unlocks decomposition. Decomposition expands the approved objective into N
    # ``task_attached`` flow_links ON THE SAME CASE with dependency edges on
    # ``metadata_json`` — a task-DAG as DATA. It creates ZERO new ``flow_runs``: the
    # orphan-flow_run scatter is the exact failure mode M2.5 exists to prevent (§0.2
    # anti-goal). All four methods are flag-gated by ``spec_authoring_enabled()`` ⇒
    # OFF is byte-identical (no ``artifact.*``/``spec.*``/``case.decomposed`` events,
    # no ``task_attached`` links). This layer RECORDS + gates; nothing here is read to
    # DRIVE execution (the Manager dispatches from the DAG in order — A56 delivers the
    # DAG as data; the parallel executor is the out-of-scope A57 spike).
    # ------------------------------------------------------------------

    def publish_artifact(
        self,
        flow_run_id: str,
        artifact_id: str,
        *,
        kind: str = "artifact",
        title: Optional[str] = None,
        uri: Optional[str] = None,
        actor: str = "manager",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """[A56/M4] Publish a durable artifact onto a Case: an ``artifact`` flow_link
        + an ``artifact.published`` event (evidence, not a second ledger).

        Idempotent via the flow_links unique index (repeat call reuses the link).
        Flag-gated: with ``SPEC_AUTHORING_ENABLED`` OFF this writes NOTHING and
        returns ``{"ok": False, "reason": "spec_authoring_disabled"}``. Returns
        ``{"ok": True, "link_id", "event_id", "artifact_id"}`` on a publish.
        ``metadata`` is JSON-encoded verbatim (kind/title/uri are folded in for the
        link record); db.py does not interpret it.
        """
        if not spec_authoring_enabled():
            return {"ok": False, "reason": "spec_authoring_disabled"}
        link_meta: Dict[str, Any] = {"kind": kind}
        if title is not None:
            link_meta["title"] = title
        if uri is not None:
            link_meta["uri"] = uri
        if metadata:
            link_meta.update(metadata)
        link_id = self.create_flow_link(
            flow_run_id, "artifact", artifact_id, kind,
            created_by=actor, metadata=link_meta,
        )
        event_id = self.append_flow_event(
            flow_run_id, "artifact.published", actor,
            entity_type="artifact", entity_id=artifact_id,
            payload={"artifact_id": artifact_id, "kind": kind, "title": title, "uri": uri},
        )
        return {
            "ok": True, "link_id": link_id, "event_id": event_id,
            "artifact_id": artifact_id,
        }

    def publish_spec(
        self,
        flow_run_id: str,
        spec_id: str,
        spec_body: str,
        *,
        title: Optional[str] = None,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A56/M4] Author a spec ON a Case as durable evidence — a specialised
        :func:`publish_artifact` (kind='spec') plus a distinct ``spec.authored``
        event so the audit trail names the authoring step explicitly.

        The spec is stored as an ``artifact`` flow_link carrying the (bounded) body
        on ``metadata_json`` so the Case is self-describing from the DB alone. This
        writes the SPEC; it does NOT grade it (that is :func:`record_spec_review`, a
        separate seat) — the author never approves its own spec. Flag-gated: OFF ⇒
        ``{"ok": False, "reason": "spec_authoring_disabled"}``, nothing written.
        """
        if not spec_authoring_enabled():
            return {"ok": False, "reason": "spec_authoring_disabled"}
        result = self.publish_artifact(
            flow_run_id, spec_id, kind="spec", title=title, actor=actor,
            metadata={"body": spec_body},
        )
        # publish_artifact re-checked the flag; if it wrote, name the authoring step.
        if result.get("ok"):
            result["event_id"] = self.append_flow_event(
                flow_run_id, "spec.authored", actor,
                entity_type="artifact", entity_id=spec_id,
                payload={"spec_id": spec_id, "title": title},
            )
            result["spec_id"] = spec_id
            self.update_flow_stage(flow_run_id, "spec_authoring")
        return result

    def record_spec_review(
        self,
        flow_run_id: str,
        spec_id: str,
        scores: Dict[str, Any],
        *,
        reviewer: str = "reviewer",
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """[A56/M4] Score a spec against R1 by a SEPARATE plan-reviewer seat and
        record the verdict as the canonical ``review.*`` event (reusing M3.2 vocab)
        PLUS a ``spec.review_scored`` event carrying the full score-card.

        ``scores`` maps each ``SPEC_REVIEW_DIMENSIONS`` key to 0–2. The verdict is
        computed by :func:`_score_spec_review` (≥8/12 AND no critical-zero) — it is
        NOT taken on the caller's word, so a low or malformed card cannot self-report
        a pass. ``reviewer`` should differ from the spec's author (the SEAT is
        separate); the db records whoever the API/orchestrator seam passes. The
        resulting ``review.accepted`` / ``review.rework_requested`` event is what
        :func:`decompose_case` gates on. Flag-gated: OFF ⇒ nothing written,
        ``{"ok": False, "reason": "spec_authoring_disabled"}``.
        """
        if not spec_authoring_enabled():
            return {"ok": False, "reason": "spec_authoring_disabled"}
        graded = _score_spec_review(scores)
        review_event_type = REVIEW_VERDICT_EVENT_TYPES[graded["verdict"]]
        # The full score-card as durable evidence (its own event so the audit trail
        # carries the numbers, not just the pass/fail verdict).
        self.append_flow_event(
            flow_run_id, "spec.review_scored", reviewer,
            entity_type="artifact", entity_id=spec_id,
            payload={
                "spec_id": spec_id,
                "scores": {d: scores.get(d) for d in SPEC_REVIEW_DIMENSIONS},
                "total": graded["total"],
                "max": graded["max"],
                "threshold": graded["threshold"],
                "passed": graded["passed"],
                "critical_zero": graded["critical_zero"],
                "missing": graded["missing"],
                "out_of_range": graded["out_of_range"],
                "reason": reason,
            },
        )
        # The canonical M3.2 verdict event — the ONE the decompose-gate reads.
        review_event_id = self.append_flow_event(
            flow_run_id, review_event_type, reviewer,
            entity_type="artifact", entity_id=spec_id,
            payload={"verdict": graded["verdict"], "spec_id": spec_id, "reason": reason},
        )
        return {
            "ok": True,
            "spec_id": spec_id,
            "verdict": graded["verdict"],
            "passed": graded["passed"],
            "total": graded["total"],
            "max": graded["max"],
            "threshold": graded["threshold"],
            "critical_zero": graded["critical_zero"],
            "missing": graded["missing"],
            "out_of_range": graded["out_of_range"],
            "event_id": review_event_id,
            "event_type": review_event_type,
        }

    def latest_spec_review(
        self,
        flow_run_id: str,
        spec_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """[A56/M4] The most recent ``spec.review_scored`` verdict on a Case (for an
        optional specific ``spec_id``), or None if the spec was never scored.

        Read-only. Returns the parsed score-card payload (with ``passed``). This is
        the authoritative "is decomposition unlocked?" read — decompose_case uses it.
        """
        latest: Optional[Dict[str, Any]] = None
        for e in self.list_flow_events(flow_run_id):
            if e.get("event_type") != "spec.review_scored":
                continue
            if spec_id is not None and e.get("entity_id") != spec_id:
                continue
            latest = _event_payload(e) or {}
        return latest

    def decompose_case(
        self,
        flow_run_id: str,
        spec_id: str,
        tasks: List[Dict[str, Any]],
        *,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A56/M4] Expand an APPROVED objective into a task-DAG ON THIS ONE CASE.

        Each entry in ``tasks`` is ``{"task_key": str, "objective": str,
        "depends_on": [task_key, ...], ...(optional planning hints)}``. For each we
        create exactly ONE ``flow_link`` with ``entity_type='task'``,
        ``role='task_attached'``, ``entity_id=task_key`` (a PLAN node id, never a
        flow_run), and the dependency edges + objective on ``metadata_json``. It
        creates ZERO ``flow_runs`` — the DAG is N attached links on the SAME Case, the
        exact shape M2.5 exists to enforce (orphan flow_runs are the anti-goal).

        HARD GATES (each writes nothing and returns a structured refusal):
          * flag OFF ⇒ ``spec_authoring_disabled``;
          * the spec was never scored, or its latest score did NOT pass ⇒
            ``spec_not_approved`` (the scored-review gate is a REAL block, not a warning);
          * duplicate/empty ``task_key`` ⇒ ``invalid_task_keys``;
          * a ``depends_on`` edge to an unknown task_key ⇒ ``unknown_dependency``;
          * the dependency graph has a CYCLE ⇒ ``cyclic_dependencies`` (a DAG must be
            acyclic — a cycle is unschedulable).

        On success returns ``{"ok": True, "task_keys": [...], "order": [...],
        "link_ids": [...]}`` where ``order`` is one valid topological schedule and
        appends one ``case.decomposed`` event. Idempotent per (case, task_key, role)
        via the flow_links unique index.
        """
        if not spec_authoring_enabled():
            return {"ok": False, "reason": "spec_authoring_disabled"}

        review = self.latest_spec_review(flow_run_id, spec_id)
        if review is None or not review.get("passed"):
            return {
                "ok": False,
                "reason": "spec_not_approved",
                "review": review,
            }

        # ---- Validate the DAG as pure DATA before any write (all-or-nothing) ----
        keys: List[str] = []
        deps: Dict[str, List[str]] = {}
        for t in tasks:
            key = str((t or {}).get("task_key") or "").strip()
            if not key or key in deps:
                return {"ok": False, "reason": "invalid_task_keys", "task_key": key}
            keys.append(key)
            deps[key] = [str(d).strip() for d in (t.get("depends_on") or []) if str(d).strip()]
        keyset = set(keys)
        for key, edges in deps.items():
            for d in edges:
                if d not in keyset:
                    return {"ok": False, "reason": "unknown_dependency",
                            "task_key": key, "depends_on": d}
        order = _topological_order(keys, deps)
        if order is None:
            return {"ok": False, "reason": "cyclic_dependencies"}

        # ---- Write: N task_attached links + ONE decomposition event. Zero flow_runs.
        link_ids: List[Optional[int]] = []
        by_key = {str(t.get("task_key")).strip(): t for t in tasks}
        for key in keys:
            t = by_key[key]
            meta: Dict[str, Any] = {
                "objective": t.get("objective"),
                "depends_on": deps[key],
                "spec_id": spec_id,
            }
            # Carry any non-authoritative planning hints verbatim.
            for hint in ("estimated_hours", "priority", "human_task", "files"):
                if hint in t:
                    meta[hint] = t[hint]
            link_ids.append(self.create_flow_link(
                flow_run_id, "task", key, "task_attached",
                created_by=actor, metadata=meta,
            ))
        self.append_flow_event(
            flow_run_id, "case.decomposed", actor,
            entity_type="artifact", entity_id=spec_id,
            payload={"spec_id": spec_id, "task_count": len(keys), "order": order},
        )
        self.update_flow_stage(flow_run_id, "decomposed")
        return {
            "ok": True,
            "task_keys": keys,
            "order": order,
            "link_ids": link_ids,
        }

    def list_dag_tasks(self, flow_run_id: str) -> List[Dict[str, Any]]:
        """[A56/M4] The decomposed task-DAG of a Case: every ``task_attached`` link
        with its parsed dependency edges. Read-only — the Manager's "what do I
        dispatch, and in what order?" read. Returns ``[{task_key, objective,
        depends_on, metadata}, ...]`` in link-id order."""
        out: List[Dict[str, Any]] = []
        for link in self.list_flow_links(
            flow_run_id=flow_run_id, entity_type="task", role="task_attached",
        ):
            meta = {}
            raw = link.get("metadata_json")
            if raw:
                try:
                    meta = json.loads(raw)
                except (ValueError, TypeError):
                    meta = {}
            out.append({
                "task_key": link.get("entity_id"),
                "objective": meta.get("objective"),
                "depends_on": meta.get("depends_on") or [],
                "metadata": meta,
            })
        return out

    # ------------------------------------------------------------------
    # [A46 / M3.3] Durable worker-wait relay. A Manager's ``wait_for_worker``
    # is a pure in-process long-poll: its whole state lives in the mcp_manager
    # subprocess and is LOST if the Manager/gateway crashes mid-wait. The
    # completion SIGNAL is already durable (a worker turn records an
    # authoritative ``task.finished`` event); only the WAITER is not. These two
    # methods close that asymmetry by recording the wait intent as an
    # append-only ``worker.wait_pending`` marker at dispatch and letting a
    # resumed Manager reconcile its outstanding waits against ``task.finished``
    # — reusing the existing flow_events substrate, no new schema.
    # ------------------------------------------------------------------

    def record_worker_wait(
        self,
        flow_run_id: str,
        task_id: str,
        *,
        timeout: Optional[float] = None,
        actor: str = "manager",
    ) -> Optional[int]:
        """[A46] Record a durable pending-wait marker for a dispatched worker.

        Appends an append-only ``worker.wait_pending`` flow_event keyed to
        (flow_run_id, task_id) so a Manager that crashes/restarts mid-wait can
        RECONCILE which workers it was still waiting on from the ledger, not from
        lost in-process ``wait_for_worker`` memory.

        Flag-gated by ``durable_relay_enabled()`` (default OFF ⇒ returns None,
        writes nothing — byte-identical). Idempotent: if an unresolved pending
        marker already exists for this (case, task) it is NOT duplicated and the
        existing event id is returned. Returns the (new or existing) event id, or
        None when the flag is OFF.
        """
        if not durable_relay_enabled():
            return None
        # Idempotent: a pending marker is "live" only until a later resolve for
        # the same task clears it — so scan in order and keep the last relevant one.
        existing: Optional[Dict[str, Any]] = None
        for e in self.list_flow_events(flow_run_id):
            if e.get("entity_id") != task_id:
                continue
            if e.get("event_type") == "worker.wait_pending":
                existing = e
            elif e.get("event_type") == "worker.wait_resolved":
                existing = None
        if existing is not None:
            return int(existing["id"])
        return self.append_flow_event(
            flow_run_id, "worker.wait_pending", actor,
            entity_type="task", entity_id=task_id,
            payload={"task_id": task_id, "timeout": timeout},
        )

    def reconcile_worker_waits(
        self,
        flow_run_id: str,
        *,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A46] Reconcile a Case's outstanding worker waits after a restart.

        Reads the durable ledger and, for each ``worker.wait_pending`` marker not
        yet matched by a ``worker.wait_resolved``, checks whether the worker's turn
        has finished (a durable ``task.finished`` event for the same task):
          * finished  ⇒ append a ``worker.wait_resolved`` marker (RESOLVED) so the
            wait is cleared from the ledger;
          * still open ⇒ report it as PENDING (the Manager re-arms a fresh bounded
            ``wait_for_worker`` for it).

        Idempotent: a wait already resolved is skipped, so a crash DURING reconcile
        + a re-run is a no-op on already-resolved waits (no duplicate markers).
        Flag-gated by ``durable_relay_enabled()`` (OFF ⇒ ``{"ok": False,
        "reason": "durable_relay_disabled"}``, no write). Returns ``{"ok",
        "resolved": [{task_id, outcome}], "pending": [{task_id, timeout}]}``.
        """
        if not durable_relay_enabled():
            return {"ok": False, "reason": "durable_relay_disabled"}

        pending_markers: Dict[str, Dict[str, Any]] = {}
        resolved_tasks: set = set()
        finished: Dict[str, str] = {}
        for e in self.list_flow_events(flow_run_id):
            et = e.get("event_type")
            tid = e.get("entity_id")
            if not tid:
                continue
            if et == "worker.wait_pending":
                pending_markers[tid] = e
            elif et == "worker.wait_resolved":
                resolved_tasks.add(tid)
            elif et == "task.finished":
                finished[tid] = str(_event_outcome(e) or "success")

        resolved_out: List[Dict[str, Any]] = []
        pending_out: List[Dict[str, Any]] = []
        for tid, marker in pending_markers.items():
            if tid in resolved_tasks:
                continue  # already reconciled — idempotent skip
            if tid in finished:
                self.append_flow_event(
                    flow_run_id, "worker.wait_resolved", actor,
                    entity_type="task", entity_id=tid,
                    payload={"task_id": tid, "outcome": finished[tid]},
                )
                resolved_out.append({"task_id": tid, "outcome": finished[tid]})
            else:
                pl = _event_payload(marker)
                pending_out.append({
                    "task_id": tid,
                    "timeout": pl.get("timeout") if isinstance(pl, dict) else None,
                })
        return {"ok": True, "resolved": resolved_out, "pending": pending_out}

    # ------------------------------------------------------------------
    # [M3.4] Autonomous Case continuation. A Manager arms a wait-GROUP over a
    # dispatch set with a condition (ANY|ALL|named); when the group is satisfied
    # over the finished-but-unconsumed members, the orchestrator Wake-Dispatcher
    # schedules ONE deterministic mesh_tasks continuation row, atomically claims
    # it (single winner), delivers one coalesced proactive turn, and — on turn
    # return — the HARNESS records consumption into the row's ``result`` (the
    # watermark). Wait-group state is DERIVED from the append-only flow_events
    # ledger; the only enriched write is the group-scoped ``worker.wait_pending``
    # payload. No new table, no new columns. Flag-gated by
    # ``case_continuation_enabled()`` (default OFF ⇒ nothing is written).
    # ------------------------------------------------------------------

    def arm_wait_group(
        self,
        flow_run_id: str,
        wait_group_id: str,
        condition: str,
        member_task_ids: List[str],
        *,
        actor: str = "manager",
    ) -> Optional[int]:
        """[M3.4] Arm a Manager wait-group as a durable ``worker.wait_pending`` marker.

        ``condition`` ∈ {ANY, ALL, NAMED} (case-insensitive; anything else ⇒ ANY).
        The group is a single group-scoped ``worker.wait_pending`` flow_event
        (``entity_type='wait_group'``, ``entity_id=wait_group_id``) carrying
        ``{wait_group_id, condition, member_task_ids}`` — the enriched payload the
        Wake-Dispatcher derives group state from. Distinct from A46's per-task
        ``worker.wait_pending`` markers (those stay untouched).

        Idempotent per (case, wait_group_id): if an unresolved group marker already
        exists it is NOT duplicated and its event id is returned. Flag-gated by
        ``case_continuation_enabled()`` (OFF ⇒ returns None, writes nothing).
        """
        if not case_continuation_enabled():
            return None
        cond = str(condition or "ANY").upper()
        if cond not in ("ANY", "ALL", "NAMED"):
            cond = "ANY"
        # Idempotency: a group marker is "live" until a later resolve for the same
        # group clears it — scan in order and keep the last relevant one.
        existing: Optional[Dict[str, Any]] = None
        for e in self.list_flow_events(flow_run_id):
            if e.get("entity_type") != "wait_group" or e.get("entity_id") != wait_group_id:
                continue
            if e.get("event_type") == "worker.wait_pending":
                existing = e
            elif e.get("event_type") == "worker.wait_resolved":
                existing = None
        if existing is not None:
            event_id = int(existing["id"])
        else:
            event_id = self.append_flow_event(
                flow_run_id, "worker.wait_pending", actor,
                entity_type="wait_group", entity_id=wait_group_id,
                payload={
                    "wait_group_id": wait_group_id,
                    "condition": cond,
                    "member_task_ids": list(member_task_ids or []),
                },
            )
        try:
            session_id = self.case_manager_session_id(flow_run_id)
            if session_id:
                self.ensure_cache_heartbeat_owner(
                    session_id,
                    reason="case_wait_group",
                    owner_type="wait_group",
                    owner_id=f"{flow_run_id}:{wait_group_id}",
                    expected_runtime_sec=None,
                )
        except Exception as e:
            logger.debug(
                "event=cache_heartbeat_wait_group_owner_failed case=%s wait_group=%s err=%s",
                flow_run_id, wait_group_id, e,
            )
        return event_id

    def list_continuation_rows(self, case_id: str) -> List[Dict[str, Any]]:
        """[M3.4] The continuation ``mesh_tasks`` rows for a Case, oldest generation
        first. Keyed by the deterministic id prefix ``cont:{case}:`` and the
        reserved ``manager_continuation`` action. Read-only."""
        rows = self._conn().execute(
            "SELECT * FROM mesh_tasks WHERE action = ? AND id LIKE ? ORDER BY id ASC",
            (CONTINUATION_ACTION, f"cont:{case_id}:%"),
        ).fetchall()
        return [dict(r) for r in rows]

    def continuation_watermark(self, case_id: str) -> Tuple[set, int, int]:
        """[M3.4] The consumed watermark for a Case, from its continuation rows.

        Returns ``(consumed_task_ids, completed_rounds, highest_generation)``:
          * ``consumed_task_ids`` = ⋃ ``result.consumed_task_ids`` over all
            **completed** continuation rows — the set a next-satisfaction check
            subtracts. An in-flight (claimed, not completed) row contributes
            NOTHING, which is exactly why a crash redelivers rather than drops.
          * ``completed_rounds`` = number of completed continuation rows = the
            authoritative round count (the next generation is this + 1).
          * ``highest_generation`` = max generation present (any status).
        """
        consumed: set = set()
        completed = 0
        highest = 0
        for r in self.list_continuation_rows(case_id):
            try:
                gen = int(str(r.get("id", "")).rsplit(":", 1)[-1])
            except Exception:
                continue
            highest = max(highest, gen)
            if r.get("status") == "completed":
                completed += 1
                raw = r.get("result")
                if raw:
                    try:
                        res = json.loads(raw)
                        consumed |= set(res.get("consumed_task_ids") or [])
                    except Exception:
                        pass
        return consumed, completed, highest

    def compute_continuation_tick(self, flow_run_id: str) -> Dict[str, Any]:
        """[M3.4] Derive, purely from the ledger, whether a Case has a satisfied
        wait-group this tick and what a wake turn would present.

        Returns ``{satisfied, presented_task_ids, satisfied_groups, retire_only_groups,
        generation_next, completed_rounds, watermark}``. ``generation_next`` = completed_rounds + 1
        (NOT highest+1): an in-flight round keeps the same generation so a racing
        tick recomputes the SAME continuation id and the atomic claim dedupes it.
        Each satisfied group carries ``{wait_group_id, condition, presented,
        retire}`` — ``retire`` marks a one-shot (ALL/NAMED) group, or an ANY group
        whose every member is now finished, to be discharged on consumption.
        """
        groups: Dict[str, Dict[str, Any]] = {}
        resolved: set = set()
        finished: Dict[str, str] = {}
        # [continuation-review-watermark] task_ids the Manager has ALREADY adjudicated
        # via a review.* event TAGGED to that task (entity_type='task'). A tagged
        # review is a consumption signal on par with a continuation ACK: a finish the
        # Manager reviewed out-of-band (e.g. during an operator poke that interleaved
        # between the worker finishing and its wake) must NOT be re-surfaced as a
        # redundant "finished since your last turn" wake — that burned a whole paid
        # Manager turn to re-conclude "already done". Untagged (Case-level) reviews
        # carry no entity_id and are ignored here, so pre-tagging behaviour is
        # byte-identical: the optimisation only engages once a task_id is supplied.
        reviewed: set = set()
        for e in self.list_flow_events(flow_run_id):
            et = e.get("event_type")
            if e.get("entity_type") == "wait_group":
                gid = e.get("entity_id")
                if not gid:
                    continue
                if et == "worker.wait_pending":
                    pl = _event_payload(e) or {}
                    groups[gid] = {
                        "condition": str(pl.get("condition", "ANY")).upper(),
                        "members": list(pl.get("member_task_ids") or []),
                    }
                elif et == "worker.wait_resolved":
                    resolved.add(gid)
            elif et == "task.finished":
                tid = e.get("entity_id")
                if tid:
                    finished[tid] = _event_outcome(e) or "success"
            elif et in _REVIEW_EVENT_TYPES and e.get("entity_type") == "task":
                tid = e.get("entity_id")
                if tid:
                    reviewed.add(tid)

        consumed, completed, _highest = self.continuation_watermark(flow_run_id)
        presented: List[str] = []
        sat_groups: List[Dict[str, Any]] = []
        # One-shot groups whose finished members are ALL adjudicated but where at
        # least one was drained by an out-of-band review (not a continuation ACK):
        # they will never produce a wake, so they must be retired explicitly or they
        # dangle 'armed' forever and get needlessly re-armed on a Manager resume.
        retire_only: List[str] = []
        for gid, g in groups.items():
            if gid in resolved:
                continue
            members = g["members"]
            cond = g["condition"]
            if not members:
                continue
            # A member is drained if a continuation ACK recorded it OR the Manager
            # already reviewed it out-of-band — either way there is nothing new to
            # present for it.
            finished_unconsumed = [
                t for t in members
                if t in finished and t not in consumed and t not in reviewed
            ]
            all_finished = all(t in finished for t in members)
            if cond in ("ALL", "NAMED"):
                ok = all_finished and len(finished_unconsumed) > 0
                retire = all_finished  # one-shot: discharged as soon as consumed
            else:  # ANY — edge-triggered, repeating; retires only when drained
                ok = len(finished_unconsumed) > 0
                retire = all_finished
            if ok:
                sat_groups.append({
                    "wait_group_id": gid,
                    "condition": cond,
                    "presented": list(finished_unconsumed),
                    "retire": bool(retire),
                })
                for t in finished_unconsumed:
                    if t not in presented:
                        presented.append(t)
            elif (
                retire
                and not finished_unconsumed
                and any(t in reviewed and t not in consumed for t in members)
            ):
                # Fully finished, nothing left to present, and a review (not a
                # continuation) is what drained it → retire WITHOUT a paid wake.
                retire_only.append(gid)
        return {
            "satisfied": len(sat_groups) > 0,
            "presented_task_ids": presented,
            "satisfied_groups": sat_groups,
            "retire_only_groups": retire_only,
            "generation_next": completed + 1,
            "completed_rounds": completed,
            "watermark": sorted(consumed),
        }

    def record_continuation_consumed(
        self,
        flow_run_id: str,
        continuation_id: str,
        generation: int,
        consumed_task_ids: List[str],
        retired_group_ids: Optional[List[str]] = None,
        *,
        actor: str = "system",
    ) -> None:
        """[M3.4] The HARNESS transport ACK: mark a continuation row completed with
        its consumed watermark, and discharge any one-shot groups it drained.

        This is written by the orchestrator when the proactive wake turn returns —
        NOT by the LLM. Setting ``status='completed'`` + ``result={generation,
        consumed_task_ids}`` advances the watermark and counts round ``generation``.
        For each retired group, a semantic ``worker.wait_resolved`` marker is
        appended (``worker.wait_resolved`` is NEVER the transport ack — that is this
        row completion). Idempotent-safe: re-completing an already-completed row is
        a harmless overwrite of the same terminal state.
        """
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'completed', result = ?, completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps({
                            "generation": int(generation),
                            "consumed_task_ids": list(consumed_task_ids or []),
                        }),
                        now, now, continuation_id,
                    ),
                )
        except Exception as e:
            logger.warning(
                "event=db_continuation_consume_failed id=%s err=%s", continuation_id, e,
            )
        for gid in (retired_group_ids or []):
            self.append_flow_event(
                flow_run_id, "worker.wait_resolved", actor,
                entity_type="wait_group", entity_id=gid,
                payload={"wait_group_id": gid, "outcome": "drained"},
            )

    # ------------------------------------------------------------------ #
    # [A82 Stage 4c] Producer 3 — Case continuation token → managed turn
    # linkage and durable finalization (design §7, packet §8 item 3).
    # ------------------------------------------------------------------ #
    def token_to_turn(self, *, coalesce_key: str, session_id: str) -> str:
        """The ONE managed turn id a producer trigger maps to: the durably
        linked id when the token row ``coalesce_key`` (a continuation token id)
        is linked, else the deterministic id for its CURRENT durable attempt
        (1 when the token does not exist yet). A crash retry after the token
        claim rediscovers the same id (SYS03). Read-only."""
        key = (coalesce_key or "").strip()
        sid = (session_id or "").strip()
        row = self._conn().execute(
            "SELECT payload, producer_turn_id FROM mesh_tasks WHERE id = ? "
            "AND COALESCE(queue_protocol, 0) = 0",
            (key,),
        ).fetchone()
        if row is not None and row["producer_turn_id"]:
            return str(row["producer_turn_id"])
        return producer_turn_id(key, sid, _token_attempt(row["payload"] if row else None))

    def producer_token_attempt(self, token_id: str, session_id: str, payload: Any) -> int:
        """[A82 Stage 4c rework] The attempt to admit NEXT for an unlinked token:
        the durable payload counter, but never one whose turn (this session's
        automation scope, key ``<token>#<n>``) already ended — so a garbled or
        lost counter converges instead of replaying a terminal turn forever.
        One range read on the idempotency index."""
        from .turn_queue import TERMINAL_STATUSES

        attempt = _token_attempt(payload)
        rows = self._conn().execute(
            "SELECT idempotency_key, status FROM mesh_tasks "
            "WHERE queue_protocol = 1 AND idempotency_scope = ? "
            "AND idempotency_key >= ? AND idempotency_key < ?",
            (f"automation:{session_id}:continuation", f"{token_id}#", f"{token_id}$"),
        ).fetchall()
        for r in rows:
            if r["status"] not in TERMINAL_STATUSES:
                continue
            try:
                attempt = max(attempt, int(str(r["idempotency_key"]).rsplit("#", 1)[1]) + 1)
            except (IndexError, ValueError):
                continue
        return attempt

    def continuation_token_for_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        """The continuation token still LINKED (unfinalized) to ``turn_id``, with
        its payload decoded, or None. Served by the partial link index."""
        row = self._conn().execute(
            "SELECT * FROM mesh_tasks INDEXED BY idx_mesh_tasks_producer_link "
            "WHERE producer_turn_id = ? AND status = 'claimed'",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["payload"] = _token_payload(out.get("payload"))
        return out

    def reconcile_finalizers(self, limit: int = 25) -> List[Dict[str, Any]]:
        """Durable finalization of linked continuation tokens whose managed turn
        reached a terminal outcome — the restart-safe completion path (an
        in-memory finalizer is never the only one; design §7).

        Per token, ONE convergent procedure (a re-run after a crash between the
        steps converges):
          * consuming outcome (``PRODUCER_CONSUMING_STATUSES``): append the
            ``worker.wait_resolved`` markers of the retired groups ONCE, then CAS
            the token ``claimed``→``completed`` with the consumed watermark —
            the round is counted exactly once (the token row IS the round);
          * withdrawn / cancelled: CAS the token back to ``pending`` with its
            attempt bumped and the link cleared — no round, nothing consumed;
            the Wake-Dispatcher re-evaluates it (a fresh deterministic id).
        Bounded (``limit``), served by the partial link index. Returns the
        tokens finalized by THIS call (a lost CAS is not reported). Raises on a
        read error; a per-token write error is logged and retried next call."""
        from .turn_queue import TERMINAL_STATUSES

        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        rows = self._conn().execute(
            f"""
            SELECT t.id AS token_id, t.payload AS token_payload,
                   t.producer_turn_id AS turn_id, x.status AS turn_status
            FROM mesh_tasks t INDEXED BY idx_mesh_tasks_producer_link
            JOIN mesh_tasks x ON x.id = t.producer_turn_id
            WHERE t.producer_turn_id IS NOT NULL AND t.status = 'claimed'
              AND t.action = ? AND x.status IN ({placeholders})
            LIMIT ?
            """,
            (CONTINUATION_ACTION, *TERMINAL_STATUSES, int(limit)),
        ).fetchall()
        done: List[Dict[str, Any]] = []
        for r in rows:
            try:
                item = self._finalize_producer_token(
                    str(r["token_id"]), str(r["turn_id"]), str(r["turn_status"]),
                    _token_payload(r["token_payload"]),
                )
            except Exception as e:  # noqa: BLE001 — stays linked; next call re-runs
                logger.warning(
                    "event=producer_finalize_failed token=%s turn=%s err=%s",
                    r["token_id"], r["turn_id"], e,
                )
                continue
            if item is not None:
                done.append(item)
        return done

    def _finalize_producer_token(
        self, token_id: str, turn_id: str, turn_status: str, payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        case_id = str(payload.get("case_id") or "")
        generation = int(payload.get("generation") or 0)
        presented = [str(t) for t in (payload.get("presented_task_ids") or [])]
        now = _now()
        item = {
            "token_id": token_id, "turn_id": turn_id, "turn_status": turn_status,
            "case_id": case_id, "generation": generation,
            "presented_task_ids": presented,
        }
        if turn_status in PRODUCER_CONSUMING_STATUSES:
            # [A82 Stage 4c rework] No event after `flow.closed` (4a rule): a
            # closed (or unknown) Case gets the token consumed, nothing appended.
            case = self.get_flow_run(case_id) if case_id else None
            case_open = case is not None and (case.get("status") or "") not in self._CLOSED_STATUSES
            for gid in (payload.get("retired_group_ids") or []) if case_open else []:
                self.append_flow_event_once(
                    case_id, "worker.wait_resolved", "system",
                    entity_type="wait_group", entity_id=str(gid),
                    payload={"wait_group_id": str(gid), "outcome": "drained"},
                )
            result = json.dumps({
                "generation": generation, "consumed_task_ids": presented,
                "turn_id": turn_id, "turn_status": turn_status,
            })
            with self._managed_write("finalize_producer_token") as conn:
                conn.execute(
                    """
                    UPDATE mesh_tasks
                    SET status = 'completed', result = ?, completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'claimed' AND producer_turn_id = ?
                      AND COALESCE(queue_protocol, 0) = 0
                    """,
                    (result, now, now, token_id, turn_id),
                )
                won = conn.execute("SELECT changes()").fetchone()[0] > 0
            return dict(item, outcome="consumed") if won else None
        rearmed = dict(payload)
        rearmed.pop("turn_id", None)
        rearmed["attempt"] = _token_attempt(payload) + 1
        with self._managed_write("rearm_producer_token") as conn:
            conn.execute(
                """
                UPDATE mesh_tasks
                SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                    producer_turn_id = NULL, payload = ?, updated_at = ?
                WHERE id = ? AND status = 'claimed' AND producer_turn_id = ?
                  AND COALESCE(queue_protocol, 0) = 0
                """,
                (json.dumps(rearmed), now, token_id, turn_id),
            )
            won = conn.execute("SELECT changes()").fetchone()[0] > 0
        return dict(item, outcome="rearmed") if won else None

    def reconcile_heartbeat_finalizers(self, limit: int = 25) -> List[Dict[str, Any]]:
        """[A82 Stage 4d] Durable finalization of cache-heartbeat leases linked
        to a managed heartbeat turn that reached a terminal outcome — the
        restart-safe completion path (no in-memory finalizer; design §7).

        Per lease, ONE transaction (`_finalize_heartbeat_lease`): CAS the lease
        ``claimed``→``completed`` fenced to the linked turn and, only when THIS
        call won the CAS and the turn actually ran (completed / failed), apply
        the controller transition (beat counted, circuit / stop rules as
        legacy). Withdrawn (expired / obsolete / session close), cancelled
        (operator stop) and node-offline turns complete the lease with NO beat.
        A re-run converges (the CAS is lost ⇒ nothing is counted twice).
        Bounded, served by the partial link index. Returns the leases finalized
        by THIS call. Raises on a read error; a per-lease write error is logged
        and retried next call."""
        from .turn_queue import TERMINAL_STATUSES

        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        rows = self._conn().execute(
            f"""
            SELECT t.id AS token_id, t.payload AS token_payload,
                   t.producer_turn_id AS turn_id, x.status AS turn_status,
                   x.result AS turn_result, x.session_id AS session_id
            FROM mesh_tasks t INDEXED BY idx_mesh_tasks_producer_link
            JOIN mesh_tasks x ON x.id = t.producer_turn_id
            WHERE t.producer_turn_id IS NOT NULL AND t.status = 'claimed'
              AND t.action = ? AND x.status IN ({placeholders})
            LIMIT ?
            """,
            (CACHE_HEARTBEAT_ACTION, *TERMINAL_STATUSES, int(limit)),
        ).fetchall()
        done: List[Dict[str, Any]] = []
        for r in rows:
            try:
                item = self._finalize_heartbeat_lease(
                    str(r["token_id"]), str(r["turn_id"]), str(r["turn_status"]),
                    _token_payload(r["token_payload"]), _token_payload(r["turn_result"]),
                    str(r["session_id"] or ""),
                )
            except Exception as e:  # noqa: BLE001 — stays linked; next call re-runs
                logger.warning(
                    "event=heartbeat_finalize_failed lease=%s turn=%s err=%s",
                    r["token_id"], r["turn_id"], e,
                )
                continue
            if item is not None:
                done.append(item)
        return done

    def _finalize_heartbeat_lease(
        self, lease_id: str, turn_id: str, turn_status: str,
        payload: Dict[str, Any], result: Dict[str, Any], session_id: str,
    ) -> Optional[Dict[str, Any]]:
        heartbeat_id = str(payload.get("heartbeat_id") or "")
        beat = turn_status in ("completed", "failed")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cache_read") or 0)
        cache_creation = int(
            usage.get("cache_creation_input_tokens") or usage.get("cache_creation") or 0
        )
        if beat and cache_read <= 0 and cache_creation <= 0 and session_id:
            evidence = self.cache_evidence_for_task(session_id, turn_id)
            if evidence:
                cache_read = int(evidence.get("cache_read_tokens") or 0)
                cache_creation = int(evidence.get("cache_creation_tokens") or 0)
        success = turn_status == "completed" and bool(result.get("success", True))
        lease_result = json.dumps({
            "heartbeat_id": heartbeat_id, "wake_task_id": turn_id,
            "turn_id": turn_id, "turn_status": turn_status, "beat": beat,
            "success": success, "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
        })
        now = _now()
        with self._managed_write("finalize_heartbeat_lease") as conn:
            conn.execute(
                """
                UPDATE mesh_tasks
                SET status = 'completed', result = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'claimed' AND producer_turn_id = ?
                  AND action = ? AND COALESCE(queue_protocol, 0) = 0
                """,
                (lease_result, now, now, lease_id, turn_id, CACHE_HEARTBEAT_ACTION),
            )
            if conn.execute("SELECT changes()").fetchone()[0] == 0:
                return None
            hb = conn.execute(
                "SELECT * FROM session_cache_heartbeats WHERE id = ?", (heartbeat_id,),
            ).fetchone() if beat and heartbeat_id else None
            if hb is not None:
                self._apply_cache_heartbeat_result(
                    conn, dict(hb), turn_id, success=success,
                    output=str(result.get("output") or ""),
                    cache_read_tokens=cache_read, cache_creation_tokens=cache_creation,
                    error_class=str(result.get("error_class") or ""),
                )
        return {
            "lease_id": lease_id, "turn_id": turn_id, "turn_status": turn_status,
            "heartbeat_id": heartbeat_id, "beat": hb is not None,
        }

    def list_open_cases(self, limit: int = 200) -> List[Dict[str, Any]]:
        """[M3.4] Open (non-terminal) Cases — the Wake-Dispatcher's per-tick scan set.

        A Case is open while its ``status`` is NULL/'' or any non-terminal value
        (terminal = ``_CLOSED_STATUSES``). Read-only; newest first."""
        placeholders = ",".join("?" * len(self._CLOSED_STATUSES))
        rows = self._conn().execute(
            f"""
            SELECT * FROM flow_runs
            WHERE COALESCE(status, '') NOT IN ({placeholders})
            ORDER BY created_at DESC LIMIT ?
            """,
            (*self._CLOSED_STATUSES, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def case_manager_session_id(self, flow_run_id: str) -> Optional[str]:
        """[M3.4] The session bound to a Case as its Manager (the wake target), or
        None. Reads the authoritative ``flow_links`` (entity_type='session',
        role='manager'); the most recent such link wins."""
        links = self.list_flow_links(
            flow_run_id=flow_run_id, entity_type="session", role="manager",
        )
        if not links:
            return None
        return str(links[-1].get("entity_id") or "") or None

    def case_quota_pause(self, flow_run_id: str) -> Optional[Dict[str, Any]]:
        """[quota-resume] The Case's OPEN quota pause, or None.

        A pause opens with ``flow.quota_paused`` (written when a Manager turn on
        this Case died on ``usage_limit``) and closes with the next
        ``flow.quota_resumed`` / ``flow.quota_pause_declined`` /
        ``case.manager_respawned`` — a Case that already got a Manager back, or
        whose resume the operator declined, is no longer waiting on quota. Reading
        it back off the append-only ledger (rather than a new status column)
        keeps the pause on the same substrate as every other Case fact and makes
        it survive a gateway restart for free.

        ONE bounded query over the three relevant event types, newest first —
        deliberately not ``list_flow_events`` (which is ``ORDER BY id ASC LIMIT
        500``, i.e. the OLDEST 500 events: on a long-running Case the pause we
        need is exactly what that window drops). Returns the payload of the
        newest still-open pause, enriched with ``paused_at``/``event_id`` so
        callers can build a deterministic resume id.
        """
        rows = self._conn().execute(
            """
            SELECT * FROM flow_events
            WHERE flow_run_id = ?
              AND event_type IN ('flow.quota_paused', 'flow.quota_resumed',
                                 'flow.quota_pause_declined', 'case.manager_respawned')
            ORDER BY id DESC LIMIT 1
            """,
            (flow_run_id,),
        ).fetchall()
        if not rows:
            return None
        newest = dict(rows[0])
        if str(newest.get("event_type") or "") != "flow.quota_paused":
            return None
        return {
            **(_event_payload(newest) or {}),
            "paused_at": newest.get("created_at"),
            "event_id": newest.get("id"),
        }

    def transient_pause(self, flow_run_id: str) -> Optional[Dict[str, Any]]:
        """[transient-resume] The Case's OPEN transient-provider pause, or None.

        Mirrors :meth:`case_quota_pause` exactly (same append-only substrate, same
        newest-first bounded query, survives a gateway restart for free). A pause
        opens with ``flow.transient_paused`` (a Manager turn on this Case died on a
        terminal transient 5xx) and closes with the next ``flow.transient_resumed``
        (the retry was delivered), ``flow.transient_pause_exhausted`` (the bounded
        retry budget ran out) or ``case.manager_respawned`` (the Case already got a
        Manager back a different way). Returns the newest still-open pause's
        payload, enriched with ``paused_at``/``event_id``.
        """
        rows = self._conn().execute(
            """
            SELECT * FROM flow_events
            WHERE flow_run_id = ?
              AND event_type IN ('flow.transient_paused', 'flow.transient_resumed',
                                 'flow.transient_pause_exhausted', 'case.manager_respawned')
            ORDER BY id DESC LIMIT 1
            """,
            (flow_run_id,),
        ).fetchall()
        if not rows:
            return None
        newest = dict(rows[0])
        if str(newest.get("event_type") or "") != "flow.transient_paused":
            return None
        return {
            **(_event_payload(newest) or {}),
            "paused_at": newest.get("created_at"),
            "event_id": newest.get("id"),
        }

    def recent_transient_pause_count(self, flow_run_id: str, since_iso: str) -> int:
        """[transient-resume] How many ``flow.transient_paused`` events this Case
        has recorded at or after ``since_iso`` (a UTC ISO-8601 string).

        Used to derive the current attempt number over a rolling window: a burst
        of transient failures escalates the backoff, but a lone 529 hours later
        starts fresh because the old pauses fall outside the window — so the bound
        self-resets without needing a 'the retry finally succeeded' hook.
        ISO-8601 UTC strings compare correctly lexicographically, matching how
        ``created_at`` is stored and how ``case_quota_pause`` treats it.
        """
        row = self._conn().execute(
            """
            SELECT COUNT(*) AS n FROM flow_events
            WHERE flow_run_id = ?
              AND event_type = 'flow.transient_paused'
              AND created_at >= ?
            """,
            (flow_run_id, since_iso),
        ).fetchone()
        return int((dict(row) if row else {}).get("n") or 0)

    def case_round_cap(self, flow_run_id: str) -> int:
        """[M3.4] The continuation round cap for a Case. Carried in
        ``completion_criteria`` as JSON ``{"round_cap": N}`` when present; otherwise
        ``DEFAULT_CONTINUATION_ROUND_CAP``. A cap is a sibling termination criterion,
        NOT a new column."""
        row = self.get_flow_run(flow_run_id)
        raw = (row or {}).get("completion_criteria")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                parsed = json.loads(raw)
                cap = parsed.get("round_cap")
                if isinstance(cap, int) and cap > 0:
                    return cap
            except Exception:
                pass
        return DEFAULT_CONTINUATION_ROUND_CAP

    # ------------------------------------------------------------------
    # [A54 / M3.4 Job 2] Durable Case reconstruction. A Manager that lost its
    # in-process context (compaction, restart, respawn) must be able to pick a
    # Case back up knowing everything that matters from the DB ALONE, in ONE
    # bounded read, and re-establish its outstanding waits/groups. get_case_brief
    # is that single read; boot_reconcile_case is the idempotent re-arm hook the
    # role-boot path fires. Both are pure reads over the existing substrate plus
    # the already-idempotent A46/M3.4 writers — no new table, no new column.
    # ------------------------------------------------------------------

    def get_case_brief(self, case_id: str) -> Optional[Dict[str, Any]]:
        """[A54] The full working state of a Case, reconstructed from the DB ALONE.

        A single BOUNDED read set (CLAUDE.md §8 — no N+1 per worker): the Case row
        (:func:`get_flow_run`), its links (:func:`list_flow_links` — one JOIN'd
        query), its events (:func:`list_flow_events` — one query), the continuation
        watermark and the derived wait-group satisfaction (:func:`compute_continuation_tick`,
        itself a bounded pass over the same events + continuation rows). Every worker
        field is bucketed from those already-fetched lists in memory — NOT re-queried
        per worker.

        Returns ``None`` for an unknown Case. The shape:
          * ``case_id`` / ``objective`` / ``status`` / ``current_stage``
          * ``completion_criteria`` — the human criteria list (dual-shape unpacked)
          * ``round_cap`` / ``rounds_used`` / ``rounds_remaining`` — the M3.4
            continuation backstop and how much of it is spent
          * ``workers`` — one entry per DISPATCHED worker (flow_link entity_type='task',
            role='task', created_by='manager'): ``{task_id, session_id, finished,
            outcome, latest_review}`` (session from the worker session link if any;
            ``latest_review`` is the newest review.* verdict TAGGED to this task, else
            None — reviews are Case-level today so most tasks carry None here)
          * ``latest_review`` — the newest Case-level review.* verdict (verdict + reason
            + event_type), or None
          * ``open_waits`` / ``ready_waits`` — per-task A46 waits still pending vs.
            finished-but-unresolved (from the wait markers + task.finished)
          * ``wait_groups`` — every ARMED (unresolved) wait-group and its live
            satisfaction state derived by ``compute_continuation_tick``:
            ``{wait_group_id, condition, members, satisfied, presented_task_ids, retire}``

        Read-only; introduces no new table/column. Never raises on a well-formed id —
        an unknown Case is ``None``.
        """
        row = self.get_flow_run(case_id)
        if row is None:
            return None

        # ---- ONE bounded read of each substrate list (no per-worker fanout) ----
        links = self.list_flow_links(flow_run_id=case_id)
        events = self.list_flow_events(case_id)

        # Index events ONCE into the buckets the brief needs.
        finished: Dict[str, str] = {}          # task_id -> outcome
        wait_pending_tasks: set = set()        # A46 per-task pending markers
        wait_resolved_tasks: set = set()       # A46 per-task resolved markers
        review_by_task: Dict[str, Dict[str, Any]] = {}   # task_id -> latest review.* (tagged)
        latest_case_review: Optional[Dict[str, Any]] = None
        for e in events:
            et = e.get("event_type")
            etype = e.get("entity_type")
            eid = e.get("entity_id")
            if et == "task.finished" and eid:
                finished[eid] = _event_outcome(e) or "success"
            elif et == "worker.wait_pending" and etype == "task" and eid:
                wait_pending_tasks.add(eid)
            elif et == "worker.wait_resolved" and etype == "task" and eid:
                wait_resolved_tasks.add(eid)
            elif et in _REVIEW_EVENT_TYPES:
                pl = _event_payload(e) or {}
                verdict_rec = {
                    "verdict": pl.get("verdict"),
                    "reason": pl.get("reason"),
                    "event_type": et,
                }
                # Newest wins (events are id-ascending, so overwrite as we go).
                latest_case_review = verdict_rec
                if eid:  # a review TAGGED to a specific worker task (forward-compat)
                    review_by_task[eid] = verdict_rec

        # ---- Dispatched workers (entity_type='task', role='task', by='manager') ----
        # Worker sessions ride the entity_type='session', role='worker' links; index
        # them ONCE by nothing (there is no task↔session key on the link), so we expose
        # the set separately rather than guess a mapping.
        worker_session_ids: List[str] = [
            str(l.get("entity_id"))
            for l in links
            if l.get("entity_type") == "session" and l.get("role") == "worker" and l.get("entity_id")
        ]
        workers: List[Dict[str, Any]] = []
        for l in links:
            if l.get("entity_type") != "task" or l.get("role") != "task":
                continue
            if str(l.get("created_by") or "") != "manager":
                continue
            tid = str(l.get("entity_id") or "")
            if not tid:
                continue
            workers.append({
                "task_id": tid,
                "finished": tid in finished,
                "outcome": finished.get(tid),
                "latest_review": review_by_task.get(tid),
            })

        # ---- A46 per-task waits: open (pending, not resolved, not finished) vs ready
        # (finished but the wait not yet resolved — the Manager should reconcile it).
        open_waits: List[str] = []
        ready_waits: List[str] = []
        for tid in wait_pending_tasks:
            if tid in wait_resolved_tasks:
                continue
            if tid in finished:
                ready_waits.append(tid)
            else:
                open_waits.append(tid)

        # ---- Armed wait-groups + live satisfaction (reuse the M3.4 derivation) ----
        tick = self.compute_continuation_tick(case_id)
        sat_by_gid = {g["wait_group_id"]: g for g in tick.get("satisfied_groups", [])}
        # Enumerate the ARMED (unresolved) groups from the same event pass.
        armed_groups: Dict[str, Dict[str, Any]] = {}
        resolved_groups: set = set()
        for e in events:
            if e.get("entity_type") != "wait_group":
                continue
            gid = e.get("entity_id")
            if not gid:
                continue
            if e.get("event_type") == "worker.wait_pending":
                pl = _event_payload(e) or {}
                armed_groups[gid] = {
                    "wait_group_id": gid,
                    "condition": str(pl.get("condition", "ANY")).upper(),
                    "members": list(pl.get("member_task_ids") or []),
                }
            elif e.get("event_type") == "worker.wait_resolved":
                resolved_groups.add(gid)
        wait_groups: List[Dict[str, Any]] = []
        for gid, g in armed_groups.items():
            if gid in resolved_groups:
                continue  # discharged — not a live obligation
            sat = sat_by_gid.get(gid)
            wait_groups.append({
                **g,
                "satisfied": sat is not None,
                "presented_task_ids": list(sat.get("presented", [])) if sat else [],
                "retire": bool(sat.get("retire")) if sat else False,
            })

        round_cap = self.case_round_cap(case_id)
        rounds_used = int(tick.get("completed_rounds", 0))
        return {
            "case_id": case_id,
            "objective": row.get("objective_lock") or row.get("objective"),
            "status": row.get("status"),
            "current_stage": row.get("current_stage"),
            "completion_criteria": _parse_completion_criteria(row.get("completion_criteria")),
            "round_cap": round_cap,
            "rounds_used": rounds_used,
            "rounds_remaining": max(0, round_cap - rounds_used),
            "workers": workers,
            "worker_session_ids": worker_session_ids,
            "latest_review": latest_case_review,
            "open_waits": sorted(open_waits),
            "ready_waits": sorted(ready_waits),
            "wait_groups": wait_groups,
        }

    def boot_reconcile_case(
        self,
        case_id: str,
        *,
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """[A54] Boot-time reconstruction hook: a Manager resuming onto an existing
        OPEN Case reconciles its outstanding worker waits AND re-arms its live
        wait-groups from the ledger — so it wakes with its FULL obligation set,
        not the empty in-process state a fresh boot starts with.

        IDEMPOTENT by construction (running it twice writes NO duplicate markers):
          * ``reconcile_worker_waits`` (A46) skips already-resolved waits;
          * re-arming replays each still-armed group through ``arm_wait_group``,
            which is idempotent per (case, wait_group_id) — an existing unresolved
            group marker is returned, never duplicated.

        Flag-gated: no-ops (``{"ok": False, "reason": ...}``) when the durable relay
        is OFF (reconcile has nothing to do) — group re-arm additionally needs
        continuation ON, but ``arm_wait_group`` self-gates so a flags-mixed state is
        safe. Returns ``{"ok", "reconciled": {...}, "rearmed": [wait_group_id, ...]}``.
        """
        if not durable_relay_enabled():
            # Nothing durable to reconcile ⇒ byte-identical no-op.
            return {"ok": False, "reason": "durable_relay_disabled"}

        reconciled = self.reconcile_worker_waits(case_id, actor=actor)

        # Re-arm every STILL-ARMED (unresolved) wait-group from the ledger. Replaying
        # arm_wait_group with the SAME (gid, condition, members) is idempotent — the
        # existing unresolved marker is returned, so a double-boot writes nothing new.
        armed: Dict[str, Dict[str, Any]] = {}
        resolved: set = set()
        for e in self.list_flow_events(case_id):
            if e.get("entity_type") != "wait_group":
                continue
            gid = e.get("entity_id")
            if not gid:
                continue
            if e.get("event_type") == "worker.wait_pending":
                pl = _event_payload(e) or {}
                armed[gid] = {
                    "condition": str(pl.get("condition", "ANY")).upper(),
                    "members": list(pl.get("member_task_ids") or []),
                }
            elif e.get("event_type") == "worker.wait_resolved":
                resolved.add(gid)
        rearmed: List[str] = []
        for gid, g in armed.items():
            if gid in resolved:
                continue
            self.arm_wait_group(
                case_id, gid, g["condition"], g["members"], actor=actor,
            )
            rearmed.append(gid)
        return {"ok": True, "reconciled": reconciled, "rearmed": rearmed}

    # ------------------------------------------------------------------
    # Approvals (Move H) — durable approval gate. A pending approval is a
    # promise of a NOT-yet-dispatched action; resolving it is what triggers
    # dispatch. Persisting it here is what lets it survive a gateway restart
    # (an in-memory asyncio.Event would not) and rebuild the pending queue.
    # ------------------------------------------------------------------

    def create_approval(
        self,
        approval_id: str,
        action: str,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        risk: str = "medium",
        reversible: bool = True,
        requested_by: str = "",
        payload: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
    ) -> None:
        """Insert a new pending approval."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO approvals (
                        id, session_id, task_id, action, risk, reversible,
                        status, requested_by, payload, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        approval_id, session_id, task_id, action, risk,
                        1 if reversible else 0, requested_by,
                        json.dumps(payload) if payload is not None else None,
                        now, expires_at,
                    ),
                )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: approvals.id" in str(e):
                logger.debug("event=db_create_approval_duplicate id=%s", approval_id)
            else:
                logger.warning("event=db_create_approval_integrity_failed id=%s err=%s", approval_id, e)
        except Exception as e:
            logger.warning("event=db_create_approval_failed id=%s err=%s", approval_id, e)

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_approvals(
        self,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT * FROM approvals {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_approval(
        self, approval_id: str, status: str, resolved_by: str = ""
    ) -> bool:
        """Guarded transition: only a PENDING approval moves to a terminal status.

        Returns True iff exactly this call performed the transition. The
        ``status = 'pending'`` guard in the WHERE makes the resolve atomic — a
        concurrent double-resolve (two surfaces racing) results in exactly one
        True; the loser sees False (caller maps to already_resolved). This is the
        same optimistic-claim pattern as ``claim_task``.
        """
        if status not in ("approved", "rejected", "expired"):
            return False
        now = _now()
        try:
            with self._write() as conn:
                cur = conn.execute(
                    """
                    UPDATE approvals
                       SET status = ?, resolved_by = ?, resolved_at = ?
                     WHERE id = ? AND status = 'pending'
                    """,
                    (status, resolved_by, now, approval_id),
                )
                return cur.rowcount == 1
        except Exception as e:
            logger.warning("event=db_resolve_approval_failed id=%s err=%s", approval_id, e)
            return False

    # ------------------------------------------------------------------
    # Task events
    # ------------------------------------------------------------------

    def append_event(
        self,
        session_id: str,
        task_id: str,
        success: bool,
        execution_time: Optional[float] = None,
        error: str = "",
    ) -> None:
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO task_events
                        (session_id, task_id, timestamp, success, execution_time, error)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, task_id, _now(), int(success), execution_time, error),
                )
        except Exception as e:
            logger.warning("event=db_append_event_failed session_id=%s err=%s", session_id, e)

    def get_events(
        self,
        session_id: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM task_events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def upsert_node(
        self,
        node_id: str,
        tailscale_ip: str,
        api_port: int,
        backends: List[str],
        max_concurrent: int,
        status: str = "online",
        projects_root: str = "",
        repos: Optional[List[dict]] = None,
        models: Optional[Dict[str, List[dict]]] = None,
        incarnation_id: Optional[str] = None,
        managed_backends: Optional[List[str]] = None,
    ) -> str:
        """Upsert a node record and return the new incarnation_id.

        New workers provide a process incarnation_id that remains stable across
        controller restarts and re-registration. Older workers omit it, so we
        mint a fresh UUID and preserve the previous restart-detection behavior.
        list_stale_claims uses the mismatch between claimer_incarnation and the
        node's current incarnation_id to detect claims orphaned by a restart.
        """
        now = _now()
        incarnation_id = incarnation_id or uuid.uuid4().hex
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO nodes
                        (node_id, tailscale_ip, api_port, backends, max_concurrent,
                         status, last_heartbeat, registered_at, updated_at,
                         projects_root, repos, model_capabilities, incarnation_id,
                         managed_backends)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        tailscale_ip   = excluded.tailscale_ip,
                        api_port       = excluded.api_port,
                        backends       = excluded.backends,
                        max_concurrent = excluded.max_concurrent,
                        status         = excluded.status,
                        last_heartbeat = excluded.last_heartbeat,
                        updated_at     = excluded.updated_at,
                        projects_root  = excluded.projects_root,
                        repos          = excluded.repos,
                        model_capabilities = excluded.model_capabilities,
                        incarnation_id = excluded.incarnation_id,
                        managed_backends = excluded.managed_backends
                    """,
                    (
                        node_id,
                        tailscale_ip,
                        api_port,
                        json.dumps(backends),
                        max_concurrent,
                        status,
                        now,
                        now,
                        now,
                        projects_root,
                        json.dumps(repos or []),
                        json.dumps(models or {}),
                        incarnation_id,
                        json.dumps(list(managed_backends or [])),
                    ),
                )
        except Exception as e:
            logger.warning("event=db_upsert_node_failed node_id=%s err=%s", node_id, e)
        return incarnation_id

    def heartbeat_node(
        self,
        node_id: str,
        live_state: Optional[str] = None,
        model_capabilities: Optional[str] = None,
    ) -> None:
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE nodes
                    SET last_heartbeat = ?, status = 'online', updated_at = ?,
                        live_state = COALESCE(?, live_state),
                        live_state_updated_at = CASE WHEN ? IS NOT NULL THEN ? ELSE live_state_updated_at END,
                        model_capabilities = COALESCE(?, model_capabilities)
                    WHERE node_id = ?
                    """,
                    (now, now, live_state, live_state, now, model_capabilities, node_id),
                )
        except Exception as e:
            logger.warning("event=db_heartbeat_node_failed node_id=%s err=%s", node_id, e)

    def mark_node_offline(self, node_id: str) -> None:
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE nodes SET status = 'offline', updated_at = ? WHERE node_id = ?",
                    (now, node_id),
                )
        except Exception as e:
            logger.warning("event=db_mark_node_offline_failed node_id=%s err=%s", node_id, e)

    def list_nodes(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self._conn().execute(
                "SELECT * FROM nodes WHERE status = ? ORDER BY node_id", (status,)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM nodes ORDER BY node_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Watched jobs
    # ------------------------------------------------------------------

    def register_job(
        self,
        job_id: str,
        node_id: str,
        label: str,
        session_id: Optional[str] = None,
        command: Optional[str] = None,
        cwd: Optional[str] = None,
        log_path: Optional[str] = None,
        notify: bool = True,
        notify_agent: bool = False,
    ) -> None:
        """Insert a new running job row."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs
                        (id, session_id, node_id, label, command, cwd, status,
                         started_at, started_epoch, log_path, notify, notify_agent,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id, session_id, node_id, label, command, cwd,
                        now, time.time(), log_path,
                        1 if notify else 0, 1 if notify_agent else 0,
                        now, now,
                    ),
                )
        except sqlite3.IntegrityError:
            logger.debug("event=db_register_job_duplicate job_id=%s", job_id)
        except Exception as e:
            logger.warning("event=db_register_job_failed job_id=%s err=%s", job_id, e)

    def start_job(
        self,
        job_id: str,
        pid: int,
        pgid: int,
        log_path: Optional[str] = None,
        started_epoch: Optional[float] = None,
        observed_command: Optional[str] = None,
    ) -> None:
        """Record PID/PGID for a spawned job."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET pid = ?, pgid = ?, log_path = COALESCE(?, log_path),
                        started_epoch = COALESCE(?, started_epoch),
                        last_checked_at = ?,
                        last_probe_error = '',
                        last_seen_command = COALESCE(?, last_seen_command),
                        last_seen_started_epoch = COALESCE(?, last_seen_started_epoch),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        pid, pgid, log_path,
                        started_epoch, now,
                        observed_command, started_epoch,
                        now, job_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_start_job_failed job_id=%s err=%s", job_id, e)

    def record_job_probe(
        self,
        job_id: str,
        *,
        checked_at: Optional[str] = None,
        observed_command: Optional[str] = None,
        observed_started_epoch: Optional[float] = None,
        probe_error: str = "",
    ) -> None:
        """Persist the worker's latest process-identity probe for a running job."""
        now = checked_at or _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET last_checked_at = ?,
                        last_probe_error = ?,
                        last_seen_command = COALESCE(?, last_seen_command),
                        last_seen_started_epoch = COALESCE(?, last_seen_started_epoch),
                        updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (
                        now,
                        probe_error,
                        observed_command,
                        observed_started_epoch,
                        now,
                        job_id,
                    ),
                )
        except Exception as e:
            logger.warning("event=db_record_job_probe_failed job_id=%s err=%s", job_id, e)

    def complete_job(
        self,
        job_id: str,
        exit_code: int,
        tail: str = "",
    ) -> None:
        """Mark a running job as done."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'done', exit_code = ?, tail = ?,
                        finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (exit_code, tail, now, now, job_id),
                )
        except Exception as e:
            logger.warning("event=db_complete_job_failed job_id=%s err=%s", job_id, e)

    def fail_job(
        self,
        job_id: str,
        error: str,
        status: str = "failed",
    ) -> None:
        """Mark a running job as failed or lost."""
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, tail = ?, finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (status, error, now, now, job_id),
                )
        except Exception as e:
            logger.warning("event=db_fail_job_failed job_id=%s err=%s", job_id, e)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_jobs(
        self,
        node_id: Optional[str] = None,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        ownership: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if node_id:
            clauses.append("node_id = ?")
            params.append(node_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        elif ownership == "unowned":
            # "Unowned" from the UI's perspective = not attached to a session the UI
            # can actually show. That is a genuinely NULL session_id OR an ORPHANED
            # one: set, but matching no known session (e.g. a job registered with a
            # backend/native UUID instead of the gateway session id). Both would
            # otherwise be invisible in EVERY session view and the old null-only
            # System panel — so a registered job could silently vanish. Surface both.
            clauses.append(
                "(jobs.session_id IS NULL OR NOT EXISTS "
                "(SELECT 1 FROM sessions WHERE sessions.session_id = jobs.session_id))"
            )
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        # Authoritatively flag orphaned jobs (session_id set but no matching session)
        # so the UI can render them honestly — with an "orphaned" marker instead of a
        # dead session link — no matter which view surfaces them. Correlated subquery,
        # no extra round-trip; the flag is additive so other callers ignore it.
        rows = self._conn().execute(
            f"""
            SELECT jobs.*,
                   CASE
                     WHEN jobs.session_id IS NOT NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM sessions
                            WHERE sessions.session_id = jobs.session_id
                          )
                     THEN 1 ELSE 0
                   END AS orphaned
            FROM jobs {where} ORDER BY created_at DESC LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_jobs_for_sessions(
        self, session_ids: List[str], limit: int = 200
    ) -> List[Dict[str, Any]]:
        """[Cockpit] All jobs owned by ANY of `session_ids` in one query (no N+1).

        Used by the Case roster: a Case's sessions (manager + workers) are the join
        key, since a `watch_job` job carries the SESSION_ID of the process that
        registered it. Carries the same `orphaned` flag as list_jobs so a job whose
        session vanished is still rendered honestly."""
        if not session_ids:
            return []
        placeholders = ",".join("?" for _ in session_ids)
        rows = self._conn().execute(
            f"""
            SELECT jobs.*,
                   CASE
                     WHEN jobs.session_id IS NOT NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM sessions
                            WHERE sessions.session_id = jobs.session_id
                          )
                     THEN 1 ELSE 0
                   END AS orphaned
            FROM jobs
            WHERE jobs.session_id IN ({placeholders})
            ORDER BY created_at DESC LIMIT ?
            """,
            [*session_ids, limit],
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session_token_totals(
        self, session_ids: List[str]
    ) -> Dict[str, Dict[str, int]]:
        """[Cockpit] Batched per-session token totals (no N+1, no double-count).

        Authoritative per-request token columns live in llm_model_requests (one
        table deeper than llm_turns, which has no session_id); we join on turn_id
        and MUST filter is_duplicate=0 or retried/duplicated requests double-count.

        Cache de-duplication (A65): codex/adapter rows are recorded with
        ``input_token_semantics='includes_cache'`` — ``input_tokens`` already
        CONTAINS the cached-input portion AND ``cache_read_tokens`` reports it
        separately. Adding both double-counts the cached tokens (the codex burn
        was inflated ~2x). We subtract the cache from ``input`` for exactly those
        sources (proven against the DB: input >= cache_read in every such row);
        claude rows are recorded exclusive-cache (input excludes the cache) and
        are left untouched.

        ``total`` is the sum of ALL FOUR buckets (input + output + cache_read +
        cache_creation) — the same definition ``pricing.TokenTotals.total`` uses.
        This was previously ``input + output`` (cache excluded), so the roster
        "Total tokens" and the Session-detail cost could disagree. Returns
        {session_id: {input, output, cache_read, cache_creation, total}}.
        Sessions with no recorded requests are simply absent (caller renders 0)."""
        if not session_ids:
            return {}
        placeholders = ",".join("?" for _ in session_ids)
        rows = self._conn().execute(
            f"""
            SELECT t.session_id AS session_id,
                   COALESCE(SUM({_COST_INPUT_EXPR}), 0)              AS input,
                   COALESCE(SUM(r.output_tokens), 0)                 AS output,
                   COALESCE(SUM(r.cache_read_tokens), 0)             AS cache_read,
                   COALESCE(SUM(r.cache_creation_tokens), 0)         AS cache_creation
            FROM llm_turns t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            WHERE t.session_id IN ({placeholders}) AND r.is_duplicate = 0
            GROUP BY t.session_id
            """,
            list(session_ids),
        ).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for r in rows:
            d = dict(r)
            d["total"] = (
                (d.get("input") or 0)
                + (d.get("output") or 0)
                + (d.get("cache_read") or 0)
                + (d.get("cache_creation") or 0)
            )
            out[d["session_id"]] = d
        return out

    def recent_cache_write(
        self, session_id: str, turns: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """[quota-resume] The LARGEST prompt-cache write this session ever
        performed — the only observed quantity that answers "what does resuming
        this conversation cost?".

        Resuming hours later re-writes the whole prompt cache (the provider's TTL
        is ~1h), so the cost scales with the CONVERSATION, not with the next
        prompt. Context size itself is NOT recorded (rows carry
        ``usage_granularity='invocation_total'`` /
        ``usage_coverage='aggregate_only'``), so the estimate uses the biggest
        cache write the session actually performed.

        The window is the WHOLE session, not a recent slice — this was a live
        defect. A fat Manager loads its bulk context on the FIRST turn (observed:
        220k on turn 1) and then only writes small deltas per turn, so the last-N
        window saw ~20k and told the operator a 336k resume cost a cent → it
        resumed ``in_place`` and paid the very rewrite this seam exists to avoid.
        A session that ever wrote 220k has at least a 220k context to rebuild, so
        the whole-session MAX is the honest lower bound. It can over-count a
        session that was ``/compact``-ed after a big write, but over-counting only
        ever recommends the CHEAP ``fresh_manager`` — it costs transcript detail,
        never money. ``turns`` (optional) caps the scan to the newest N turns for
        callers that explicitly want a recency window; the default is the whole
        session. ``is_duplicate=0`` so a retried invocation cannot double-count.
        Returns ``{cache_creation, model, observed_at}`` or None."""
        if turns is not None:
            rows = self._conn().execute(
                """
                SELECT r.cache_creation_tokens AS cache_creation,
                       r.model                 AS model,
                       COALESCE(t.ended_at, t.created_at) AS observed_at
                FROM (
                    SELECT turn_id, ended_at, created_at FROM llm_turns
                    WHERE session_id = ?
                    ORDER BY COALESCE(ended_at, created_at) DESC
                    LIMIT ?
                ) t
                JOIN llm_model_requests r ON r.turn_id = t.turn_id
                WHERE r.is_duplicate = 0
                """,
                (session_id, max(1, int(turns))),
            ).fetchall()
            if not rows:
                return None
            best = max(rows, key=lambda r: int(r["cache_creation"] or 0))
            return dict(best)
        # Whole-session largest cache write (the default, correct path). This
        # reads telemetry we already store (per-request cache_creation_tokens) —
        # it does NOT re-measure or re-derive anything. It is index-driven via
        # idx_llm_turns_session (session_id) → idx_llm_model_requests_turn: the
        # optimizer enters through this session's turns only. Do NOT drop that
        # index or reorder these joins — without the session_id index SQLite
        # falls back to a full scan of llm_model_requests, and this runs on the
        # UI-polled resume-state path.
        row = self._conn().execute(
            """
            SELECT r.cache_creation_tokens AS cache_creation,
                   r.model                 AS model,
                   COALESCE(t.ended_at, t.created_at) AS observed_at
            FROM llm_turns t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            WHERE t.session_id = ? AND r.is_duplicate = 0
            ORDER BY r.cache_creation_tokens DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # A65 cost read-model — bounded SQL aggregates over the same
    # llm_model_requests telemetry (is_duplicate=0 everywhere, includes_cache
    # corrected). Each call is ONE bounded GROUP BY; pricing lives in
    # src/control/cost_read_model.py.
    # ------------------------------------------------------------------

    def cost_usage_rows(
        self,
        *,
        dimension: str,
        granularity: str = "day",
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        repo_path: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Per (time-bucket, dimension, model) token rows — the explorer fuel.

        One bounded GROUP BY over llm_model_requests joined to their turn/session
        (no N+1). ``dimension`` is one of project|backend|model|role|session;
        ``granularity='day'`` buckets by the request's UTC date. Rows carry the
        normalized (uncached) input + the four raw buckets; the assembler prices
        per model in Python and rolls up. ``is_duplicate=0`` replicates the
        proven de-dup filter; turns with no session_id are excluded here and
        surfaced separately via ``cost_unattributed``."""
        if dimension not in _COST_DIMENSIONS:
            raise ValueError(f"unknown cost dimension: {dimension}")
        dim_expr: str = _COST_DIMENSIONS[dimension]
        bucket_expr: str = (
            "substr(COALESCE(r.started_at, t.started_at, t.created_at), 1, 10)"
            if granularity == "day"
            else "''"
        )
        where = ["r.is_duplicate = 0", "t.session_id IS NOT NULL"]
        params: List[Any] = []
        if from_ts:
            where.append("COALESCE(r.started_at, t.started_at, t.created_at) >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("COALESCE(r.started_at, t.started_at, t.created_at) <= ?")
            params.append(to_ts)
        if repo_path:
            where.append("s.repo_path = ?")
            params.append(repo_path)
        params.append(max(1, min(int(limit), 100_000)))
        rows = self._conn().execute(
            f"""
            SELECT {bucket_expr} AS bucket,
                   {dim_expr} AS dim,
                   COALESCE(NULLIF(r.model, ''), '') AS model,
                   SUM({_COST_INPUT_EXPR})                              AS input,
                   SUM(COALESCE(r.output_tokens, 0))                    AS output,
                   SUM(COALESCE(r.cache_read_tokens, 0))                AS cache_read,
                   SUM(COALESCE(r.cache_creation_tokens, 0))            AS cache_creation,
                   COALESCE(NULLIF(t.backend, ''), s.backend, 'unknown') AS backend,
                   COALESCE(NULLIF(s.case_role, ''), 'standalone')      AS role
            FROM llm_turns t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            LEFT JOIN sessions s ON s.session_id = t.session_id
            WHERE {' AND '.join(where)}
            GROUP BY bucket, dim, r.model
            ORDER BY bucket, dim, model
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def cost_case_rows(self, flow_run_id: str) -> List[Dict[str, Any]]:
        """Per (session, model) token rows for every session linked to a case.

        The operator-question fuel: manager + its flow_links-joined workers, each
        session's tokens split per model so USD prices correctly per model. The
        case's session roles come from the links (fetched by the assembler)."""
        rows = self._conn().execute(
            f"""
            SELECT t.session_id AS session_id,
                   COALESCE(NULLIF(r.model, ''), '') AS model,
                   SUM({_COST_INPUT_EXPR})                            AS input,
                   SUM(COALESCE(r.output_tokens, 0))                  AS output,
                   SUM(COALESCE(r.cache_read_tokens, 0))              AS cache_read,
                   SUM(COALESCE(r.cache_creation_tokens, 0))          AS cache_creation
            FROM flow_links fl
            JOIN llm_turns t ON t.session_id = fl.entity_id
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            WHERE fl.flow_run_id = ? AND fl.entity_type = 'session'
              AND r.is_duplicate = 0
            GROUP BY t.session_id, r.model
            ORDER BY t.session_id, model
            """,
            (flow_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def cost_session_model_rows(
        self,
        *,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        repo_path: Optional[str] = None,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        """Per (session, model) token rows within a window — the top-spenders
        fuel. The assembler prices per model, rolls each session up, and sorts by
        USD. Bounded (LIMIT) so the read stays cheap on a grown DB."""
        where = ["r.is_duplicate = 0", "t.session_id IS NOT NULL"]
        params: List[Any] = []
        if from_ts:
            where.append("COALESCE(r.started_at, t.started_at, t.created_at) >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("COALESCE(r.started_at, t.started_at, t.created_at) <= ?")
            params.append(to_ts)
        if repo_path:
            where.append("s.repo_path = ?")
            params.append(repo_path)
        params.append(max(1, min(int(limit), 100_000)))
        rows = self._conn().execute(
            f"""
            SELECT t.session_id AS session_id,
                   COALESCE(NULLIF(r.model, ''), '') AS model,
                   SUM({_COST_INPUT_EXPR})                            AS input,
                   SUM(COALESCE(r.output_tokens, 0))                  AS output,
                   SUM(COALESCE(r.cache_read_tokens, 0))              AS cache_read,
                   SUM(COALESCE(r.cache_creation_tokens, 0))          AS cache_creation,
                   s.repo_path                                        AS repo_path,
                   COALESCE(NULLIF(t.backend, ''), s.backend, 'unknown') AS backend,
                   COALESCE(NULLIF(s.case_role, ''), 'standalone')    AS role
            FROM llm_turns t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            LEFT JOIN sessions s ON s.session_id = t.session_id
            WHERE {' AND '.join(where)}
            GROUP BY t.session_id, r.model
            ORDER BY (SUM({_COST_INPUT_EXPR}) + COALESCE(SUM(r.output_tokens), 0)) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def cost_unattributed(
        self,
        *,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Per-model tokens on turns with NO session_id (not attributable to any
        session/project). Surfaced honestly as an ``unattributed`` bucket rather
        than silently dropped (the audit found 26 orphan-session + 14 no-session
        turns ≈ 11M tokens, ~0.2%)."""
        where = ["r.is_duplicate = 0", "t.session_id IS NULL"]
        params: List[Any] = []
        if from_ts:
            where.append("COALESCE(r.started_at, t.started_at, t.created_at) >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("COALESCE(r.started_at, t.started_at, t.created_at) <= ?")
            params.append(to_ts)
        rows = self._conn().execute(
            f"""
            SELECT COALESCE(NULLIF(r.model, ''), '') AS model,
                   SUM({_COST_INPUT_EXPR})                            AS input,
                   SUM(COALESCE(r.output_tokens, 0))                  AS output,
                   SUM(COALESCE(r.cache_read_tokens, 0))              AS cache_read,
                   SUM(COALESCE(r.cache_creation_tokens, 0))          AS cache_creation
            FROM llm_turns t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            WHERE {' AND '.join(where)}
            GROUP BY r.model
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_cost_projects(
        self,
        *,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Distinct repo_paths with usage — the project-filter dropdown fuel,
        ordered by total tokens so the spenders float to the top. Rows also carry
        the per-project token rollup for the dashboard's spend-by-project card."""
        where = ["r.is_duplicate = 0", "t.session_id IS NOT NULL"]
        params: List[Any] = []
        if from_ts:
            where.append("COALESCE(r.started_at, t.started_at, t.created_at) >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("COALESCE(r.started_at, t.started_at, t.created_at) <= ?")
            params.append(to_ts)
        params.append(max(1, min(int(limit), 1000)))
        rows = self._conn().execute(
            f"""
            SELECT COALESCE(NULLIF(s.repo_path, ''), '<no repo path>') AS repo_path,
                   SUM({_COST_INPUT_EXPR})                            AS input,
                   SUM(COALESCE(r.output_tokens, 0))                  AS output,
                   SUM(COALESCE(r.cache_read_tokens, 0))              AS cache_read,
                   SUM(COALESCE(r.cache_creation_tokens, 0))          AS cache_creation
            FROM llm_turns t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            LEFT JOIN sessions s ON s.session_id = t.session_id
            WHERE {' AND '.join(where)}
            GROUP BY s.repo_path
            ORDER BY (SUM({_COST_INPUT_EXPR}) + COALESCE(SUM(r.output_tokens), 0)
                      + COALESCE(SUM(r.cache_read_tokens), 0)) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session_turn_counts(
        self, session_ids: List[str]
    ) -> Dict[str, int]:
        """[Cockpit] Batched turn count per session (no N+1). Counts ALL turns
        (including ones with no model-request row yet, e.g. an in-flight turn), so
        it is an honest activity depth — the operator's "was the manager shallow?"
        signal — not just turns that produced billed requests."""
        if not session_ids:
            return {}
        placeholders = ",".join("?" for _ in session_ids)
        rows = self._conn().execute(
            f"""
            SELECT session_id, COUNT(*) AS turn_count
            FROM llm_turns WHERE session_id IN ({placeholders})
            GROUP BY session_id
            """,
            list(session_ids),
        ).fetchall()
        return {r["session_id"]: r["turn_count"] for r in rows}

    def get_terminal_jobs_since(self, since: str) -> List[Dict[str, Any]]:
        """Return jobs that reached a terminal state after `since`."""
        rows = self._conn().execute(
            "SELECT * FROM jobs WHERE updated_at > ? AND status IN ('done', 'failed', 'lost')",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_running_jobs_for_node(self, node_id: str) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM jobs WHERE node_id = ? AND status = 'running'",
            (node_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Mesh health samples (M5)
    # ------------------------------------------------------------------

    def record_mesh_health_sample(self, source: str = "manual") -> Dict[str, Any]:
        """Append one aggregate mesh health sample and enforce retention."""
        snapshot = self.stats()
        mesh_load = snapshot.get("mesh_load") or {}
        sampled_at: str = _now()
        row: Dict[str, Any] = {
            "sampled_at": sampled_at,
            "source": source,
            "sessions_busy": int(snapshot.get("sessions_busy") or 0),
            "tasks_pending": int(snapshot.get("tasks_pending") or 0),
            "tasks_claimed": int(snapshot.get("tasks_claimed") or 0),
            "nodes_online": int(snapshot.get("nodes_online") or 0),
            "nodes_total": int(snapshot.get("nodes_total") or 0),
            "slots_used": int(mesh_load.get("slots_used") or 0),
            "slots_total": int(mesh_load.get("slots_total") or 0),
            "slots_available": int(mesh_load.get("slots_available") or 0),
            "active_tasks": int(mesh_load.get("active_tasks") or 0),
            "stale_busy_sessions": int(mesh_load.get("stale_busy_sessions") or 0),
            "nodes_with_live_state": int(mesh_load.get("nodes_with_live_state") or 0),
            "nodes_without_live_state": int(mesh_load.get("nodes_without_live_state") or 0),
            "stale_live_state_nodes_json": json.dumps(mesh_load.get("stale_live_state_nodes") or []),
        }
        try:
            with self._write() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO mesh_health_samples (
                        sampled_at, source, sessions_busy, tasks_pending,
                        tasks_claimed, nodes_online, nodes_total, slots_used,
                        slots_total, slots_available, active_tasks,
                        stale_busy_sessions, nodes_with_live_state,
                        nodes_without_live_state, stale_live_state_nodes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["sampled_at"],
                        row["source"],
                        row["sessions_busy"],
                        row["tasks_pending"],
                        row["tasks_claimed"],
                        row["nodes_online"],
                        row["nodes_total"],
                        row["slots_used"],
                        row["slots_total"],
                        row["slots_available"],
                        row["active_tasks"],
                        row["stale_busy_sessions"],
                        row["nodes_with_live_state"],
                        row["nodes_without_live_state"],
                        row["stale_live_state_nodes_json"],
                    ),
                )
                row["id"] = int(cur.lastrowid)
            self.prune_mesh_health_samples()
        except Exception as e:
            logger.warning("event=db_record_mesh_health_sample_failed err=%s", e)
        return self._decode_mesh_health_sample(row)

    def maybe_record_mesh_health_sample(
        self,
        source: str = "manual",
        *,
        min_interval_seconds: float = 30.0,
    ) -> Optional[Dict[str, Any]]:
        """Append a sample at most once per source interval."""
        now = time.monotonic()
        with _mesh_health_sample_lock:
            last = _mesh_health_last_sample.get(source)
            if last is not None and now - last < max(1.0, min_interval_seconds):
                return None
            _mesh_health_last_sample[source] = now
        return self.record_mesh_health_sample(source=source)

    def list_mesh_health_samples(
        self,
        *,
        since: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 1000))
        params: List[Any] = []
        where = ""
        if since:
            where = "WHERE sampled_at >= ?"
            params.append(since)
        params.append(bounded_limit)
        rows = self._conn().execute(
            f"""
            SELECT * FROM mesh_health_samples
            {where}
            ORDER BY sampled_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._decode_mesh_health_sample(dict(r)) for r in rows]

    def prune_mesh_health_samples(
        self,
        *,
        retention_hours: int = 48,
        max_rows: int = 10000,
    ) -> None:
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=max(1, retention_hours))).isoformat()
        try:
            with self._write() as conn:
                conn.execute("DELETE FROM mesh_health_samples WHERE sampled_at < ?", (cutoff,))
                conn.execute(
                    """
                    DELETE FROM mesh_health_samples
                    WHERE id NOT IN (
                        SELECT id FROM mesh_health_samples
                        ORDER BY sampled_at DESC
                        LIMIT ?
                    )
                    """,
                    (max(1, max_rows),),
                )
        except Exception as e:
            logger.debug("event=db_prune_mesh_health_samples_failed err=%s", e)

    def _decode_mesh_health_sample(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raw_nodes = row.pop("stale_live_state_nodes_json", "[]")
        try:
            stale_nodes = json.loads(raw_nodes) if isinstance(raw_nodes, str) else raw_nodes
        except Exception:
            stale_nodes = []
        row["stale_live_state_nodes"] = stale_nodes if isinstance(stale_nodes, list) else []
        return row

    # ------------------------------------------------------------------
    # Web Push subscriptions (#21)
    # ------------------------------------------------------------------

    def upsert_push_subscription(
        self,
        endpoint: str,
        p256dh_key: str,
        auth_key: str,
        label: Optional[str] = None,
    ) -> None:
        """Insert or refresh a browser push subscription (idempotent by endpoint).

        Re-subscribing from the same browser re-enables and refreshes keys/label.
        """
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    """
                    INSERT INTO push_subscriptions
                        (endpoint, p256dh_key, auth_key, enabled, label,
                         last_error, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, NULL, ?, ?)
                    ON CONFLICT(endpoint) DO UPDATE SET
                        p256dh_key = excluded.p256dh_key,
                        auth_key   = excluded.auth_key,
                        enabled    = 1,
                        label      = excluded.label,
                        last_error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (endpoint, p256dh_key, auth_key, label, now, now),
                )
        except Exception as e:
            logger.warning("event=db_upsert_push_subscription_failed err=%s", e)

    def list_push_subscriptions(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        try:
            if enabled_only:
                rows = self._conn().execute(
                    "SELECT * FROM push_subscriptions WHERE enabled = 1 ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT * FROM push_subscriptions ORDER BY created_at"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("event=db_list_push_subscriptions_failed err=%s", e)
            return []

    def disable_push_subscription(self, endpoint: str) -> None:
        """Disable a subscription (unsubscribe or after a permanent send error)."""
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE push_subscriptions SET enabled = 0, updated_at = ? WHERE endpoint = ?",
                    (_now(), endpoint),
                )
        except Exception as e:
            logger.warning("event=db_disable_push_subscription_failed err=%s", e)

    def mark_push_error(self, endpoint: str, error: str) -> None:
        """Record the last transient send error without disabling the subscription."""
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE push_subscriptions SET last_error = ?, updated_at = ? WHERE endpoint = ?",
                    (str(error)[:500], _now(), endpoint),
                )
        except Exception as e:
            logger.debug("event=db_mark_push_error_failed err=%s", e)

    def list_recent_system_alerts(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Read-only surface for ``system_alerts`` — written by the external
        ``aiteam-healthcheck.sh`` liveness probe, which runs OUTSIDE this
        process on purpose (it must still work when the gateway itself is
        unresponsive). This process never writes the table, only reads it."""
        try:
            rows = self._conn().execute(
                "SELECT id, source, kind, message, detail, opened_at, resolved_at "
                "FROM system_alerts ORDER BY opened_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("event=db_list_system_alerts_failed err=%s", e)
            return []

    # ------------------------------------------------------------------
    # Session cache heartbeats (A80)
    # ------------------------------------------------------------------

    def recent_cache_evidence(self, session_id: str, turns: int = 5) -> Optional[Dict[str, Any]]:
        """Newest bounded cache-token evidence for one session.

        Returns the strongest cache-bearing request from the last ``turns`` turns:
        ``{cache_read_tokens, cache_creation_tokens, observed_at, task_id, model}``.
        Read-only and bounded by a small turn subquery so scheduler ticks do not
        scan the telemetry table.
        """
        rows = self._conn().execute(
            """
            SELECT r.cache_read_tokens, r.cache_creation_tokens, r.model,
                   COALESCE(t.ended_at, t.created_at) AS observed_at,
                   t.task_id
            FROM (
                SELECT turn_id, task_id, ended_at, created_at FROM llm_turns
                WHERE session_id = ?
                ORDER BY COALESCE(ended_at, created_at) DESC
                LIMIT ?
            ) t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            WHERE r.is_duplicate = 0
            """,
            (session_id, max(1, int(turns))),
        ).fetchall()
        if not rows:
            return None
        best = max(
            rows,
            key=lambda r: int(r["cache_read_tokens"] or 0) + int(r["cache_creation_tokens"] or 0),
        )
        return dict(best)

    def latest_cache_evidence(self, session_id: str, turns: int = 5) -> Optional[Dict[str, Any]]:
        """Latest successful cache-token evidence for one session.

        ``recent_cache_evidence`` intentionally returns the strongest request in
        a bounded window. Heartbeat scheduling needs freshness instead: the most
        recent successful turn that actually touched prompt-cache accounting.
        Quota refusals are not useful cache-preservation work: when quota is
        exhausted, heartbeat delivery must be blocked by quota state instead of
        pretending the refusal refreshed the session.
        """
        rows = self._conn().execute(
            """
            SELECT t.task_id, t.observed_at, MAX(r.model) AS model,
                   COALESCE(SUM(r.cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(r.cache_creation_tokens), 0) AS cache_creation_tokens
            FROM (
                SELECT turn_id, task_id, ended_at, created_at,
                       COALESCE(ended_at, created_at) AS observed_at
                FROM llm_turns
                WHERE session_id = ?
                  AND COALESCE(final_status, 'success') IN ('success', 'completed')
                ORDER BY COALESCE(ended_at, created_at) DESC
                LIMIT ?
            ) t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            WHERE r.is_duplicate = 0
            GROUP BY t.turn_id, t.task_id, t.observed_at
            ORDER BY observed_at DESC
            """,
            (session_id, max(1, int(turns))),
        ).fetchall()
        for row in rows:
            token_sum = int(row["cache_read_tokens"] or 0) + int(row["cache_creation_tokens"] or 0)
            if token_sum > 0:
                return dict(row)
        return None

    def cache_evidence_for_task(self, session_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Cache-token evidence for one task in one session, if telemetry landed."""
        sid = str(session_id or "").strip()
        tid = str(task_id or "").strip()
        if not sid or not tid:
            return None
        row = self._conn().execute(
            """
            SELECT t.task_id, COALESCE(t.ended_at, t.created_at) AS observed_at,
                   MAX(r.model) AS model,
                   COALESCE(SUM(r.cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(r.cache_creation_tokens), 0) AS cache_creation_tokens
            FROM llm_turns t
            JOIN llm_model_requests r ON r.turn_id = t.turn_id
            WHERE t.session_id = ?
              AND t.task_id = ?
              AND r.is_duplicate = 0
            GROUP BY t.task_id, COALESCE(t.ended_at, t.created_at)
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (sid, tid),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        token_sum = int(out.get("cache_read_tokens") or 0) + int(out.get("cache_creation_tokens") or 0)
        return out if token_sum > 0 else None

    def _cache_heartbeat_next_due(self, last_touch: Optional[str], interval_sec: int) -> str:
        base = _parse_datetime_utc(last_touch) or datetime.now(timezone.utc)
        return (base + timedelta(seconds=max(60, int(interval_sec)))).isoformat()

    def refresh_cache_heartbeat_from_recent_evidence(self, session_id: str) -> bool:
        """Advance an active heartbeat controller from authoritative turn data."""
        sid = str(session_id or "").strip()
        if not sid:
            return False
        evidence = self.latest_cache_evidence(sid)
        observed_at = str((evidence or {}).get("observed_at") or "").strip()
        observed_dt = _parse_datetime_utc(observed_at)
        if evidence is None or observed_dt is None:
            return False
        read_tokens = int(evidence.get("cache_read_tokens") or 0)
        creation_tokens = int(evidence.get("cache_creation_tokens") or 0)
        now = _now()
        changed = False
        with self._write() as conn:
            rows = conn.execute(
                """
                SELECT id, last_cache_touch_at, interval_sec
                FROM session_cache_heartbeats
                WHERE session_id = ? AND status IN ('observe_only', 'active')
                """,
                (sid,),
            ).fetchall()
            for row in rows:
                last_dt = _parse_datetime_utc(row["last_cache_touch_at"])
                if last_dt is not None and observed_dt <= last_dt:
                    continue
                next_due = self._cache_heartbeat_next_due(
                    observed_at,
                    int(row["interval_sec"] or cache_heartbeat_interval_sec()),
                )
                conn.execute(
                    """
                    UPDATE session_cache_heartbeats
                    SET last_cache_touch_at = ?, last_cache_read_tokens = ?,
                        last_cache_creation_tokens = ?, next_due_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (observed_at, read_tokens, creation_tokens, next_due, now, row["id"]),
                )
                changed = True
        return changed

    def refresh_cache_heartbeats_from_recent_evidence(self, limit: int = 100) -> int:
        """Refresh active heartbeat clocks before evaluating due work."""
        rows = self._conn().execute(
            """
            SELECT session_id FROM session_cache_heartbeats
            WHERE status IN ('observe_only', 'active')
            ORDER BY
                CASE
                    WHEN next_due_at IS NOT NULL AND next_due_at <= ? THEN 0
                    ELSE 1
                END,
                COALESCE(next_due_at, updated_at) ASC,
                updated_at DESC
            LIMIT ?
            """,
            (_now(), max(1, min(int(limit), 500))),
        ).fetchall()
        changed = 0
        seen: set[str] = set()
        for row in rows:
            session_id = str(row["session_id"] or "")
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            changed += 1 if self.refresh_cache_heartbeat_from_recent_evidence(session_id) else 0
        return changed

    def ensure_cache_heartbeat_owner(
        self,
        session_id: str,
        *,
        reason: str,
        owner_type: str,
        owner_id: str,
        expected_runtime_sec: Optional[int] = None,
        expires_at: Optional[str] = None,
        max_beats: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create/update a heartbeat controller owner.

        Observe-only is default-on; paid active mode requires
        ``CACHE_HEARTBEAT_ACTIVE``. Adding a second owner to an active episode
        reuses the controller and does not reset its beat count.
        """
        if not (cache_heartbeat_observe_enabled() or cache_heartbeat_active_enabled()):
            return None
        sid = str(session_id or "").strip()
        oid = str(owner_id or "").strip()
        if not sid or not oid:
            return None
        reason_s = str(reason or "").strip()[:64]
        owner_type_s = str(owner_type or "").strip()[:64]
        if reason_s not in ("case_wait_group", "watched_job", "manual", "agent_requested"):
            return None
        if not owner_type_s:
            return None
        now = _now()
        ttl = cache_heartbeat_ttl_sec()
        interval = cache_heartbeat_interval_sec()
        hard = cache_heartbeat_hard_max_beats()
        maxb = min(max(1, int(max_beats or cache_heartbeat_max_beats_default())), hard)
        default_exp = (
            datetime.now(timezone.utc) + timedelta(seconds=(interval * maxb) + ttl)
        ).isoformat()
        exp = expires_at or default_exp
        evidence = self.latest_cache_evidence(sid)
        last_touch = (evidence or {}).get("observed_at") or now
        status = "active" if cache_heartbeat_active_enabled() else "observe_only"
        heartbeat_id = ""
        try:
            with self._write() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM session_cache_heartbeats
                    WHERE session_id = ? AND status IN ('observe_only', 'active')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (sid,),
                ).fetchone()
                if row is None:
                    heartbeat_id = f"schb_{uuid.uuid4().hex[:12]}"
                    conn.execute(
                        """
                        INSERT INTO session_cache_heartbeats (
                            id, session_id, status, ttl_sec, interval_sec,
                            next_due_at, expires_at, beat_count, max_beats,
                            hard_max_beats, last_cache_touch_at,
                            last_cache_read_tokens, last_cache_creation_tokens,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            heartbeat_id, sid, status, ttl, interval,
                            self._cache_heartbeat_next_due(str(last_touch), interval),
                            exp, maxb, hard, str(last_touch),
                            int((evidence or {}).get("cache_read_tokens") or 0),
                            int((evidence or {}).get("cache_creation_tokens") or 0),
                            now, now,
                        ),
                    )
                else:
                    current = dict(row)
                    heartbeat_id = str(current["id"])
                    next_status = "active" if cache_heartbeat_active_enabled() else current["status"]
                    current_touch = _parse_datetime_utc(current.get("last_cache_touch_at"))
                    evidence_touch = _parse_datetime_utc(str(last_touch))
                    should_refresh_touch = evidence_touch is not None and (
                        current_touch is None or evidence_touch > current_touch
                    )
                    next_due = (
                        self._cache_heartbeat_next_due(str(last_touch), interval)
                        if should_refresh_touch
                        else current.get("next_due_at")
                    )
                    conn.execute(
                        """
                        UPDATE session_cache_heartbeats
                        SET status = ?, ttl_sec = ?, interval_sec = ?,
                            expires_at = MAX(COALESCE(expires_at, ''), ?),
                            max_beats = MAX(max_beats, ?),
                            hard_max_beats = MAX(hard_max_beats, ?),
                            last_cache_touch_at = CASE WHEN ? THEN ? ELSE last_cache_touch_at END,
                            last_cache_read_tokens = CASE WHEN ? THEN ? ELSE last_cache_read_tokens END,
                            last_cache_creation_tokens = CASE WHEN ? THEN ? ELSE last_cache_creation_tokens END,
                            next_due_at = COALESCE(?, next_due_at), updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            next_status, ttl, interval, exp, maxb, hard,
                            1 if should_refresh_touch else 0,
                            str(last_touch),
                            1 if should_refresh_touch else 0,
                            int((evidence or {}).get("cache_read_tokens") or 0),
                            1 if should_refresh_touch else 0,
                            int((evidence or {}).get("cache_creation_tokens") or 0),
                            next_due, now, heartbeat_id,
                        ),
                    )
                owner_row = conn.execute(
                    """
                    SELECT * FROM session_cache_heartbeat_owners
                    WHERE session_id = ? AND reason = ? AND owner_type = ?
                      AND owner_id = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (sid, reason_s, owner_type_s, oid),
                ).fetchone()
                if owner_row is None:
                    conn.execute(
                        """
                        INSERT INTO session_cache_heartbeat_owners (
                            id, heartbeat_id, session_id, reason, owner_type,
                            owner_id, status, expected_runtime_sec, started_at,
                            expires_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                        """,
                        (
                            f"schbo_{uuid.uuid4().hex[:12]}",
                            heartbeat_id, sid, reason_s, owner_type_s, oid,
                            int(expected_runtime_sec) if expected_runtime_sec is not None else None,
                            now, expires_at, now, now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE session_cache_heartbeat_owners
                        SET heartbeat_id = ?, expected_runtime_sec = COALESCE(?, expected_runtime_sec),
                            expires_at = COALESCE(?, expires_at), updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            heartbeat_id,
                            int(expected_runtime_sec) if expected_runtime_sec is not None else None,
                            expires_at, now, owner_row["id"],
                        ),
                    )
            return self.get_cache_heartbeat(heartbeat_id)
        except Exception as e:
            logger.warning("event=db_cache_heartbeat_owner_failed session_id=%s err=%s", sid, e)
            return None

    def get_cache_heartbeat(self, heartbeat_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM session_cache_heartbeats WHERE id = ?",
            (heartbeat_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["owners"] = self.list_cache_heartbeat_owners(str(out["id"]))
        return out

    def list_cache_heartbeat_owners(
        self, heartbeat_id: str, *, active_only: bool = False,
    ) -> List[Dict[str, Any]]:
        where = "WHERE heartbeat_id = ?"
        params: List[Any] = [heartbeat_id]
        if active_only:
            where += " AND status = 'active'"
        rows = self._conn().execute(
            f"SELECT * FROM session_cache_heartbeat_owners {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_cache_heartbeats(
        self,
        *,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        rows = self._conn().execute(
            f"SELECT * FROM session_cache_heartbeats {where} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        out = [dict(r) for r in rows]
        if out and len(out) <= 50:
            ids = [str(hb["id"]) for hb in out]
            placeholders = ",".join("?" for _ in ids)
            owner_rows = self._conn().execute(
                f"SELECT * FROM session_cache_heartbeat_owners WHERE heartbeat_id IN ({placeholders}) ORDER BY created_at ASC",
                ids,
            ).fetchall()
            owners_by_hb: Dict[str, List[Dict[str, Any]]] = {hb_id: [] for hb_id in ids}
            for r in owner_rows:
                owners_by_hb.setdefault(str(r["heartbeat_id"]), []).append(dict(r))
            for hb in out:
                hb["owners"] = owners_by_hb.get(str(hb["id"]), [])
        return out

    def due_cache_heartbeats(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT * FROM session_cache_heartbeats
            WHERE status = 'active'
              AND next_due_at IS NOT NULL
              AND next_due_at <= ?
            ORDER BY next_due_at ASC LIMIT ?
            """,
            (_now(), max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(r) for r in rows]

    def stop_cache_heartbeat_owner(
        self,
        session_id: str,
        *,
        reason: str,
        owner_type: str,
        owner_id: str,
        stop_reason: str,
    ) -> int:
        now = _now()
        with self._write() as conn:
            conn.execute(
                """
                UPDATE session_cache_heartbeat_owners
                SET status = 'stopped', stop_reason = ?, updated_at = ?
                WHERE session_id = ? AND reason = ? AND owner_type = ?
                  AND owner_id = ? AND status = 'active'
                """,
                (stop_reason[:256], now, session_id, reason, owner_type, owner_id),
            )
            changed = int(conn.execute("SELECT changes()").fetchone()[0])
        self.stop_cache_heartbeats_without_owners()
        return changed

    def stop_cache_heartbeat(self, heartbeat_id: str, stop_reason: str) -> bool:
        now = _now()
        with self._write() as conn:
            conn.execute(
                """
                UPDATE session_cache_heartbeats
                SET status = 'stopped', circuit_reason = COALESCE(?, circuit_reason),
                    updated_at = ?
                WHERE id = ? AND status IN ('observe_only', 'active')
                """,
                (stop_reason[:256], now, heartbeat_id),
            )
            changed = int(conn.execute("SELECT changes()").fetchone()[0])
            conn.execute(
                """
                UPDATE session_cache_heartbeat_owners
                SET status = 'stopped', stop_reason = ?, updated_at = ?
                WHERE heartbeat_id = ? AND status = 'active'
                """,
                (stop_reason[:256], now, heartbeat_id),
            )
        return changed > 0

    def stop_cache_heartbeats_without_owners(self) -> int:
        now = _now()
        with self._write() as conn:
            rows = conn.execute(
                """
                SELECT h.id FROM session_cache_heartbeats h
                WHERE h.status IN ('observe_only', 'active')
                  AND NOT EXISTS (
                    SELECT 1 FROM session_cache_heartbeat_owners o
                    WHERE o.heartbeat_id = h.id AND o.status = 'active'
                  )
                """
            ).fetchall()
            ids = [str(r["id"]) for r in rows]
            for heartbeat_id in ids:
                conn.execute(
                    """
                    UPDATE session_cache_heartbeats
                    SET status = 'stopped', circuit_reason = COALESCE(circuit_reason, 'no_active_owners'),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, heartbeat_id),
                )
        return len(ids)

    def record_cache_heartbeat_sent(self, heartbeat_id: str, task_id: str) -> None:
        now = _now()
        with self._write() as conn:
            conn.execute(
                """
                UPDATE session_cache_heartbeats
                SET last_beat_task_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (task_id, now, heartbeat_id),
            )

    def record_cache_heartbeat_result(
        self,
        heartbeat_id: str,
        task_id: str,
        *,
        success: bool,
        output: str = "",
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        error_class: str = "",
    ) -> None:
        """Record one heartbeat turn outcome and advance/stop the controller."""
        row = self.get_cache_heartbeat(heartbeat_id)
        if row is None:
            return
        with self._write() as conn:
            self._apply_cache_heartbeat_result(
                conn, row, task_id, success=success, output=output,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens, error_class=error_class,
            )

    def _apply_cache_heartbeat_result(
        self,
        conn: sqlite3.Connection,
        row: Dict[str, Any],
        task_id: str,
        *,
        success: bool,
        output: str = "",
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        error_class: str = "",
    ) -> None:
        """The controller transition of one heartbeat outcome, written INSIDE
        the caller's transaction (legacy ``record_cache_heartbeat_result`` and
        the A82 Stage 4d durable finalizer share it)."""
        heartbeat_id = str(row["id"])
        now = _now()
        read_tokens = max(0, int(cache_read_tokens or 0))
        creation_tokens = max(0, int(cache_creation_tokens or 0))
        beat_count = int(row.get("beat_count") or 0) + 1
        status = str(row.get("status") or "active")
        circuit: Optional[str] = None
        if "STOP_CACHE_HEARTBEAT" in str(output or ""):
            status = "stopped"
            circuit = "agent_requested_stop"
        elif error_class in ("usage_limit", "rate_limit", "upstream_error"):
            status = "stopped"
            circuit = f"heartbeat_{error_class}"
        elif not success:
            status = "stopped"
            circuit = "heartbeat_failed"
        elif creation_tokens >= cache_heartbeat_min_cache_tokens() and read_tokens < creation_tokens:
            status = "circuit_open"
            circuit = "cache_miss_rewrite"
        elif beat_count >= min(int(row.get("max_beats") or 0), int(row.get("hard_max_beats") or 0)):
            status = "stopped"
            circuit = "max_beats_reached"
        next_due = self._cache_heartbeat_next_due(now, int(row.get("interval_sec") or 2700))
        if status not in ("active", "observe_only"):
            next_due = None
        conn.execute(
            """
            UPDATE session_cache_heartbeats
            SET beat_count = ?, last_beat_task_id = ?,
                last_cache_touch_at = CASE WHEN ? > 0 THEN ? ELSE last_cache_touch_at END,
                last_cache_read_tokens = ?, last_cache_creation_tokens = ?,
                next_due_at = ?, status = ?, circuit_reason = COALESCE(?, circuit_reason),
                updated_at = ?
            WHERE id = ?
            """,
            (
                beat_count, task_id, read_tokens, now, read_tokens, creation_tokens,
                next_due, status, circuit, now, heartbeat_id,
            ),
        )
        if status not in ("active", "observe_only"):
            conn.execute(
                """
                UPDATE session_cache_heartbeat_owners
                SET status = 'stopped', stop_reason = ?, updated_at = ?
                WHERE heartbeat_id = ? AND status = 'active'
                """,
                (circuit or status, now, heartbeat_id),
            )

    def expire_cache_heartbeat_state(self) -> int:
        """Stop expired owners/controllers and return changed rows count."""
        now = _now()
        changed = 0
        with self._write() as conn:
            conn.execute(
                """
                UPDATE session_cache_heartbeat_owners
                SET status = 'stopped', stop_reason = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, now),
            )
            changed += int(conn.execute("SELECT changes()").fetchone()[0])
            conn.execute(
                """
                UPDATE session_cache_heartbeats
                SET status = 'stopped', circuit_reason = COALESCE(circuit_reason, 'expired'),
                    updated_at = ?
                WHERE status IN ('observe_only', 'active')
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, now),
            )
            changed += int(conn.execute("SELECT changes()").fetchone()[0])
        changed += self.stop_cache_heartbeats_without_owners()
        return changed

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the per-thread connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def stats(self) -> Dict[str, Any]:
        """Quick health snapshot — useful for /status Telegram command."""
        conn = self._conn()
        # Exclude the gateway's own liveness-only self-node (empty backends, no
        # tailscale IP) from every operator-facing counter derived here — it is
        # infra plumbing, not fleet capacity. Registration/reaping still see it
        # via list_nodes(); this filter is presentation-only.
        nodes = [n for n in self.list_nodes() if not _is_gateway_self_node(n)]
        mesh_load = _mesh_load_stats(nodes, self.list_stale_busy_sessions(limit=10000))
        return {
            "sessions_total":   conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "sessions_busy":    conn.execute("SELECT COUNT(*) FROM sessions WHERE status='busy'").fetchone()[0],
            "tasks_pending":    conn.execute("SELECT COUNT(*) FROM mesh_tasks WHERE status='pending'").fetchone()[0],
            "tasks_claimed":    conn.execute("SELECT COUNT(*) FROM mesh_tasks WHERE status='claimed'").fetchone()[0],
            "tasks_completed":  conn.execute("SELECT COUNT(*) FROM mesh_tasks WHERE status='completed'").fetchone()[0],
            "tasks_failed":     conn.execute("SELECT COUNT(*) FROM mesh_tasks WHERE status IN ('failed','failed_node_offline')").fetchone()[0],
            # Derived from the same `nodes` snapshot as mesh_load (consistent view).
            # nodes_total is the current fleet, not every row ever registered, so
            # long-dead test/canary inventory no longer inflates "N/M online".
            "nodes_online":     sum(1 for n in nodes if n.get("status") == "online"),
            "nodes_total":      _count_fleet_nodes(nodes),
            "schema_version":   conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0,
            "db_path":          str(self._path),
            "mesh_load":        mesh_load,
        }


# ---------------------------------------------------------------------------
# Migrations — add future ALTER TABLE statements here
# ---------------------------------------------------------------------------

def _get_migrations() -> List[tuple]:
    """Return list of (version, sql) tuples in ascending version order.

    Version 1 is the baseline — it's recorded after the initial _DDL runs
    so the migration framework has a clean starting point.

    To add a migration:
        1. Append (N, "ALTER TABLE ...") to this list.
    _CURRENT_VERSION is derived from this list automatically (below) — there is
    no separate constant to bump.
    """
    return [
        (1, ""),  # baseline marker — DDL already applied above
        (2, "ALTER TABLE nodes ADD COLUMN projects_root TEXT NOT NULL DEFAULT ''"),
        (3, "ALTER TABLE nodes ADD COLUMN repos TEXT NOT NULL DEFAULT '[]'"),
        (4, ""),  # jobs table added to _DDL; marker for clean version tracking
        (5, "ALTER TABLE jobs ADD COLUMN cwd TEXT"),  # working directory for spawn mode
        (6, "ALTER TABLE nodes ADD COLUMN incarnation_id TEXT"),  # per-restart UUID for orphan detection
        (7, "ALTER TABLE mesh_tasks ADD COLUMN claimer_incarnation TEXT"),  # matched against nodes.incarnation_id by reaper
        (8, "ALTER TABLE nodes ADD COLUMN live_state TEXT"),  # JSON snapshot sent with each heartbeat (slots, active tasks)
        (9, "ALTER TABLE nodes ADD COLUMN live_state_updated_at TEXT"),  # timestamp of last live_state update; NULL = never received
        (10, """
            ALTER TABLE jobs ADD COLUMN last_checked_at TEXT;
            ALTER TABLE jobs ADD COLUMN last_probe_error TEXT;
            ALTER TABLE jobs ADD COLUMN last_seen_command TEXT;
            ALTER TABLE jobs ADD COLUMN last_seen_started_epoch REAL
        """),  # durable watched-job process identity probes
        (11, "ALTER TABLE sessions ADD COLUMN model TEXT"),  # per-session picked model; NULL = backend default
        (12, "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT '{\"channel\":\"telegram\",\"kind\":\"user\"}'"),  # transport-neutral origin tag {channel, kind}; old rows default to telegram/user
        (13, _APPROVALS_SCHEMA_SQL),  # Web UI lineage: durable approval gate
        (14, _LLM_TELEMETRY_SCHEMA_SQL),  # main lineage: durable LLM telemetry
        (15, ""),  # marker retained for main telemetry history compatibility
        (16, ""),  # merged-lineage marker; _ensure_merged_schema repairs both paths
        (17, """
            ALTER TABLE mesh_tasks ADD COLUMN prompt TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN reply_text TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN parsed_output_json TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN file_changes_json TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN files_modified_json TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN usage_json TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN error_class TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN return_code INTEGER
        """),  # artifact-complete task rows: full reply + structured fields so
               # the DB is self-sufficient and results/task_*.json can be dropped.
               # reply_text holds the FULL untruncated assistant reply (the chat
               # source); the legacy `result` JSON keeps output[:2000] for back-compat.
        (18, """
            ALTER TABLE sessions ADD COLUMN driver_type TEXT NOT NULL DEFAULT '';
            ALTER TABLE sessions ADD COLUMN driver_status TEXT NOT NULL DEFAULT '';
            ALTER TABLE sessions ADD COLUMN cache_health TEXT NOT NULL DEFAULT 'unknown';
            ALTER TABLE sessions ADD COLUMN cache_unhealthy_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE sessions ADD COLUMN previous_backend_session_ids TEXT NOT NULL DEFAULT '[]'
        """),  # P0 replacement-engine driver state: persisted so the
               # cache_unhealthy_count>=2 guard and driver_status=lost guard
               # survive across turns and gateway restarts.
        (19, """
            CREATE TABLE IF NOT EXISTS mesh_health_samples (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                sampled_at                  TEXT NOT NULL,
                source                      TEXT NOT NULL,
                sessions_busy               INTEGER NOT NULL DEFAULT 0,
                tasks_pending               INTEGER NOT NULL DEFAULT 0,
                tasks_claimed               INTEGER NOT NULL DEFAULT 0,
                nodes_online                INTEGER NOT NULL DEFAULT 0,
                nodes_total                 INTEGER NOT NULL DEFAULT 0,
                slots_used                  INTEGER NOT NULL DEFAULT 0,
                slots_total                 INTEGER NOT NULL DEFAULT 0,
                slots_available             INTEGER NOT NULL DEFAULT 0,
                active_tasks                INTEGER NOT NULL DEFAULT 0,
                stale_busy_sessions         INTEGER NOT NULL DEFAULT 0,
                nodes_with_live_state       INTEGER NOT NULL DEFAULT 0,
                nodes_without_live_state    INTEGER NOT NULL DEFAULT 0,
                stale_live_state_nodes_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_mesh_health_samples_sampled_at
                ON mesh_health_samples(sampled_at)
        """),  # M5 operational mesh health trend ledger.
        (20, """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint    TEXT PRIMARY KEY,
                p256dh_key  TEXT NOT NULL,
                auth_key    TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                label       TEXT,
                last_error  TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """),  # #21 Web Push: durable browser push subscriptions. endpoint is the
               # natural key (unique per browser/device); re-subscribe is an upsert.
        (21, """
            CREATE TABLE IF NOT EXISTS flow_runs (
                flow_run_id     TEXT PRIMARY KEY,
                task_id         TEXT,
                current_stage   TEXT,
                objective_lock  TEXT,
                created_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_flow_runs_task
                ON flow_runs(task_id)
        """),  # A19 v0.4 §13 item 1: FlowRun RECORD (not a stage machine). One row
               # per dispatch flow; nothing reads current_stage to drive behavior.
        (22, """
            ALTER TABLE flow_runs ADD COLUMN approved_plan TEXT;
            ALTER TABLE flow_runs ADD COLUMN plan_review TEXT;
            ALTER TABLE flow_runs ADD COLUMN burn_down_items TEXT;
            ALTER TABLE flow_runs ADD COLUMN execution_result TEXT;
            ALTER TABLE flow_runs ADD COLUMN implementation_review TEXT;
            ALTER TABLE flow_runs ADD COLUMN waived_findings TEXT;
            ALTER TABLE flow_runs ADD COLUMN closure_summary TEXT;
            ALTER TABLE flow_runs ADD COLUMN role_assignments TEXT;
            ALTER TABLE flow_runs ADD COLUMN artifact_links TEXT;
            ALTER TABLE flow_runs ADD COLUMN status TEXT;
            ALTER TABLE flow_runs ADD COLUMN updated_at TEXT;
            ALTER TABLE flow_runs ADD COLUMN parent_flow_run_id TEXT;
            ALTER TABLE flow_runs ADD COLUMN dispatched_by TEXT;
            ALTER TABLE flow_runs ADD COLUMN dispatch_file TEXT
        """),  # A21 v0.4 §11: promote the 5-column FlowRun RECORD to the full
               # state model + dispatch lineage. All ADDITIVE + NULLable so A19's
               # byte-identical write stays valid and existing rows are untouched.
               # Still a RECORD: nothing reads these to drive execution. Structured
               # fields (plan_review, burn_down_items, execution_result,
               # implementation_review, waived_findings, role_assignments,
               # artifact_links) are JSON-encoded TEXT. parent_flow_run_id is the
               # lineage back-reference (child→parent recovered by reverse-lookup;
               # no redundant worker_task_ids column).
        (23, """
            CREATE TABLE IF NOT EXISTS flow_links (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                flow_run_id   TEXT NOT NULL,
                entity_type   TEXT NOT NULL,
                entity_id     TEXT NOT NULL,
                role          TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                created_by    TEXT,
                metadata_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_flow_links_unique
                ON flow_links(flow_run_id, entity_type, entity_id, role);
            CREATE INDEX IF NOT EXISTS idx_flow_links_flow
                ON flow_links(flow_run_id);
            CREATE INDEX IF NOT EXISTS idx_flow_links_entity
                ON flow_links(entity_type, entity_id);
            CREATE TABLE IF NOT EXISTS flow_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                flow_run_id   TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                actor         TEXT NOT NULL,
                from_state    TEXT,
                to_state      TEXT,
                entity_type   TEXT,
                entity_id     TEXT,
                payload_json  TEXT,
                created_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_flow_events_flow
                ON flow_events(flow_run_id, id)
        """),  # A25 Work Control Substrate: authoritative case↔entity relationship
               # ledger (flow_links) + append-only case audit trail (flow_events).
               # ADDITIVE + NULLable; a RECORD/relationship layer — nothing here is
               # read to DRIVE execution. flow_links relate EXISTING entities only
               # (not a second task ledger); flow_events store compact references,
               # not bulk evidence. The unique index makes link writes idempotent.
               # The optional convenience columns (mesh_tasks/approvals.flow_run_id)
               # are added defensively in _ensure_substrate_columns (below) so a
               # DB missing an optional table can never abort this migration.
        (24, """
            ALTER TABLE flow_runs ADD COLUMN completion_criteria TEXT;
            ALTER TABLE sessions ADD COLUMN current_case_id TEXT;
            ALTER TABLE sessions ADD COLUMN case_role TEXT
        """),  # A36 M2.5 Case admission: `completion_criteria` is the checkable
               # "done" condition a Case is opened with (demanded by close_case in
               # A37). `current_case_id`/`case_role` give a Session a DURABLE Case
               # affiliation that survives across turns (set on attach/open, cleared
               # on close in A37) — replacing the per-read most-recent-link derive.
               # All ADDITIVE + NULLable ⇒ existing rows/writers untouched.
        (25, "ALTER TABLE sessions ADD COLUMN effort TEXT"),  # per-session thinking effort; NULL = backend default
        (26, "ALTER TABLE sessions ADD COLUMN role_boot TEXT"),  # [Worker role] explicit opt-in role-boot signal; NULL = tier-0 default
        (27, "ALTER TABLE sessions ADD COLUMN continued_from TEXT"),  # [Session-fork] session→session lineage; NULL = not a continuation
        (28, """
            CREATE TABLE IF NOT EXISTS runtime_flags (
                flag_name TEXT PRIMARY KEY,
                value     TEXT NOT NULL,
                source    TEXT NOT NULL DEFAULT 'api',
                set_at    TEXT NOT NULL,
                set_by    TEXT NOT NULL DEFAULT ''
            )
        """),  # Agent-operable feature flag overrides. Missing row => env/default fallback.
        (29, """
            ALTER TABLE sessions ADD COLUMN keep_pinned INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE sessions ADD COLUMN keep_note TEXT NOT NULL DEFAULT ''
        """),  # Operator keep marker + note. Distinct from machine_id affinity pinning.
        (30, """
            CREATE TABLE IF NOT EXISTS system_alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL DEFAULT 'healthcheck',
                kind        TEXT NOT NULL,
                message     TEXT NOT NULL,
                detail      TEXT NOT NULL DEFAULT '',
                opened_at   TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_system_alerts_opened_at
                ON system_alerts(opened_at DESC)
        """),  # Durable liveness-outage log. Written by ~/scripts/aiteam-healthcheck.sh
               # (a process OUTSIDE the gateway, deliberately — it must record an
               # outage even when the gateway itself is unresponsive), not by this
               # process. The gateway only ever reads it, for the Web UI banner and
               # Telegram alert history. Schema is duplicated defensively in the
               # healthcheck script's own CREATE TABLE IF NOT EXISTS so a fresh host
               # can alert before its first post-migration gateway boot.
        (31, """
            CREATE TABLE IF NOT EXISTS session_cache_heartbeats (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                ttl_sec INTEGER NOT NULL,
                interval_sec INTEGER NOT NULL,
                next_due_at TEXT,
                expires_at TEXT,
                beat_count INTEGER NOT NULL DEFAULT 0,
                max_beats INTEGER NOT NULL,
                hard_max_beats INTEGER NOT NULL,
                last_beat_task_id TEXT,
                last_cache_touch_at TEXT,
                last_cache_read_tokens INTEGER,
                last_cache_creation_tokens INTEGER,
                circuit_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_session_cache_heartbeats_active
                ON session_cache_heartbeats(session_id)
                WHERE status IN ('observe_only', 'active');
            CREATE INDEX IF NOT EXISTS idx_session_cache_heartbeats_due
                ON session_cache_heartbeats(status, next_due_at);
            CREATE TABLE IF NOT EXISTS session_cache_heartbeat_owners (
                id TEXT PRIMARY KEY,
                heartbeat_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                owner_type TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                expected_runtime_sec INTEGER,
                started_at TEXT NOT NULL,
                expires_at TEXT,
                stop_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_session_cache_heartbeat_owners_active
                ON session_cache_heartbeat_owners(session_id, reason, owner_type, owner_id)
                WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS idx_session_cache_heartbeat_owners_hb
                ON session_cache_heartbeat_owners(heartbeat_id, status)
        """),  # A80 session-cache heartbeat controllers and owner records.
        (32, "ALTER TABLE nodes ADD COLUMN model_capabilities TEXT NOT NULL DEFAULT '{}'"),
        (33, """
            CREATE INDEX IF NOT EXISTS idx_mesh_tasks_session_created
                ON mesh_tasks(session_id, created_at)
        """),  # A81: composite index for the transcript read
               # (WHERE session_id=? ORDER BY created_at ASC) — covered range scan.
        (34, """
            ALTER TABLE mesh_tasks ADD COLUMN queue_protocol INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE mesh_tasks ADD COLUMN queue_sequence INTEGER;
            ALTER TABLE mesh_tasks ADD COLUMN turn_source TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN sender_session_id TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN turn_kind TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN idempotency_scope TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN idempotency_key TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN admission_hash TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE mesh_tasks ADD COLUMN not_before TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN expires_at TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN activated_at TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN started_at TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN claim_token TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN claim_carrier_kind TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN claim_incarnation TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN coalesce_key TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN blocked_reason TEXT;
            ALTER TABLE sessions ADD COLUMN turn_queue_enrolled INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE sessions ADD COLUMN turn_queue_paused INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE sessions ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 1;
            CREATE TABLE IF NOT EXISTS mesh_turn_revisions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       TEXT NOT NULL,
                revision      INTEGER NOT NULL,
                actor         TEXT NOT NULL DEFAULT '',
                change_kind   TEXT NOT NULL,
                body          TEXT,
                attachments_json TEXT,
                created_at    TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mesh_turn_revisions_unique
                ON mesh_turn_revisions(task_id, revision);
            CREATE INDEX IF NOT EXISTS idx_mesh_turn_revisions_task
                ON mesh_turn_revisions(task_id, revision);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mesh_turns_one_active_session
                ON mesh_tasks(session_id)
                WHERE queue_protocol = 1 AND session_id IS NOT NULL
                  AND status IN ('pending', 'claimed', 'running', 'recovery_required');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mesh_turns_session_sequence
                ON mesh_tasks(session_id, queue_sequence)
                WHERE queue_protocol = 1 AND queue_sequence IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_mesh_turns_waiting
                ON mesh_tasks(created_at, id)
                WHERE queue_protocol = 1 AND status = 'queued';
            CREATE INDEX IF NOT EXISTS idx_mesh_turns_session_open
                ON mesh_tasks(session_id, queue_sequence)
                WHERE queue_protocol = 1
                  AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mesh_turns_idempotency
                ON mesh_tasks(idempotency_scope, idempotency_key)
                WHERE queue_protocol = 1 AND idempotency_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mesh_turns_active_coalesce
                ON mesh_tasks(coalesce_key)
                WHERE queue_protocol = 1 AND coalesce_key IS NOT NULL
                  AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')
        """),  # A82 Stage 2: managed session turn queue (design §§3-4). All
               # mesh_tasks columns are ADDITIVE + NULLable/DEFAULT-safe so legacy
               # protocol-0 writers (queue_protocol defaults 0) are byte-identical
               # and existing rows are untouched. The unique indexes are ALL
               # partial on `queue_protocol = 1`, so they can NEVER fire on legacy
               # data — a fixture with duplicate legacy active rows, cancellation
               # rows and NULL-session sentinel/scheduling tokens installs cleanly
               # (design §2 P0, §12). The one-active-session /
               # idempotency / coalesce / sequence indexes match design §3 exactly.
               # `mesh_turn_revisions` is the append-only edit audit (design §3);
               # sessions.turn_queue_enrolled/paused/config_revision are the
               # per-session enrollment + queue-pause + activation-config markers.
        (35, """
            ALTER TABLE mesh_tasks ADD COLUMN intent_bytes INTEGER;
            ALTER TABLE mesh_tasks ADD COLUMN blocked_until TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN blocked_attempts INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE nodes ADD COLUMN managed_backends TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE mesh_tasks ADD COLUMN lineage_state TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN lineage_token TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN lineage_lease_until TEXT
        """),  # A82 Stage 4a: persisted stored-intent byte accounting for the
               # managed waiting budget (design §8) + blocked-head retry backoff
               # (design §5.2). NULL/0 on every legacy row. Unreleased (branch-only).
        (36, """
            ALTER TABLE mesh_tasks ADD COLUMN cancel_token TEXT;
            ALTER TABLE mesh_tasks ADD COLUMN cancel_requested_at TEXT;
            CREATE INDEX IF NOT EXISTS idx_mesh_turns_lineage_void
                ON mesh_tasks(created_at, id)
                WHERE queue_protocol = 1 AND lineage_state = 'void'
        """),  # A82 Stage 4b: operator cancel recorded against the attempt token
               # (cancel_token) + a small partial index over withdrawn rows whose
               # Case lineage still needs voiding. NULL on every legacy row.
        (37, """
            ALTER TABLE sessions ADD COLUMN turn_queue_hold TEXT
        """),  # A82 Stage 4b rework 2: durable operator-stop hold record
               # ('operator_stop' | NULL) — distinguishes "stopped by the operator"
               # from dead/crashed, for activation and Case automation.
        (38, """
            ALTER TABLE mesh_tasks ADD COLUMN producer_turn_id TEXT;
            CREATE INDEX IF NOT EXISTS idx_mesh_tasks_producer_link
                ON mesh_tasks(producer_turn_id)
                WHERE producer_turn_id IS NOT NULL AND status = 'claimed'
        """),  # A82 Stage 4c: durable producer-token → managed-turn link (a Case
               # continuation token names the deterministic protocol-1 turn it
               # admitted, in the admission txn). NULL on every legacy row; the
               # partial index holds only linked tokens awaiting finalization.
    ]


# Single source of truth for the current schema version: the highest migration.
_CURRENT_VERSION = max(v for v, _ in _get_migrations())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compose_completion_criteria(
    raw: Optional[str], round_cap: Optional[int],
) -> Optional[str]:
    """[M3.4/A52] The value to PERSIST in ``completion_criteria`` given an optional
    human criteria string and an optional continuation ``round_cap``.

    Without a positive ``round_cap`` the human ``raw`` is stored verbatim
    (byte-identical to the pre-A52 behaviour ⇒ the flag/feature is a no-op).
    With one, the two are folded into a single JSON object
    ``{"round_cap": N, "criteria": <raw>}`` — the object shape
    :func:`case_round_cap` already reads and :func:`_parse_completion_criteria`
    now unpacks — so no new column is introduced. A ``raw`` that is itself a JSON
    list/string is embedded as its parsed value so the array shape survives the
    round-trip. Pure; never raises."""
    if not isinstance(round_cap, int) or round_cap <= 0:
        return raw
    obj: Dict[str, Any] = {"round_cap": round_cap}
    if raw is not None and str(raw).strip():
        criteria_val: Any = str(raw)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (list, str)):
                criteria_val = parsed
        except Exception:
            pass
        obj["criteria"] = criteria_val
    return json.dumps(obj)


def _parse_completion_criteria(raw: Optional[str]) -> List[str]:
    """[A37] A Case's ``completion_criteria`` → a list of criterion strings.

    A JSON array yields its (non-blank) items; a JSON object (the [A52] dual-shape
    that also carries ``round_cap``) yields its ``criteria`` member unpacked the
    same way; any other non-blank value is a single-criterion list; None/blank
    yields an empty list (⇒ no criteria to reconcile). Pure; never raises."""
    if raw is None:
        return []
    try:
        v = json.loads(raw)
        if isinstance(v, dict):
            inner = v.get("criteria")
            if isinstance(inner, list):
                return [str(x).strip() for x in inner if str(x).strip()]
            if isinstance(inner, str):
                return [inner.strip()] if inner.strip() else []
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
    except Exception:
        pass
    s = str(raw).strip()
    return [s] if s else []


def _criterion_resolved(entry: Any) -> bool:
    """[A37] A reconciliation entry resolves its criterion iff it is recorded
    ``met``, or explicitly ``waived`` with a non-empty reason (mirrors the
    ``waived_findings`` honesty contract). Anything else is unresolved.

    [A39] Liberal-in-what-we-accept on the Decision surface: the canonical shape is
    ``{"status": "met"}`` / ``{"status": "waived", "reason": ...}``, but a Manager
    (or its LLM) commonly emits the boolean shorthand ``{"met": true}`` /
    ``{"waived": true, "reason": ...}``. Both are honored so a slightly-off format
    guess cannot produce a perpetually-unclosable Case. Safety is preserved:
    ``met`` must be exactly ``True`` (``{"met": false}`` does NOT resolve) and a
    waiver still requires a non-empty reason."""
    if not isinstance(entry, dict):
        return False
    status = str(entry.get("status") or "").strip().lower()
    if status == "met" or entry.get("met") is True:
        return True
    waived = status == "waived" or entry.get("waived") is True
    if waived and str(entry.get("reason") or "").strip():
        return True
    return False


def _unreconciled_criteria(
    raw: Optional[str], reconciliation: Optional[List[Dict[str, Any]]],
) -> List[str]:
    """[A37] The criterion strings NOT resolved by ``reconciliation``.

    Each reconciliation entry claims a criterion by its ``criterion`` text and is
    honored only when :func:`_criterion_resolved`. An empty list means every
    criterion is met/waived ⇒ the Case may close. Pure; never raises."""
    criteria = _parse_completion_criteria(raw)
    if not criteria:
        return []
    resolved = {
        str(e.get("criterion") or "").strip()
        for e in (reconciliation or [])
        if _criterion_resolved(e)
    }
    return [c for c in criteria if c not in resolved]


def _event_payload(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """[A46] Decode a flow_event's ``payload_json`` to a dict (or None). Pure; the
    row stores JSON as text, so a str is parsed and anything unparseable ⇒ None."""
    payload = event.get("payload_json")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _event_outcome(event: Dict[str, Any]) -> Optional[str]:
    """[A46] The ``outcome`` recorded in a flow_event's payload, or None."""
    pl = _event_payload(event)
    return str(pl["outcome"]) if isinstance(pl, dict) and pl.get("outcome") else None


_TURN_BLOCK_BACKOFF_BASE_SEC = 3.0
_TURN_BLOCK_BACKOFF_CAP_SEC = 300.0


def _apply_turn_block(conn: sqlite3.Connection, task_id: str, reason: str) -> bool:
    """[A82 Stage 4a rework] Inside an open write txn: record a bounded
    `blocked_reason`, bump `blocked_attempts` and set `blocked_until` =
    now + min(3 s * 2^(attempts-1), 300 s). Returns True if the reason changed."""
    row = conn.execute(
        "SELECT blocked_reason, blocked_attempts FROM mesh_tasks "
        "WHERE id = ? AND queue_protocol = 1 AND status = 'queued'",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    bounded = (reason or "blocked")[:500]
    attempts = int(row["blocked_attempts"] or 0) + 1
    delay = min(_TURN_BLOCK_BACKOFF_BASE_SEC * (2 ** min(attempts - 1, 16)),
                _TURN_BLOCK_BACKOFF_CAP_SEC)
    until = (datetime.now(tz=timezone.utc) + timedelta(seconds=delay)).isoformat()
    conn.execute(
        "UPDATE mesh_tasks SET blocked_reason = ?, blocked_attempts = ?, blocked_until = ?, "
        "updated_at = ? WHERE id = ? AND queue_protocol = 1 AND status = 'queued'",
        (bounded, attempts, until, _now(), task_id),
    )
    return row["blocked_reason"] != bounded


def _carrier_fresh_cutoff() -> str:
    """Oldest heartbeat that still counts as a live carrier."""
    try:
        from config import config as _cfg
        timeout = float(_cfg.mesh.node_heartbeat_timeout_sec)
    except Exception:
        timeout = 90.0
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=timeout)).isoformat()


def _iso_in(seconds: float) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=float(seconds))).isoformat()


def _canonical_admission_hash(request: Dict[str, Any]) -> str:
    """[A82 Stage 4a] Stable hash of the ORIGINAL admission request (design §4:
    target, body, attachments, relevant options). Canonical JSON (sorted keys,
    no whitespace variance) so a byte-identical retry hashes identically."""
    blob = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _turn_backing_error(op: str, err: Exception, **ctx: Any) -> BackingStoreError:
    """Wrap an unexpected DB failure inside a strict managed helper as a typed
    503 (design §6/§8: managed admission/consumption fails CLOSED). Logged once;
    the typed error is raised to the caller so no swallowed write returns as a
    silent success (the exact legacy anti-pattern the managed path avoids)."""
    logger.warning("event=db_turn_%s_failed err=%s ctx=%s", op, err, ctx)
    return BackingStoreError(f"managed {op} failed: {err}", op=op, **ctx)


def _is_quiescence_evidence(evidence: Any) -> bool:
    """A recorded authenticated quiescence observation is a dict carrying a
    terminal/stop signal bound to an execution attempt (design §6). A bare
    boolean, None, free text, or a mere offline label is NOT evidence.

    Minimum shape: a mapping with either a durable terminal `result`, or a
    `quiescent` marker accompanied by attempt-identifying + terminal fields
    (e.g. task/token/native execution identity + terminal/stop evidence)."""
    if not isinstance(evidence, dict):
        return False
    if evidence.get("result") is not None:
        return True
    if not evidence.get("quiescent"):
        return False
    # A quiescence claim must be backed by attempt identity + a terminal/stop
    # observation — not just `{"quiescent": true}` (which is a bare boolean in
    # disguise). Node offline/registration status alone is explicitly rejected.
    has_attempt = bool(
        evidence.get("claim_token")
        or evidence.get("native_session_id")
        or evidence.get("task_id")
    )
    has_terminal = bool(
        evidence.get("terminal")
        or evidence.get("stopped")
        or evidence.get("terminal_status")
    )
    return has_attempt and has_terminal


def _release_stop_hold(conn: sqlite3.Connection, session_id: str, now: str) -> None:
    """[A82 Stage 4b rework 2] An operator action releases the operator-stop
    hold inside the caller's transaction: clears the durable record and turns a
    `cancelled` status back to `idle` (any other live status is kept)."""
    conn.execute(
        "UPDATE sessions SET status = CASE WHEN status = 'cancelled' "
        "THEN 'idle' ELSE status END, turn_queue_hold = NULL, "
        "updated_at = ? WHERE session_id = ? "
        "AND (status = 'cancelled' OR turn_queue_hold IS NOT NULL)",
        (now, session_id),
    )


# [A82 Stage 4d] Session states in which OPTIONAL automation (a cache
# heartbeat) must not run: dead, operator-stopped, or errored.
_NOT_IDLE_SESSION_STATUSES = ("closed", "cancelled", "error")


def _session_idle_for_optional_turn(
    conn: sqlite3.Connection, session_id: str, exclude_turn_id: Optional[str] = None,
) -> bool:
    """[A82 Stage 4d] Idle-only eligibility of OPTIONAL automation (cache
    heartbeat, design §7) read from the ledger, never from BUSY/IDLE display
    (packet §3.5): the session exists, is not closed / held / paused /
    cancelled / errored, and has NO open work — no managed queued / pending /
    claimed / running / recovery_required turn and no legacy nonterminal row —
    other than ``exclude_turn_id`` (the heartbeat itself, at activation).
    Bounded: one PK read + two ``LIMIT 1`` probes (the managed one on the
    partial open-subset index)."""
    srow = conn.execute(
        "SELECT status, turn_queue_hold, turn_queue_paused FROM sessions "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if srow is None or (srow["status"] or "") in _NOT_IDLE_SESSION_STATUSES:
        return False
    if srow["turn_queue_hold"] or int(srow["turn_queue_paused"] or 0):
        return False
    managed = conn.execute(
        f"SELECT 1 FROM mesh_tasks INDEXED BY idx_mesh_turns_session_open "
        f"WHERE session_id = ? AND {_MANAGED_OPEN_PREDICATE} AND id IS NOT ? LIMIT 1",
        (session_id, exclude_turn_id),
    ).fetchone()
    if managed is not None:
        return False
    legacy = conn.execute(
        "SELECT 1 FROM mesh_tasks WHERE session_id = ? "
        "AND COALESCE(queue_protocol, 0) = 0 "
        "AND status IN ('pending', 'claimed', 'running') AND id IS NOT ? LIMIT 1",
        (session_id, exclude_turn_id),
    ).fetchone()
    return legacy is None


def _token_payload(raw: Any) -> Dict[str, Any]:
    """[A82 Stage 4c] Decoded producer-token payload ({} when absent/garbled)."""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        val = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _token_attempt(raw: Any) -> int:
    """[A82 Stage 4c] The token's durable attempt counter (>= 1)."""
    try:
        return max(1, int(_token_payload(raw).get("attempt") or 1))
    except (TypeError, ValueError):
        return 1


def _link_producer_token(
    conn: sqlite3.Connection,
    token_id: str,
    turn_id: str,
    turn_status: str,
    meta: Optional[Dict[str, Any]],
    now: str,
) -> None:
    """[A82 Stage 4c] Link a Case continuation token to its managed turn INSIDE
    the admission transaction (design §7: "in one transaction link token/trigger
    to its deterministic protocol-1 turn"). Converges when already linked to the
    same turn; raises (rolling the admission back) when the token is missing,
    already finalized, linked elsewhere, or the turn is no longer open. The
    linked token is `claimed` with ``claimed_at`` NULL: its owner is the linked
    turn, so the legacy lease reaper (claimed_at IS NOT NULL) never re-offers
    it."""
    from .turn_queue import OPEN_STATUSES

    row = conn.execute(
        "SELECT status, queue_protocol, action, payload, producer_turn_id "
        "FROM mesh_tasks WHERE id = ?",
        (token_id,),
    ).fetchone()
    if (
        row is None or int(row["queue_protocol"] or 0) != 0
        or row["action"] not in PRODUCER_TOKEN_SENTINELS
    ):
        raise TurnNotFoundError("no producer token to link", task_id=token_id)
    if row["producer_turn_id"] == turn_id:
        return
    if row["producer_turn_id"] is not None or row["status"] != "pending":
        raise OwnershipConflictError(
            "producer token already linked or finalized", task_id=token_id,
            linked=row["producer_turn_id"], status=row["status"],
        )
    if turn_status not in OPEN_STATUSES:
        raise OwnershipConflictError(
            "producer token cannot link a terminal turn", task_id=turn_id,
            status=turn_status,
        )
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, ValueError):
        payload = {}
    payload.update(meta or {})
    payload["turn_id"] = turn_id
    conn.execute(
        """
        UPDATE mesh_tasks
        SET status = 'claimed', claimed_by = ?, claimed_at = NULL,
            claimer_incarnation = NULL, producer_turn_id = ?, payload = ?,
            updated_at = ?
        WHERE id = ? AND status = 'pending' AND producer_turn_id IS NULL
          AND COALESCE(queue_protocol, 0) = 0
        """,
        (PRODUCER_TOKEN_SENTINELS[row["action"]], turn_id, json.dumps(payload), now, token_id),
    )
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        raise OwnershipConflictError("producer token link lost the race", task_id=token_id)


def _cancel_requested_for(row: Any, claim_token: str) -> bool:
    """[A82 Stage 4b] True iff an operator cancel was recorded against the
    attempt presenting ``claim_token`` (cancel_token is set from the claim token
    at request time, so a superseded attempt never inherits it)."""
    import hmac

    recorded = row["cancel_token"] if "cancel_token" in row.keys() else None
    return bool(recorded) and hmac.compare_digest(str(recorded), str(claim_token or ""))


def _now() -> str:
    # Always produce a timezone-aware UTC string so the browser can correctly
    # convert to local time. datetime.utcnow() produced naive strings that JS
    # treated as local time, causing a 3-hour clock skew vs telemetry timestamps
    # (which are always UTC-aware).
    return datetime.now(tz=timezone.utc).isoformat()


def _origin_json(origin: Any) -> str:
    """Serialize a SessionOrigin (or None) to the stored {channel, kind} JSON.

    db.py stays free of core imports, so this reads attributes duck-typed and
    falls back to the telegram/user default when origin is missing.
    """
    channel = getattr(origin, "channel", None) or "telegram"
    kind = getattr(origin, "kind", None) or "user"
    return json.dumps({"channel": channel, "kind": kind})


# A node not seen within this window is decommissioned inventory (e.g. old
# test/canary rows never pruned), not part of the current fleet. Excluding it
# keeps "nodes online N/M" honest instead of counting long-dead ghosts.
_NODE_FLEET_RETENTION_SEC = 2 * 86400


def _is_gateway_self_node(row: Dict[str, Any]) -> bool:
    """True for the gateway's OWN hostname self-registration.

    ``task_server._register_local_node()`` registers the gateway host under
    ``socket.gethostname()`` with *empty backends* and *no tailscale IP* (api_port
    = dashboard_port) purely so the gateway's in-process self-claims stay 'live'
    and aren't reaped as ``node_offline``. It is infrastructure plumbing, not a
    selectable worker, so it must not appear in operator-facing node listings, the
    session machine picker, or mesh-health counters. This predicate is
    presentation-only: the self-claim mechanism still sees the row via
    ``list_nodes()`` (registration/heartbeat/reaping are untouched).

    Identified precisely (never a real worker): node_id == this host's name AND
    empty backends AND empty tailscale_ip. A real worker always advertises at
    least one backend and a tailscale IP; dead canary/test rows carry a different
    node_id, so they are never matched here. Handles both the raw-DB shape
    (backends as a JSON string) and the registry shape (backends as a list).
    """
    try:
        if (row.get("node_id") or "") != socket.gethostname():
            return False
    except Exception:
        return False
    backends = row.get("backends")
    backends_empty = (
        backends is None
        or (isinstance(backends, str) and backends.strip() in ("", "[]"))
        or (isinstance(backends, (list, tuple)) and len(backends) == 0)
    )
    ip = row.get("tailscale_ip") or ""
    ip_empty = not (ip.strip() if isinstance(ip, str) else ip)
    return backends_empty and ip_empty


def _count_fleet_nodes(nodes: List[Dict[str, Any]]) -> int:
    """Count nodes that belong to the *current* fleet: online, or offline but
    heartbeated within the retention window. Long-dead inventory is excluded."""
    now = datetime.now(timezone.utc)
    count = 0
    for row in nodes:
        if row.get("status") == "online":
            count += 1
            continue
        hb = row.get("last_heartbeat")
        if not hb:
            continue
        try:
            ts = datetime.fromisoformat(str(hb))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - ts).total_seconds() <= _NODE_FLEET_RETENTION_SEC:
            count += 1
    return count


def _mesh_load_stats(nodes: List[Dict[str, Any]], stale_busy: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Aggregate node live_state blobs into network-wide slot/task counters.

    Only ``online`` nodes contribute. An offline node holds no live capacity and
    is not "heartbeating but scheduler-invisible" — it is simply gone. Counting
    offline nodes here previously (a) inflated ``slots_total`` with dead-node
    capacity and (b) reported long-dead inventory in ``nodes_without_live_state``
    as if it were live-but-silent, producing the misleading
    "N nodes heartbeating but scheduler-invisible" banner.
    """
    slots_used = 0
    slots_total = 0
    active_tasks = 0
    nodes_with_state = 0
    nodes_without_state = 0
    stale_state_nodes: List[str] = []

    # tz-aware "now": live_state_updated_at is written tz-aware (+00:00) by the
    # registry heartbeat, so subtracting it from a naive utcnow() raised
    # TypeError — caught below — which silently marked EVERY fresh online node
    # stale (zeroing slots and dropping active tasks). Compare aware-to-aware.
    now = datetime.now(timezone.utc)
    _live_state_max_age_s = 120
    for row in nodes:
        # Offline nodes are inventory, not live mesh — skip them entirely.
        if row.get("status") != "online":
            continue

        live_raw = row.get("live_state")
        live: Dict[str, Any] = {}
        if isinstance(live_raw, dict):
            live = live_raw
        elif isinstance(live_raw, str) and live_raw.strip():
            try:
                parsed = json.loads(live_raw)
                if isinstance(parsed, dict):
                    live = parsed
            except Exception:
                live = {}

        # Check staleness before aggregating — stale live_state must not
        # contribute phantom slot/task counts to the mesh totals.
        live_is_fresh = False
        updated = row.get("live_state_updated_at")
        if live and updated:
            try:
                parsed_ts = datetime.fromisoformat(str(updated))
                if parsed_ts.tzinfo is None:
                    # Legacy naive timestamps are assumed UTC (that's how they
                    # were written before the registry moved to tz-aware).
                    parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                age_s = (now - parsed_ts).total_seconds()
                live_is_fresh = age_s <= _live_state_max_age_s
            except Exception:
                live_is_fresh = False
        elif live and not updated:
            live_is_fresh = False  # live_state present but timestamp missing — treat as stale

        if live and live_is_fresh:
            nodes_with_state += 1
            try:
                slots_used += int(live.get("slots_used") or 0)
            except Exception:
                pass
            try:
                slots_total += int(live.get("slots_total") or row.get("max_concurrent") or 0)
            except Exception:
                pass
            tasks = live.get("active_tasks")
            if isinstance(tasks, list):
                active_tasks += len(tasks)
        else:
            nodes_without_state += 1
            try:
                slots_total += int(row.get("max_concurrent") or 0)
            except Exception:
                pass

        # Reached only for online nodes (offline skipped above): an online node
        # with missing/stale live_state is genuinely reporting-silent.
        if not updated or not live_is_fresh:
            stale_state_nodes.append(row.get("node_id", ""))

    return {
        "slots_used": slots_used,
        "slots_total": slots_total,
        "slots_available": max(slots_total - slots_used, 0),
        "active_tasks": active_tasks,
        "nodes_with_live_state": nodes_with_state,
        "nodes_without_live_state": nodes_without_state,
        "stale_live_state_nodes": [n for n in stale_state_nodes if n],
        "stale_busy_sessions": len(stale_busy or []),
    }


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------

_db_instance: Optional[MeshDB] = None
_db_lock = threading.Lock()


def get_db() -> Optional[MeshDB]:
    """Return the singleton MeshDB if shadow_write is enabled, else None.

    The first call initialises the DB.  Subsequent calls return the cached
    instance.  Returns None when mesh.shadow_write is False so callers can
    guard with a simple `if db:` check.
    """
    global _db_instance
    if _db_instance is not None:
        return _db_instance
    with _db_lock:
        if _db_instance is not None:
            return _db_instance
        try:
            from config import config as _cfg
            if not _cfg.mesh.shadow_write:
                return None
            project_root = Path(__file__).resolve().parent.parent.parent
            db_path = Path(_cfg.mesh.db_path)
            if not db_path.is_absolute():
                db_path = project_root / db_path
            _db_instance = MeshDB(str(db_path))
            logger.info("event=mesh_db_ready path=%s", db_path)
        except Exception as e:
            logger.warning("event=mesh_db_init_failed err=%s — shadow writes disabled", e)
            return None
    return _db_instance
