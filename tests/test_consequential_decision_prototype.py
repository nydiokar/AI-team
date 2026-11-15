"""Offline consumer proof. No gateway, worker, model, network or production files."""

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from scripts.prototypes.consequential_decision import (
    Blocked,
    Explanation,
    PaletteConsumer,
    Policy,
    Principal,
    Resolution,
    State,
    contrast,
)


@pytest.fixture
def pilot(tmp_path) -> tuple[PaletteConsumer, Principal, Principal, Principal, list[float]]:
    owner = Principal(id="owner", kind="human")
    agent = Principal(id="manager", kind="agent")
    reviewer = Principal(id="reviewer", kind="agent")
    clock = [1000.0]
    consumer = PaletteConsumer(tmp_path / "pilot.db", (owner, agent, reviewer), lambda: clock[0])
    consumer.initialize(State(policy=Policy(), accent="#000080"))
    return consumer, owner, agent, reviewer, clock


def explanation() -> Explanation:
    return Explanation(
        effects="Accent is the background of buttons carrying white text.",
        alternatives=(
            "#000080: dark navy, readable white text",
            "#777777: neutral gray, white contrast below 4.5",
        ),
        uncertainty="Contrast does not establish brand suitability.",
    )


def proposed(pilot, actor=None) -> str:
    consumer, _, agent, _, _ = pilot
    return consumer.propose(actor or agent, explanation(), 2000)


def approved(pilot, evaluate=True) -> str:
    consumer, owner, *_ = pilot
    revision = proposed(pilot)
    consumer.resolve(owner, Resolution(revision=revision, decision="approved"))
    if evaluate:
        consumer.evaluate(revision)
    return revision


def rows(consumer, table) -> list[tuple[object, ...]]:
    with sqlite3.connect(consumer.path) as db:
        return db.execute(f"SELECT * FROM {table}").fetchall()


def state_of(consumer) -> State:
    with consumer.transaction() as db:
        return consumer.state(db)


def change(pilot, **updates) -> None:
    consumer, owner, *_ = pilot
    state = state_of(consumer).model_dump()
    state.update(updates)
    consumer.configure(owner, State.model_validate(state))


def test_baseline_inherited_default_publishes_without_decision(tmp_path) -> None:
    """Executable FM-1 counterexample: the pre-control fixture consumer."""
    with sqlite3.connect(tmp_path / "baseline.db") as db:
        db.execute("CREATE TABLE artifacts (css TEXT NOT NULL)")
        config: dict[str, str] = {}
        value = config.get("accent", "#777777")
        db.execute("INSERT INTO artifacts VALUES (?)", (f":root {{ --accent: {value}; }}",))
    with sqlite3.connect(tmp_path / "baseline.db") as reopened:
        assert reopened.execute("SELECT css FROM artifacts").fetchone()[0] == (
            ":root { --accent: #777777; }"
        )


def test_required_default_has_no_consumer_effect(pilot) -> None:
    consumer, _, agent, *_ = pilot
    change(pilot, accent=None)
    with pytest.raises(Blocked, match="accent.*required decision"):
        consumer.publish(agent, "blocked")
    assert rows(consumer, "artifacts") == []


def test_exact_approved_snapshot_and_packet_are_consumed_after_reopen(pilot) -> None:
    consumer, owner, agent, reviewer, clock = pilot
    revision = approved(pilot)
    packet = json.loads(consumer.render(revision))
    assert packet["value"] == "#000080"
    assert packet["source"] == "project"
    assert packet["explanation"] == explanation().model_dump(mode="json")
    assert packet["objective"] and packet["policy"]["evaluation"] == "contrast-1"
    reopened = PaletteConsumer(consumer.path, (owner, agent, reviewer), lambda: clock[0])
    result = json.loads(reopened.publish(agent, "publish", revision=revision))
    assert result == {
        "mode": "authoritative",
        "revision": revision,
        "value": packet["value"],
        "css": ":root { --accent: #000080; }",
    }
    assert rows(consumer, "artifacts")[0][3] == json.dumps(result, sort_keys=True)
    assert rows(consumer, "decisions")[0][1:3] == ("approved", "owner")


@pytest.mark.parametrize(
    "field,value",
    [
        ("accent", "#003300"),
        ("accent", None),
        ("objective", "New objective"),
        ("schema_revision", "palette-2"),
        ("evidence_revision", "black-text-2"),
        ("policy", Policy(revision="policy-2")),
        ("policy", Policy(evaluation="na", na_reason="Owner-only brand choice")),
    ],
)
def test_material_changes_cannot_publish(pilot, field, value) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot)
    change(pilot, **{field: value})
    with pytest.raises(Blocked, match="stale"):
        consumer.publish(agent, "new", revision=revision)
    assert rows(consumer, "artifacts") == []
    assert len(rows(consumer, "evaluations")) == 1  # historical result is retained


def test_unrelated_change_does_not_reapprove(pilot) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot)
    change(pilot, routine_note="new development note")
    consumer.publish(agent, "ordinary", revision=revision)
    assert len(rows(consumer, "artifacts")) == 1


@pytest.mark.parametrize("decision", ["rejected", "cancelled"])
def test_terminal_nonapproval_never_certifies(pilot, decision) -> None:
    consumer, owner, agent, *_ = pilot
    revision = proposed(pilot)
    consumer.resolve(owner, Resolution(revision=revision, decision=decision))
    consumer.evaluate(revision)
    with pytest.raises(Blocked, match="missing/rejected/cancelled"):
        consumer.publish(agent, "no", revision=revision)
    assert not rows(consumer, "artifacts")


def test_approval_and_evaluation_are_separate(pilot) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot, evaluate=False)
    assert not rows(consumer, "evaluations")
    with pytest.raises(Blocked, match="evaluation missing"):
        consumer.publish(agent, "no", revision=revision)
    assert not rows(consumer, "artifacts")
    assert consumer.evaluate(revision)["status"] == "passed"
    consumer.publish(agent, "yes", revision=revision)


def test_inherited_gray_fails_actual_contrast_evaluation(pilot) -> None:
    consumer, _, agent, *_ = pilot
    change(pilot, accent=None)
    revision = approved(pilot)
    assert json.loads(consumer.render(revision))["source"] == "default"
    assert 4.4 < contrast("#777777") < 4.5
    assert json.loads(rows(consumer, "evaluations")[0][1])["status"] == "failed"
    with pytest.raises(Blocked, match="evaluation missing/failed"):
        consumer.publish(agent, "gray", revision=revision)
    assert not rows(consumer, "artifacts")


def test_explicit_na_and_reason(pilot) -> None:
    consumer, _, agent, *_ = pilot
    change(pilot, policy=Policy(evaluation="na", na_reason="Brand judgment has no local evaluator"))
    revision = approved(pilot)
    assert consumer.evaluate(revision) == {
        "status": "na",
        "reason": "Brand judgment has no local evaluator",
    }
    consumer.publish(agent, "na", revision=revision)


def test_na_without_reason_blocks(pilot) -> None:
    consumer, *_ = pilot
    change(pilot, policy=Policy(evaluation="na"))
    with pytest.raises(Blocked, match="N/A requires"):
        proposed(pilot)
    assert not rows(consumer, "proposals")


def test_spoofed_identity_and_agent_self_approval(pilot) -> None:
    consumer, owner, agent, *_ = pilot
    revision = proposed(pilot)
    resolution = Resolution(revision=revision, decision="approved")
    with pytest.raises(Blocked, match="untrusted principal"):
        consumer.resolve(Principal(id="owner", kind="human"), resolution)
    with pytest.raises(ValidationError):
        Resolution(revision=revision, decision="approved", resolved_by="owner")
    grant = consumer.delegate(owner, agent, revision, 1900)
    with pytest.raises(Blocked, match="own proposal"):
        consumer.resolve(agent, resolution, grant)
    assert not rows(consumer, "decisions")
    with pytest.raises(Blocked):
        consumer.publish(agent, "no", revision=revision)
    assert not rows(consumer, "artifacts")


@pytest.mark.parametrize("invalidity", ["revoked", "expired", "wrong_scope", "unknown"])
def test_delegation_is_current_scoped_and_cannot_be_resurrected(pilot, invalidity) -> None:
    consumer, owner, agent, reviewer, clock = pilot
    revision = proposed(pilot)
    grant = consumer.delegate(owner, reviewer, revision, 1500)
    consumer.resolve(reviewer, Resolution(revision=revision, decision="approved"), grant)
    consumer.evaluate(revision)
    if invalidity == "revoked":
        consumer.revoke(owner, grant)
        consumer.delegate(owner, reviewer, revision, 1900)  # cannot resurrect old approval
    elif invalidity == "expired":
        clock[0] = 1500
    elif invalidity == "wrong_scope":
        change(pilot, accent="#003300")
    else:
        consumer.principals = (owner, agent)  # authority no longer exists on host
    with pytest.raises(Blocked):
        consumer.publish(agent, "no", revision=revision)
    assert not rows(consumer, "artifacts")


def test_valid_delegation_and_human_proposer(pilot) -> None:
    consumer, owner, agent, reviewer, _ = pilot
    revision = proposed(pilot)
    grant = consumer.delegate(owner, reviewer, revision, 1900)
    consumer.resolve(reviewer, Resolution(revision=revision, decision="approved"), grant)
    consumer.evaluate(revision)
    consumer.publish(agent, "delegated", revision=revision)
    own = proposed(pilot, owner)
    consumer.resolve(owner, Resolution(revision=own, decision="approved"))
    consumer.evaluate(own)
    consumer.publish(agent, "human", revision=own)


def test_timeout_does_not_approve(pilot) -> None:
    consumer, _, agent, _, clock = pilot
    revision = approved(pilot)
    clock[0] = 2000
    with pytest.raises(Blocked, match="expired"):
        consumer.publish(agent, "no", revision=revision)
    assert not rows(consumer, "artifacts")


def test_sandbox_runs_without_approval_and_cannot_be_promoted(pilot) -> None:
    consumer, _, agent, *_ = pilot
    result = json.loads(consumer.publish(agent, "experiment", sandbox=True))
    assert result["mode"] == "provisional" and result["revision"] is None
    with pytest.raises(Blocked, match="promotion forbidden"):
        consumer.publish(agent, "experiment")
    with pytest.raises(Blocked):
        consumer.publish(agent, "promoted", revision="experiment")
    with pytest.raises(TypeError):
        consumer.publish(agent, "promoted", artifact=result)
    assert [row[1] for row in rows(consumer, "artifacts")] == ["provisional"]


def test_explicit_optout_keeps_original_path(tmp_path, pilot) -> None:
    _, owner, agent, *_ = pilot
    consumer = PaletteConsumer(tmp_path / "unconfigured.db", (owner, agent))
    consumer.initialize(State(policy=None))
    result = json.loads(consumer.publish(agent, "normal"))
    assert result["css"] == ":root { --accent: #777777; }"
    assert not rows(consumer, "decisions") and not rows(consumer, "evaluations")


@pytest.mark.parametrize("bad", ["not json", "{}", '{"policy": {"setting": "unknown"}}'])
def test_policy_load_errors_fail_closed_even_in_sandbox(pilot, bad) -> None:
    consumer, _, agent, *_ = pilot
    with sqlite3.connect(consumer.path) as db:
        db.execute("UPDATE config SET body=?", (bad,))
    for sandbox in (False, True):
        with pytest.raises(ValidationError):
            consumer.publish(agent, "no", sandbox=sandbox)
    assert not rows(consumer, "artifacts")


def test_missing_store_and_missing_policy_do_not_optout(pilot) -> None:
    consumer, _, agent, *_ = pilot
    with sqlite3.connect(consumer.path) as db:
        db.execute("DELETE FROM config")
    with pytest.raises(Blocked, match="unavailable"):
        consumer.publish(agent, "no")
    assert not rows(consumer, "artifacts")
    missing = PaletteConsumer(consumer.path.parent / "absent.db", consumer.principals)
    with pytest.raises(sqlite3.OperationalError):
        missing.publish(agent, "no")


def test_policy_cannot_be_disabled_by_task_or_agent(pilot) -> None:
    consumer, owner, agent, *_ = pilot
    with pytest.raises(Blocked, match="owner required"):
        consumer.configure(agent, State(policy=None))
    with pytest.raises(Blocked, match="cannot be removed"):
        consumer.configure(owner, State(policy=None))
    with pytest.raises(TypeError):
        consumer.publish(agent, "no", policy=None)
    assert not rows(consumer, "artifacts")


def test_missing_explanation_unknown_fields_and_injection_are_rejected(pilot) -> None:
    consumer, *_ = pilot
    with pytest.raises(ValidationError):
        Explanation(effects=" ", alternatives=("one",), uncertainty="?")
    with pytest.raises(ValidationError):
        State(policy=Policy(), accent="#000000; body {display:none}")
    with pytest.raises(ValidationError):
        Policy(setting="loss_weight")
    assert not rows(consumer, "proposals") and not rows(consumer, "artifacts")


def test_duplicate_proposal_resolution_and_consumer_are_idempotent(pilot) -> None:
    consumer, owner, agent, *_ = pilot
    revision = approved(pilot)
    assert proposed(pilot) == revision
    consumer.resolve(owner, Resolution(revision=revision, decision="approved"))
    first = consumer.publish(agent, "same", revision=revision)
    assert consumer.publish(agent, "same", revision=revision) == first
    assert len(rows(consumer, "proposals")) == len(rows(consumer, "decisions")) == 1
    assert len(rows(consumer, "artifacts")) == 1
    change(pilot, accent="#003300")
    # Receipt retrieval is not a new effect; original revision/value stays inspectable.
    assert consumer.publish(agent, "same", revision=revision) == first
    with pytest.raises(Blocked, match="operation conflict"):
        consumer.publish(agent, "same", revision=proposed(pilot))


def test_concurrent_conflicting_decisions_have_one_winner(pilot) -> None:
    consumer, owner, agent, *_ = pilot
    revision = proposed(pilot)
    barrier = Barrier(2)

    def decide(outcome) -> str:
        barrier.wait(timeout=5)
        try:
            consumer.resolve(owner, Resolution(revision=revision, decision=outcome))
            return outcome
        except Blocked:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ("approved", "rejected")))
    assert results.count("conflict") == 1
    assert len(rows(consumer, "decisions")) == 1
    consumer.evaluate(revision)
    if rows(consumer, "decisions")[0][1] == "approved":
        consumer.publish(agent, "effect", revision=revision)
        assert len(rows(consumer, "artifacts")) == 1
    else:
        with pytest.raises(Blocked):
            consumer.publish(agent, "effect", revision=revision)
        assert not rows(consumer, "artifacts")


@pytest.mark.parametrize("timing", ["BEFORE", "AFTER"])
@pytest.mark.parametrize(
    "table,action",
    [
        ("proposals", "propose"),
        ("decisions", "resolve"),
        ("evaluations", "evaluate"),
        ("artifacts", "publish"),
    ],
)
def test_storage_write_failure_is_never_success_and_recovers(pilot, table, action, timing) -> None:
    consumer, owner, agent, *_ = pilot
    revision = None if action == "propose" else proposed(pilot)
    if action in ("evaluate", "publish"):
        consumer.resolve(owner, Resolution(revision=revision, decision="approved"))
    if action == "publish":
        consumer.evaluate(revision)
    with sqlite3.connect(consumer.path) as db:
        db.execute(
            f"CREATE TRIGGER fail {timing} INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
        )

    def attempt() -> object:
        if action == "propose":
            return proposed(pilot)
        if action == "resolve":
            return consumer.resolve(owner, Resolution(revision=revision, decision="approved"))
        if action == "evaluate":
            return consumer.evaluate(revision)
        return consumer.publish(agent, "effect", revision=revision)

    with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
        attempt()
    assert not rows(consumer, table)
    assert not rows(consumer, "artifacts")
    with sqlite3.connect(consumer.path) as db:
        db.execute("DROP TRIGGER fail")
    attempt()
    assert len(rows(consumer, table)) == 1


def test_no_change_between_check_and_use(pilot, monkeypatch) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot)
    original = consumer.authority

    def race(db, packet, actor, grant) -> None:
        original(db, packet, actor, grant)
        with (
            sqlite3.connect(consumer.path, timeout=0) as competing,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            competing.execute("UPDATE config SET body='{}'")

    monkeypatch.setattr(consumer, "authority", race)
    result = json.loads(consumer.publish(agent, "effect", revision=revision))
    assert result["value"] == "#000080"
    assert rows(consumer, "artifacts")[0][2] == revision


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_real_process_death_during_publication_recovers(pilot, stage) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot)
    program = """
import os, sys
from pathlib import Path
from contextlib import contextmanager
from scripts.prototypes.consequential_decision import PaletteConsumer, Principal
owner = Principal(id="owner", kind="human")
agent = Principal(id="manager", kind="agent")
c = PaletteConsumer(Path(sys.argv[1]), (owner, agent), lambda: 1000)
original = c.transaction
@contextmanager
def crashing():
    with original() as db:
        yield db
        os._exit(17)
if sys.argv[3] == "before_commit":
    c.transaction = crashing
c.publish(agent, "crash", revision=sys.argv[2])
os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(consumer.path), revision, stage],
        env={**os.environ, "AI_TEAM_TEST_MODE": "1"},
        timeout=10,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 17, result.stderr.decode()
    assert len(rows(consumer, "artifacts")) == (stage == "after_commit")
    consumer.publish(agent, "crash", revision=revision)
    consumer.publish(agent, "crash", revision=revision)
    assert len(rows(consumer, "decisions")) == len(rows(consumer, "artifacts")) == 1


def test_reverting_a_value_does_not_resurrect_approval(pilot) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot)
    change(pilot, accent="#003300")
    change(pilot, accent="#000080")
    with pytest.raises(Blocked, match="stale"):
        consumer.publish(agent, "reverted", revision=revision)
    assert not rows(consumer, "artifacts")
    new_revision = approved(pilot)
    assert new_revision != revision
    consumer.publish(agent, "new", revision=new_revision)


def test_commit_failure_rolls_back_actual_artifact_and_retry_recovers(pilot) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot)
    with sqlite3.connect(consumer.path) as db:
        db.executescript("""
            CREATE TABLE commit_guard (
                ref INTEGER REFERENCES config(id) DEFERRABLE INITIALLY DEFERRED);
            CREATE TRIGGER commit_fail AFTER INSERT ON artifacts BEGIN
                INSERT INTO commit_guard VALUES (999);
            END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        consumer.publish(agent, "effect", revision=revision)
    assert not rows(consumer, "artifacts") and not rows(consumer, "commit_guard")
    with sqlite3.connect(consumer.path) as db:
        db.execute("DROP TRIGGER commit_fail")
    consumer.publish(agent, "effect", revision=revision)
    assert len(rows(consumer, "artifacts")) == 1


def test_proposal_corruption_cannot_substitute_an_approved_value(pilot) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot)
    packet = json.loads(consumer.render(revision))
    packet["value"] = "#003300"
    with sqlite3.connect(consumer.path) as db:
        db.execute("UPDATE proposals SET body=? WHERE revision=?", (json.dumps(packet), revision))
    with pytest.raises(Blocked, match="corrupt proposal"):
        consumer.publish(agent, "substitute", revision=revision)
    assert not rows(consumer, "artifacts")


def test_concurrent_retries_produce_one_persisted_effect(pilot) -> None:
    consumer, _, agent, *_ = pilot
    revision = approved(pilot)
    barrier = Barrier(2)

    def publish(_) -> str:
        barrier.wait(timeout=5)
        return consumer.publish(agent, "same", revision=revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert results[0] == results[1]
    assert len(rows(consumer, "artifacts")) == 1


def test_explanation_change_requires_its_own_decision(pilot) -> None:
    consumer, _, agent, *_ = pilot
    original = approved(pilot)
    changed = explanation().model_copy(update={"effects": "New effects disclosed"})
    revision = consumer.propose(agent, changed, 2000)
    assert revision != original
    consumer.evaluate(revision)
    with pytest.raises(Blocked, match="approval missing"):
        consumer.publish(agent, "changed-explanation", revision=revision)
    assert not rows(consumer, "artifacts")


def test_grant_cannot_approve_another_current_packet(pilot) -> None:
    consumer, owner, agent, reviewer, _ = pilot
    original = proposed(pilot)
    grant = consumer.delegate(owner, reviewer, original, 1900)
    changed = explanation().model_copy(update={"uncertainty": "Additional uncertainty disclosed"})
    revision = consumer.propose(agent, changed, 2000)
    with pytest.raises(Blocked, match="scoped delegation"):
        consumer.resolve(reviewer, Resolution(revision=revision, decision="approved"), grant)
    with pytest.raises(Blocked, match="scoped delegation"):
        consumer.resolve(reviewer, Resolution(revision=original, decision="approved"))
    assert not rows(consumer, "decisions")
    consumer.evaluate(revision)
    with pytest.raises(Blocked, match="approval missing"):
        consumer.publish(agent, "wrong-grant", revision=revision)
    assert not rows(consumer, "artifacts")
