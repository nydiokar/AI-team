"""A113 executable simulation, deliberately NOT imported by the gateway.

One trusted host, one project, one setting and one SQLite consumer effect. Principals
are host-injected capabilities, not HTTP authentication. Direct DB/file access is
trusted. See docs/harness/consequential_decision_control.md for the proof boundary.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Color = Annotated[str, StringConstraints(pattern=r"^#[0-9a-f]{6}$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Principal(Record):
    id: Text
    kind: Literal["human", "agent"]


class Policy(Record):
    setting: Literal["accent"] = "accent"
    revision: Text = "policy-1"
    owner: Literal["owner"] = "owner"
    evaluation: Literal["contrast-1", "na"] = "contrast-1"
    na_reason: Text | None = None


class State(Record):
    # Required null means explicitly unconfigured; unreadable/missing is NOT null.
    policy: Policy | None
    accent: Color | None = None
    objective: Text = "Readable white button text on the accent background"
    schema_revision: Text = "palette-1"
    evidence_revision: Text = "white-text-1"
    routine_note: Text = "development"


class Explanation(Record):
    effects: Text
    alternatives: Annotated[tuple[Text, ...], Field(min_length=2, max_length=8)]
    uncertainty: Text


class Packet(Record):
    setting: Literal["accent"] = "accent"
    scope: Literal["fixture/palette-bundle"] = "fixture/palette-bundle"
    value: Color
    source: Literal["default", "project"]
    policy: Policy
    objective: Text
    schema_revision: Text
    evidence_revision: Text
    explanation: Explanation
    proposer: Text
    expires_at: int
    generation: int


class Resolution(Record):
    revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    decision: Literal["approved", "rejected", "cancelled"]


class Blocked(ValueError):
    """Stable reason identifies the setting and decision point."""


def block(reason: str) -> NoReturn:
    raise Blocked(f"accent: publication/owner decision: {reason}")


def digest(packet: Packet) -> str:
    return hashlib.sha256(packet.model_dump_json().encode()).hexdigest()


def contrast(value: str) -> float:
    channels: list[float] = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear: list[float] = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    luminance: float = sum(c * w for c, w in zip(linear, (0.2126, 0.7152, 0.0722)))
    return 1.05 / (luminance + 0.05)


class PaletteConsumer:
    def __init__(
        self, path: Path, principals: tuple[Principal, ...], clock: Callable[[], float] = time.time
    ) -> None:
        self.path = path
        self.principals = principals
        self.clock = clock
        if len({p.id for p in principals}) != len(principals):
            raise ValueError("principal IDs must be unique")

    def initialize(self, state: State) -> None:
        """Trusted fixture provisioning only. Never used to recover missing state."""
        with self.transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS config (
                    id INTEGER PRIMARY KEY CHECK(id=1), body TEXT, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS proposals (revision TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions (
                    revision TEXT PRIMARY KEY, outcome TEXT, actor TEXT, grant_id TEXT, at REAL);
                CREATE TABLE IF NOT EXISTS grants (
                    id TEXT PRIMARY KEY, actor TEXT, revision TEXT, until REAL, revoked INTEGER);
                CREATE TABLE IF NOT EXISTS evaluations (revision TEXT PRIMARY KEY, body TEXT);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, mode TEXT, revision TEXT, body TEXT NOT NULL);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO config VALUES (1, ?, 0)", (state.model_dump_json(),))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # A single writer, bounded lock wait; no callbacks or external effects.
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def principal(self, actor: Principal) -> None:
        if not any(actor is known for known in self.principals):
            block("untrusted principal capability")

    def owner(self, actor: Principal) -> None:
        self.principal(actor)
        if actor.id != "owner" or actor.kind != "human":
            block("owner required")

    def state(self, db: sqlite3.Connection) -> State:
        row = db.execute("SELECT body FROM config WHERE id=1").fetchone()
        if row is None:
            block("policy/config unavailable")
        return State.model_validate_json(row[0])

    def configure(self, actor: Principal, state: State) -> None:
        self.owner(actor)
        with self.transaction() as db:
            current = self.state(db)
            if current.policy is not None and state.policy is None:
                block("enrollment cannot be removed")
            dependencies = {"policy", "accent", "objective", "schema_revision", "evidence_revision"}
            material = current.model_dump(include=dependencies) != state.model_dump(
                include=dependencies
            )
            db.execute(
                "UPDATE config SET body=?, generation=generation+? WHERE id=1",
                (state.model_dump_json(), int(material)),
            )

    def snapshot(
        self,
        state: State,
        explanation: Explanation,
        proposer: str,
        expires_at: int,
        generation: int,
    ) -> Packet:
        if state.policy is None:
            block("project has no declaration")
        if state.policy.evaluation == "na" and not state.policy.na_reason:
            block("evaluation N/A requires a reason")
        return Packet(
            value=state.accent or "#777777",
            source="project" if state.accent is not None else "default",
            policy=state.policy,
            objective=state.objective,
            schema_revision=state.schema_revision,
            evidence_revision=state.evidence_revision,
            explanation=explanation,
            proposer=proposer,
            expires_at=expires_at,
            generation=generation,
        )

    def propose(self, actor: Principal, explanation: Explanation, expires_at: int) -> str:
        self.principal(actor)
        if type(expires_at) is not int or not self.clock() < expires_at <= self.clock() + 86400:
            block("proposal expiry must be within one day")
        with self.transaction() as db:
            packet = self.snapshot(
                self.state(db),
                explanation,
                actor.id,
                expires_at,
                db.execute("SELECT generation FROM config WHERE id=1").fetchone()[0],
            )
            revision = digest(packet)
            db.execute(
                "INSERT OR IGNORE INTO proposals VALUES (?, ?)",
                (revision, packet.model_dump_json()),
            )
            return revision

    def packet(self, db: sqlite3.Connection, revision: str) -> Packet:
        row = db.execute("SELECT body FROM proposals WHERE revision=?", (revision,)).fetchone()
        if row is None:
            block("missing proposal")
        packet = Packet.model_validate_json(row[0])
        if digest(packet) != revision:
            block("corrupt proposal")
        if self.clock() >= packet.expires_at:
            block("expired proposal")
        current = self.snapshot(
            self.state(db),
            packet.explanation,
            packet.proposer,
            packet.expires_at,
            db.execute("SELECT generation FROM config WHERE id=1").fetchone()[0],
        )
        if current != packet:
            block("stale proposal")
        return packet

    def render(self, revision: str) -> str:
        with self.transaction() as db:
            return self.packet(db, revision).model_dump_json(indent=2)

    def delegate(self, owner: Principal, actor: Principal, revision: str, until: float) -> str:
        self.owner(owner)
        self.principal(actor)
        with self.transaction() as db:
            packet = self.packet(db, revision)
            if not self.clock() < until <= packet.expires_at:
                block("invalid delegation lifetime")
            grant = uuid.uuid4().hex
            db.execute(
                "INSERT INTO grants VALUES (?, ?, ?, ?, 0)", (grant, actor.id, revision, until)
            )
            return grant

    def revoke(self, owner: Principal, grant: str) -> None:
        self.owner(owner)
        with self.transaction() as db:
            db.execute("UPDATE grants SET revoked=1 WHERE id=?", (grant,))

    def authority(
        self, db: sqlite3.Connection, packet: Packet, actor: Principal, grant: str | None
    ) -> None:
        self.principal(actor)
        if actor.id == packet.policy.owner and actor.kind == "human" and grant is None:
            return
        if actor.id == packet.proposer:
            block("agent cannot approve its own proposal")
        row = db.execute(
            "SELECT actor, revision, until, revoked FROM grants WHERE id=?", (grant,)
        ).fetchone()
        if (
            row is None
            or row[0] != actor.id
            or row[1] != digest(packet)
            or row[3]
            or row[2] <= self.clock()
        ):
            block("missing, expired or revoked scoped delegation")

    def resolve(self, actor: Principal, resolution: Resolution, grant: str | None = None) -> None:
        with self.transaction() as db:
            packet = self.packet(db, resolution.revision)
            self.authority(db, packet, actor, grant)
            existing = db.execute(
                "SELECT outcome, actor, grant_id FROM decisions WHERE revision=?",
                (resolution.revision,),
            ).fetchone()
            expected = (resolution.decision, actor.id, grant)
            if existing is not None:
                if existing != expected:
                    block("conflicting terminal decision")
                return
            db.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?)",
                (resolution.revision, *expected, self.clock()),
            )

    def evaluate(self, revision: str) -> dict[str, str | float | bool]:
        with self.transaction() as db:
            packet = self.packet(db, revision)
            if packet.policy.evaluation == "na":
                result: dict[str, str | float | bool] = {
                    "status": "na",
                    "reason": packet.policy.na_reason or "",
                }
            else:
                ratio = contrast(packet.value)
                result = {
                    "status": "passed" if ratio >= 4.5 else "failed",
                    "contrast": ratio,
                    "plan": "contrast-1: white text, minimum 4.5",
                }
            db.execute(
                "INSERT OR IGNORE INTO evaluations VALUES (?, ?)", (revision, json.dumps(result))
            )
            return result

    def publish(
        self,
        actor: Principal,
        operation: str,
        *,
        revision: str | None = None,
        sandbox: bool = False,
    ) -> str:
        """The ONLY consumer effect writer. No arbitrary CSS/artifact input or promotion path.

        A committed admission is immutable: a retry returns that receipt, even if
        policy/authority changes later. A different revision requires a new operation.
        """
        self.principal(actor)
        if (
            not isinstance(operation, str)
            or not 1 <= len(operation) <= 128
            or type(sandbox) is not bool
        ):
            block("invalid operation")
        mode = "provisional" if sandbox else "authoritative"
        with self.transaction() as db:
            # Always load policy, including on opt-out/retry. Store failure never opts out.
            state = self.state(db)
            existing = db.execute(
                "SELECT mode, revision, body FROM artifacts WHERE id=?", (operation,)
            ).fetchone()
            if existing is not None:
                if existing[:2] != (mode, revision):
                    block("operation conflict; sandbox promotion forbidden")
                return existing[2]
            value = state.accent or "#777777"
            if not sandbox and state.policy is not None:
                if revision is None:
                    block("required decision missing")
                packet = self.packet(db, revision)
                decision = db.execute(
                    "SELECT outcome, actor, grant_id FROM decisions WHERE revision=?", (revision,)
                ).fetchone()
                if decision is None or decision[0] != "approved":
                    block("required approval missing/rejected/cancelled")
                approver = next((p for p in self.principals if p.id == decision[1]), None)
                if approver is None:
                    block("approver unavailable")
                self.authority(db, packet, approver, decision[2])
                evaluation = db.execute(
                    "SELECT body FROM evaluations WHERE revision=?", (revision,)
                ).fetchone()
                if evaluation is None or json.loads(evaluation[0])["status"] not in (
                    "passed",
                    "na",
                ):
                    block("evaluation missing/failed")
                value = packet.value  # consume THIS checked immutable snapshot, never re-resolve
            elif revision is not None:
                block("revision is not applicable to sandbox/unconfigured consumer")
            body = json.dumps(
                {
                    "mode": mode,
                    "revision": revision,
                    "value": value,
                    "css": f":root {{ --accent: {value}; }}",
                },
                sort_keys=True,
            )
            db.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?)", (operation, mode, revision, body)
            )
            return body
