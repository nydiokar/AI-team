"""M4 — workflow event service tests (no network, no paid backend).

Verifies the reserved vocabulary (CONTROL_CONTRACT §7) is emitted with correct
names + correlation, that validation rejects bad inputs with stable reason
codes, and that the service mutates no state (no engine, no tables).
"""
import pytest

from src.core import observability
from src.services.workflow_service import (
    WorkflowService, WORKFLOW_EVENTS,
    EVENT_REVIEW_REQUESTED, EVENT_REVIEW_COMPLETED, EVENT_HANDOFF_CREATED,
    EVENT_APPROVAL_REQUESTED, EVENT_APPROVAL_GRANTED,
)
from src.services.session_service import CommandResult


@pytest.fixture
def events_file(monkeypatch, tmp_path):
    monkeypatch.setattr(observability, "_LOGS_DIR", tmp_path)
    return tmp_path / "events.ndjson"


@pytest.fixture
def svc():
    return WorkflowService()


def _events(file):
    return observability.read_recent_events(limit=1000)["events"]


# --- happy path: each method emits its reserved event -----------------------

def test_review_requested_emits(svc, events_file):
    res = svc.review_requested(session_id="s1", reviewer="alice", note="please look")
    assert isinstance(res, CommandResult) and res.ok
    ev = _events(events_file)[-1]
    assert ev["event"] == EVENT_REVIEW_REQUESTED
    assert ev["session_id"] == "s1"
    assert ev["reviewer"] == "alice"


def test_review_completed_emits_with_verdict(svc, events_file):
    res = svc.review_completed(session_id="s1", verdict="approved", task_id="t9")
    assert res.ok
    ev = _events(events_file)[-1]
    assert ev["event"] == EVENT_REVIEW_COMPLETED
    assert ev["verdict"] == "approved"
    assert ev["task_id"] == "t9"


def test_handoff_created_emits(svc, events_file):
    res = svc.handoff_created(session_id="s1", to="LP-2", reason="needs gpu")
    assert res.ok
    ev = _events(events_file)[-1]
    assert ev["event"] == EVENT_HANDOFF_CREATED
    assert ev["to"] == "LP-2"


def test_approval_requested_emits(svc, events_file):
    res = svc.approval_requested(session_id="s1", action="deploy", requested_by="bob")
    assert res.ok
    ev = _events(events_file)[-1]
    assert ev["event"] == EVENT_APPROVAL_REQUESTED
    assert ev["action"] == "deploy"


def test_approval_granted_emits(svc, events_file):
    res = svc.approval_granted(session_id="s1", action="deploy", approver="carol")
    assert res.ok
    ev = _events(events_file)[-1]
    assert ev["event"] == EVENT_APPROVAL_GRANTED
    assert ev["granted"] is True


def test_approval_denied_uses_same_event_with_granted_false(svc, events_file):
    res = svc.approval_granted(session_id="s1", action="deploy", granted=False)
    assert res.ok
    ev = _events(events_file)[-1]
    assert ev["event"] == EVENT_APPROVAL_GRANTED
    assert ev["granted"] is False


# --- validation: stable reason codes, nothing emitted on reject -------------

def test_missing_session_id_rejected(svc, events_file):
    res = svc.review_requested(session_id="")
    assert not res.ok and res.reason == "missing_session_id"
    assert _events(events_file) == []  # nothing emitted


def test_invalid_verdict_rejected(svc, events_file):
    res = svc.review_completed(session_id="s1", verdict="lgtm")
    assert not res.ok and res.reason == "invalid_verdict"
    assert _events(events_file) == []


def test_handoff_requires_target(svc, events_file):
    res = svc.handoff_created(session_id="s1", to="")
    assert not res.ok and res.reason == "missing_handoff_target"


def test_approval_requires_action(svc, events_file):
    assert svc.approval_requested(session_id="s1", action="").reason == "missing_action"
    assert svc.approval_granted(session_id="s1", action="").reason == "missing_action"


# --- vocabulary integrity ---------------------------------------------------

def test_reserved_vocabulary_is_exactly_the_contract_set():
    assert WORKFLOW_EVENTS == {
        "review.requested", "review.completed",
        "handoff.created",
        "approval.requested", "approval.granted",
    }


# --- wired into the orchestrator -------------------------------------------

def test_orchestrator_exposes_workflow_service():
    from src.orchestrator import TaskOrchestrator
    orch = TaskOrchestrator()
    assert isinstance(orch.workflow_service, WorkflowService)
    # Shares no store / holds no state — stateless by construction.
    assert not hasattr(orch.workflow_service, "store")
