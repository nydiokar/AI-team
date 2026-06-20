# Cockpit Refactor Specification

Status: v3 — adversarially reviewed + OpenClaw-informed, re-ranked, with build milestone M1
        (no code changed by this document)
Owner: Nyd
Date: 2026-06-20
Source review: `src/` as of commit `3b59e5b`
Companion: `uploads/remote_coding_agent_cockpit_orientation.md` (strategic context only)

---

## 0. Purpose & scope of THIS document

This is a **refactoring specification**, not an implementation. It does two things:

1. **Validates** the orientation brief against the actual codebase — what the
   brief asks for that *already exists*, what is *missing*, and what is *over-asked*.
2. **Derives** the smallest set of changes ("moves") that make the system ready to
   grow a Web UI and later review/handoff workflows, with detailed-blueprint depth
   per move (signatures, new module skeletons, exact call sites).

Hard rule carried from the brief: **a move is allowed only if it satisfies at least
one of**
1. removes coupling blocking a second interface beside Telegram;
2. centralizes duplicated session/task/run state used in multiple places;
3. makes backends interchangeable through an existing/near-existing interface;
4. creates one stable event/status shape both Telegram and a future Web UI consume;
5. reduces future feature work without speculative machinery.

Anything failing all five is **rejected** below, on purpose.

---

## 1. Audit — spec vs. reality

### 1.1 Already mature — DO NOT TOUCH

| Brief concept | Where it already lives | Evidence | Verdict |
|---|---|---|---|
| Backend adapter contract (§14) | `src/core/interfaces.py` → `CodingBackend` ABC + `ExecutionResult` | `create_session / resume_session / run_oneoff / cancel / close / compact_session` | The backend boundary exists. Preserve & harden, never replace. |
| Stable event envelope (§12, brief field list) | `src/core/observability.py` → `emit_event()` | Canonical NDJSON: `timestamp, event, node_id, task_id, session_id, **fields`; correlation contextvars; rotation; redaction. Docstring already says *"feeds both Telegram and the future Web UI."* | The event envelope exists. This is why no big refactor is needed. |
| Transport notification boundary (out) (§13 "UI renders events") | `src/core/notification_service.py` → `NotificationService` | Orchestrator calls `self.notifier.notify_*`; never reaches into `telegram_interface` for outbound. Docstring: adding a WebSocket channel = one handler call. | Outbound seam exists and is correct. |
| State + queryable mirror (§10 storage) | `src/control/db.py` + `src/core/session_store.py` | JSON authoritative + DB shadow-mirror; `list_sessions / list_tasks / list_nodes / get_task / get_task_by_session`. | Read model exists. No schema migration needed. |
| Worker registry (§10.4, §15) | `src/control/node_registry.py`, `task_server.py`, `node_inspector.py` | Singleton `get_registry()`, capability rows, heartbeats, inspect-on-owning-node. | Worker registry exists. |
| Result rendering | `src/core/result_text.py` | `session_reply_text / short_failure_reason / format_file_change_lines` already extracted out of orchestrator. | Formatting already separated. |

**Consequence:** of the brief's "8 boundaries" (transport/command/event/session/task/
backend/worker/workflow), **event, outbound-transport, backend, worker, and storage
are effectively done.** Only **command (inbound)**, **session-lifecycle ownership**,
and a **read view-model** are genuinely missing.

### 1.2 Real seams / gaps

**G1 — No *transport-neutral* inbound command boundary.**
`submit_instruction()` (`orchestrator.py:1157`) is a clean dispatch seam, but session
*lifecycle* (create + bind + node-pin + model-pin) is reachable **only through
Telegram**. It is not scattered — it is one private method,
`TelegramInterface._create_and_bind_session` (`interface.py:1063`), called from three
sites (`:2101`, `:2297`, `:2489`). So the logic is already consolidated; what's missing
is that it lives **on the Telegram class** instead of in a transport-neutral service.
Secondary: the valid-backend tuple is duplicated at `interface.py:2252` and `:2380`.

A Web UI "New session" button would have to **re-implement `_create_and_bind_session`**
(including `machine_id`/node pinning and `model` pinning, which happen *inside* it).
Fails brief rule 1. NOTE: there is **no "switch backend" flow** in the codebase — do not
invent one (see Move B).

**G2 — Backend set duplicated; not interchangeable through one place.**
Identical `{name: adapter}` literals at `orchestrator.py:59` and `worker/agent.py:385`;
display-icon `if backend == "codex"` branches at `interface.py:800,829,852,874`;
valid-names tuple twice. Adding a backend touches ≥4 files. Fails brief rule 3.

**G3 — No read-side view model.**
Per-session display state is re-derived ad hoc in each list handler
(`interface.py:787, 2117, 2158`) and `orchestrator.get_status()` returns a thin dict.
A Web UI dashboard re-derives it a third time. Fails brief rule 4.

**G4 — No workflow hook points.** No `review.requested / handoff.created /
approval.requested` events or service methods. (Substrate — the event stream — exists,
so this is cheap later. Not pain *today*.)

### 1.3 Over-asked by the brief — explicitly REJECTED or DEFERRED here

| Brief asks | Decision | Reason |
|---|---|---|
| Task/Run/Review/Handoff as new domain tables (§10.1–10.8) | **REJECT now** | No current pain; `Task`+`Session`+events already cover live needs. Speculative schema. |
| Registries for transports/roles/prompt-profiles/tools (§15) | **DEFER** | Only `BackendRegistry` has present pain (G2). The rest are future. |
| WebSocket gateway + HTTP routes (§12) | **DEFER** | Not requested; event stream + DB reads already make it additive later. |
| ACP / A2A bridges (§19–20) | **DEFER** | Brief itself defers these. |
| Supervisor agents / workflow engine (§16–17) | **DEFER** | Document the hook seam only (Move E), build nothing. |
| Full layer re-org into `interfaces/api/core/...` (§9) | **REJECT** | Cosmetic move of mature code = risk with no leverage. Keep current layout. |

---

## 1bis. What we borrow from OpenClaw (and what we deliberately don't)

OpenClaw is a mature hub-and-spoke gateway (a single WS Gateway control plane in front
of an Agent Runtime, with operator/node client roles). Reviewing its gateway protocol
and session/state design (sources at end of section) mostly **validates** what this
codebase already has, and suggests exactly **one** new primitive worth adopting now.

**Validated — already present here, no change needed:**

| OpenClaw pattern | Our existing equivalent |
|---|---|
| WS frame envelope `{type, id, method/event, payload, seq}` | `observability.emit_event` NDJSON envelope `{timestamp, event, node_id, task_id, session_id, **fields}` |
| "Events are not replayed; refresh state on a gap" | DB read model (`db.list_sessions/list_tasks/list_nodes`) **is** the refresh path |
| Two-layer persistence: metadata `sessions.json` + append-only `.jsonl` transcript | `session_store.py` (one JSON per session) + per-session event log + `events.ndjson` |
| `operator` vs `node` client roles | gateway vs mesh worker split (`node_registry`) |
| Gateway owns routing/queue/state; Runtime owns reasoning | orchestrator (queue/state) vs `CodingBackend` adapters (execution) |
| Structured `req/res` with `id` + `ok` + `error` (no prose in protocol) | confirms v2 `CommandResult` design (`reason` code, not text) |

**Borrow now — one primitive:**

- **`SessionOrigin` (inspired by OpenClaw's `sessionKey` = `agent:<id>:<channel>:<kind>:<id>`).**
  Today a `Session` carries Telegram-shaped fields (`telegram_chat_id`, `telegram_thread_id`)
  and nothing else describes *where it came from*. OpenClaw makes origin explicit and
  parseable, which is precisely what lets a Web-UI session and a Telegram session coexist
  without one channel's fields leaking into core. We adopt the **concept** (a small,
  transport-neutral `channel` + `kind` tag), **not** their key-string format or their four
  scoping modes. This is the seam that future-proofs `SessionService` for a second surface.
  See Move B (now folds this in).

**Deliberately NOT borrowed (their scale, our over-engineering trap):**

- 4-tier caching, string interning, `enforceSessionDiskBudget` — built for millions of
  sessions; we have tens. **Reject.**
- Full WS handshake / challenge-nonce / device-pairing / scope tokens — that is the Web-UI
  transport itself = Move F, **deferred**. Don't pull it forward.
- Multiple scoping strategies (`main`/`per-sender`/`global`) — we have exactly one
  (per chat). Adding modes we don't need is speculative. **Reject.**

Sources: [Gateway protocol](https://github.com/openclaw/openclaw/blob/main/docs/gateway/protocol.md) ·
[Session & state management](https://deepwiki.com/openclaw/openclaw/2.4-session-and-state-management) ·
[Architecture overview](https://ppaolo.substack.com/p/openclaw-system-architecture-overview)

---

## 2. Ranked moves (re-ranked after OpenClaw review + adversarial pass)

The v1 order led with the backend registry. After the adversarial pass shrank it to a
4-line factory table, and after the OpenClaw review confirmed the **command seam** is the
real unlock, the order is reversed: **the contract doc and the SessionService seam lead;
the registry rides along inside B; the read-model waits for a consumer.**

| ID | Move | Brief rule(s) | Leverage | Size | Risk | Verdict |
|---|---|---|---|---|---|---|
| **D** | `docs/CONTROL_CONTRACT.md` (event + command catalog) | 4,5 | High | S | None | **DO 1st** |
| **B** | `SessionService` + `SessionOrigin` (extract lifecycle off Telegram) | 1,2 | **Highest** | M | Med | **DO 2nd** |
| A | Backend registry (now a sub-step of B, not a standalone move) | 3 | Low now | XS | Low | **DO inside B** |
| C | Session view-model DTO | 4 | Med | S | Low | **DEFER until a 2nd reader exists** |
| E | Reserve workflow event names (doc only) | 5 | Med | XS | None | **DO (in D)** |
| F | WebSocket / HTTP surface | — | — | L | — | **DEFER** |
| G | New domain tables | — | — | L | — | **REJECT** |

Rationale for the demotions/deferrals:
- **A → sub-step of B:** deduping a 4-line backend dict across two files doesn't earn a
  standalone "move." `SessionService` needs `is_valid_backend()` anyway, so the registry
  is created as the first commit *of* B.
- **C → deferred:** nothing consumes `SessionView` today; Telegram works without it.
  Building a read-model before a second reader exists is the speculation the brief warns
  against. Build it *with* the Web UI, when its shape is driven by a real consumer.
- **E → folded into D:** reserving event names is a doc act, so it lives in the contract doc.

**The actual near-term work is D then B.** Everything else is deferred or rejected. The
detailed build plan for D and B is **§12 (Milestone M1)**.

---

## 3. Move A — Backend registry (blueprint)

**Goal:** one declaration site for the set of backends. Today that set is a literal dict
in `orchestrator.py:59` and an identical one in `worker/agent.py:385`, plus a valid-names
tuple at `interface.py:2252` and `:2380`. Adding a backend means editing all of them.
This move collapses that to **one** edit. It does **not** touch the `CodingBackend`
contract and it is **not** a plugin system — it is a typed lookup table.

Scope discipline: the registry owns *identity and construction only* — name → adapter
factory. **Display concerns (icons, labels) are NOT in the registry**; they belong to the
surface that renders them. (An earlier draft put icons here; that was scope creep.)

### A.1 New file: `src/backends/registry.py`

```python
from __future__ import annotations
from typing import Callable, Dict, Tuple

from src.core.interfaces import CodingBackend
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend
from .opencode import OpenCodeBackend, OpenCodeServerBackend

DEFAULT_BACKEND = "claude"

# The ONE place the backend set is declared. name -> zero-arg factory.
_FACTORIES: Dict[str, Callable[[], CodingBackend]] = {
    "claude":          ClaudeCodeBackend,
    "codex":           CodexBackend,
    "opencode":        OpenCodeBackend,
    "opencode-server": OpenCodeServerBackend,
}

def build_backends() -> Dict[str, CodingBackend]:
    """Instantiate {name: CodingBackend} — replaces the duplicated dict literals."""
    return {name: factory() for name, factory in _FACTORIES.items()}

def valid_backend_names() -> Tuple[str, ...]:
    return tuple(_FACTORIES.keys())

def is_valid_backend(name: str) -> bool:
    return (name or "").strip().lower() in _FACTORIES
```

### A.2 Exact call-site changes

| File:line | Before | After |
|---|---|---|
| `orchestrator.py:59` | `self._backends = { "claude": ClaudeCodeBackend(), ... }` | `self._backends = build_backends()` |
| `orchestrator.py:1540,1552,2502` | `self._backends.get(name, self._backends["claude"])` | unchanged (still a name-keyed dict); may use `DEFAULT_BACKEND` |
| `worker/agent.py:385` | `_make_backends()` literal dict | `return build_backends()` |
| `telegram/interface.py:2252,2380` | `_valid_backends = ("claude","codex",...)` | `_valid_backends = valid_backend_names()` |

**Out of scope (do NOT touch in Move A):** the `interface.py:800/829/852/874` icon
branches. They are presentation, decided by Telegram, and unrelated to the backend set.
Leave them exactly as they are; revisit only if/when a surface needs richer backend
display, and even then the mapping lives in that surface, not the registry.

### A.3 Risk & test
Low — pure consolidation, adapters unchanged. New `tests/test_backend_registry.py`:
`set(build_backends()) == set(valid_backend_names())`, and `build_backends()` values are
`CodingBackend` instances. Existing `tests/test_*_backend.py` continue to cover adapters.

---

## 4. Move C — Session view-model DTO (blueprint)

**Goal:** one read shape for "what the operator sees about a session", consumed by
Telegram lists today and a Web UI dashboard later. Read-only; additive.

### C.1 New file: `src/core/view_models.py`

```python
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
from src.core.interfaces import Session, SessionStatus


@dataclass(frozen=True)
class SessionView:
    """Operator-facing read model for a session. Derived, never persisted.

    Maps 1:1 to existing Session fields and the few *derived booleans* every
    surface re-computes today (needs_input, is_active). Carries the raw
    ``backend`` string only — rendering (icons/labels) is each surface's job,
    NOT this DTO's. Both Telegram lists and a future Web UI consume this instead
    of re-deriving status logic from Session ad hoc.
    """
    session_id: str
    backend: str                # raw name; surface decides how to display it
    repo_path: str
    status: str                 # SessionStatus value
    machine_id: str
    model: Optional[str]
    last_task_id: str
    last_summary: str
    last_files_modified: List[str]
    needs_input: bool           # status == AWAITING_INPUT
    is_active: bool             # status not in {CLOSED, ERROR, CANCELLED}
    updated_at: str

    @classmethod
    def from_session(cls, s: Session) -> "SessionView":
        return cls(
            session_id=s.session_id,
            backend=s.backend,
            repo_path=s.repo_path,
            status=s.status.value,
            machine_id=s.machine_id,
            model=s.model,
            last_task_id=s.last_task_id,
            last_summary=s.last_result_summary or s.last_summary,
            last_files_modified=list(s.last_files_modified or []),
            needs_input=(s.status == SessionStatus.AWAITING_INPUT),
            is_active=s.status not in (SessionStatus.CLOSED, SessionStatus.ERROR, SessionStatus.CANCELLED),
            updated_at=s.updated_at,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)   # JSON-ready for a future Web UI / WebSocket
```

### C.2 Adoption (incremental, non-breaking)
- Telegram list handlers (`interface.py:787, 2117, 2158`) **may** switch to
  `SessionView.from_session(s)` for the derived `status`/`needs_input`/`is_active` logic
  they recompute today — optional, handler-by-handler. Icon/label rendering stays in
  Telegram (the DTO deliberately does not carry it). The DTO existing is the deliverable;
  migration is opt-in.
- A future Web UI calls `[SessionView.from_session(s).to_dict() for s in store.list_all()]`
  with zero new query code.

### C.3 Risk & test
Low (additive). New `tests/test_view_models.py`: every `SessionStatus` maps to correct
`needs_input`/`is_active`; `to_dict()` is JSON-serializable.

---

## 5. Move B — `SessionService` + `SessionOrigin` (the only behavior-touching move)

**Goal:** lift session *lifecycle* logic out of Telegram into a transport-neutral core
service, so a second interface issues the same calls. This is the inbound symmetry to
the existing outbound `NotificationService`. **Thin extraction — move existing logic,
do not redesign it.** The one *addition* (from the OpenClaw review) is `SessionOrigin`:
a tiny transport-neutral tag so a session records *which channel/kind created it* without
Telegram fields leaking into core. Everything else is pure extraction.

### B.0 `SessionOrigin` — the one borrowed primitive

OpenClaw resolves every inbound message to `agent:<id>:<channel>:<kind>:<id>`. We adopt
only the **concept**: two strings on the session describing its origin.

```python
# in src/core/interfaces.py, alongside Session
from dataclasses import dataclass

@dataclass(frozen=True)
class SessionOrigin:
    channel: str = "telegram"   # "telegram" | "web" | "cli" | future surfaces
    kind: str = "user"          # "user" | "cron" | "subagent" (future workflow)
```

Adoption rules (keep it non-breaking):
- Add **one optional field** to `Session`: `origin: Optional[SessionOrigin] = None`.
  `SessionStore._to_dict/_from_dict` serialize it as a nested `{channel, kind}` dict;
  a missing value reads back as `SessionOrigin()` → defaults to today's behavior
  (`telegram`/`user`). **No DB migration** — it rides in the existing JSON + shadow row.
- Telegram passes nothing → gets the default. A future Web UI passes `channel="web"`.
- This is the seam that makes the per-chat `telegram_chat_id` *one* transport's detail
  rather than a core assumption. Do NOT add scoping modes — origin is descriptive, not a
  routing policy (that's where OpenClaw's complexity lives; we don't need it).

**Ground truth (verified, do not get this wrong):**
- The lifecycle logic is exactly one method — `TelegramInterface._create_and_bind_session`
  (`interface.py:1063`), called from `:2101`, `:2297`, `:2489`.
- That method does FOUR things, and a faithful extraction must preserve all four:
  `store.create(...)` → optionally set `session.model` → optionally set
  `session.machine_id = node_id` (mesh node pinning) → `store.bind(chat_id, sid)`.
  **Dropping `node_id`/`model` would silently break remote sessions and model pinning.**
- There is **no "switch backend" flow** anywhere in the codebase. The only
  `backend_session_id = ""` reset (`interface.py:2772`) is session *close*, not a switch.
  Do NOT add a `switch_backend` method — it would be inventing a feature.

### B.1 New file: `src/core/session_service.py`

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional
from src.core.session_store import SessionStore
from src.core.interfaces import Session, SessionOrigin
from src.core.view_models import SessionView          # only if Move C is built
from src.backends.registry import is_valid_backend, DEFAULT_BACKEND


@dataclass(frozen=True)
class CommandResult:
    """Accepted/rejected envelope for inbound session commands.

    Carries the structured outcome ONLY — no user-facing text. ``reason`` is a
    stable machine code (e.g. "unknown_backend", "session_not_found"); each
    transport maps it to its own wording. ``session`` is the affected Session so
    a transport can render confirmation (e.g. the session card) without a second
    lookup. Keeping prose out of core is the whole point of the transport
    boundary — don't reintroduce it here.
    """
    ok: bool
    reason: str = ""                    # "" on success; stable code on reject
    session: Optional[Session] = None


class SessionService:
    """Transport-neutral session lifecycle. Telegram and a future Web UI both
    call these methods instead of owning create/bind logic.

    Owns *session lifecycle only*. Task dispatch stays on
    orchestrator.submit_instruction() (already transport-neutral); outbound
    notifications stay on NotificationService. This closes the inbound gap (G1)
    without absorbing those existing seams.
    """
    def __init__(self, session_store: SessionStore):
        # Reuse the orchestrator's store — never construct a second one.
        self.store = session_store

    def create_session(self, *, backend: str, repo_path: str,
                        chat_id: Optional[int] = None,
                        owner_user_id: Optional[int] = None,
                        node_id: str = "__local__",
                        model: Optional[str] = None,
                        origin: Optional[SessionOrigin] = None,
                        bind_chat: bool = True) -> CommandResult:
        """Faithful extraction of TelegramInterface._create_and_bind_session.

        Preserves node pinning (machine_id), model pinning, and the
        single-save semantics of the original. Adds origin tagging (defaults to
        telegram/user so existing behavior is unchanged).
        """
        backend = (backend or DEFAULT_BACKEND).strip().lower()
        if not is_valid_backend(backend):
            return CommandResult(False, reason="unknown_backend")
        s = self.store.create(backend=backend, repo_path=repo_path,
                              telegram_chat_id=chat_id, owner_user_id=owner_user_id)
        s.origin = origin or SessionOrigin()
        dirty = True   # origin set above; single save covers model/node too
        if model:
            s.model = model
        if node_id and node_id != "__local__":
            s.machine_id = node_id
        if dirty:
            self.store.save(s)
        if bind_chat and chat_id is not None:
            self.store.bind(chat_id, s.session_id)
        return CommandResult(True, session=s)

    def bind_active(self, chat_id: int, session_id: str) -> CommandResult:
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        self.store.bind(chat_id, session_id)
        return CommandResult(True, session=s)

    # --- queries (read) — only add these two when Move C (SessionView) is built ---
    def list_views(self) -> List[SessionView]:
        return [SessionView.from_session(s) for s in self.store.list_all()]

    def active_view(self, chat_id: int) -> Optional[SessionView]:
        s = self.store.get_active(chat_id)
        return SessionView.from_session(s) if s else None
```

> The two `*_view` query methods depend on Move C, which is **deferred**. For M1, ship
> `SessionService` with `create_session` + `bind_active` only; add the read methods when a
> second reader (Web UI) exists. The service is fully useful without them.

### B.2 Wiring
- `orchestrator.__init__`: `self.session_service = SessionService(self.session_store)`
  (reuse the existing store instance).
- `TelegramInterface` already holds `self.orchestrator`; reach it via
  `self.orchestrator.session_service`. Keep `TelegramInterface._create_and_bind_session`
  as a **thin wrapper** that calls `create_session(...)` and returns
  `result.session` — so the three existing call sites (`:2101/:2297/:2489`) and their
  tests don't change. This is the minimal-blast-radius path: one method body changes,
  callers don't.

### B.3 Migration (smallest blast radius)
| `interface.py` | Change |
|---|---|
| `_create_and_bind_session` body (`1063`) | Replace its 4 inline steps with `self.orchestrator.session_service.create_session(backend=..., repo_path=..., chat_id=..., owner_user_id=..., node_id=node_id, model=model)`; return `.session`. Signature unchanged → 3 callers untouched. |
| `bind(...)`-after-`get(...)` sites (`2223, 2340, 2366`) | Optional, later: route through `bind_active`. Not required for the milestone. |
| list handlers (`787, 2117, 2158`) | Optional: consume `list_views()` (ties to Move C). Not required. |

**Explicitly NOT in Move B:** no `switch_backend` (doesn't exist), no change to node/repo
picker UI, no change to `submit_instruction` dispatch, no model-picker rewrite. Behavior
stays identical; only the *owner* of the create steps moves.

### B.4 Risk & test
Medium — touches the create path. Mitigations:
- Existing coverage already exercises this exact path:
  `test_session_new_creates_session_and_guides_next_step`,
  `test_session_new_repo_callback_creates_session`,
  `test_session_new_remote_command_uses_db_node_repos` (node pinning), and
  `test_session_close_closes_local_backend_and_clears_backend_session_id`. Run
  `tests/test_telegram_session_flow.py` after the wrapper change — green = behavior held.
- `SessionService` shares the orchestrator's `SessionStore` (DB shadow-write unchanged).
- No change to `Session` shape, JSON layout, or DB schema → revert = restore the method
  body; the service can sit unused if a problem appears.

---

## 6. Move D — `docs/CONTROL_CONTRACT.md` (blueprint)

**Goal:** stop the next agent from re-discovering the architecture. One doc that pins the
**already-stable** contracts so a Web UI/workflow author reads instead of greps.

Required sections:
1. **Event envelope** — copy the canonical fields from `observability.emit_event`
   docstring; declare them stable; "unknown fields are opaque, skip them."
2. **Event catalog** — enumerate event names currently emitted (grep `_emit_event(` /
   `emit_event(` across `orchestrator.py`, `notification_service.py`, `task_server.py`)
   with a one-line meaning each; mark which Telegram currently consumes.
3. **Inbound command surface** — document the two transport-neutral entry points:
   `orchestrator.submit_instruction(...)` (dispatch) and `SessionService.*` (lifecycle,
   once Move B lands) as *the* way any new surface issues intent. State the rule:
   *no transport may write session state directly; it goes through SessionService.*
4. **Backend contract pointer** — point at `CodingBackend` + `registry.py` as the single
   place to add a backend.
5. **Read model pointer** — `SessionView` + `db.list_sessions/list_tasks/list_nodes`.

This doc is the acceptance artifact proving "final enough for the next interface."

---

## 7. Move E — Workflow hook seam (doc only)

In `CONTROL_CONTRACT.md`, **reserve** (do not implement) these event names for the future
review/handoff workflow, so they are emitted from one vocabulary when built:

```
review.requested   review.completed
handoff.created
approval.requested approval.granted
run.failed         run.completed   (map to existing *_finished events)
```

Rule recorded in the doc: *workflow steps emit events + call existing services; they do
not mutate state directly and do not require an engine.* No code in this move.

---

## 8. Sequencing & acceptance

Order: **A → C → D → E → B.**

Per-move acceptance:
- **A:** all backend tests green; Telegram icons byte-identical; backend list comes from
  one module; adding a backend = one edit in `registry.py`.
- **C:** `SessionView` covers all `SessionStatus` values; `to_dict()` JSON-serializable;
  no persisted-state change.
- **D/E:** doc lists every currently-emitted event and the two inbound entry points; a
  reader can answer "how do I add a surface?" without reading orchestrator internals.
- **B:** `tests/test_telegram_session_flow.py` + `test_components.py` green; no Telegram
  behavior change; create/bind/switch flow only through `SessionService`; second-interface
  author can create a session with one service call.

**Milestone definition of done (matches brief §27, scoped to what we build now):**
> Telegram session create/bind/switch flows route through `SessionService`; backends are
> declared once; a `SessionView` read model exists; and `CONTROL_CONTRACT.md` documents the
> event + command + backend + read seams. A Web UI can then be added by consuming the
> documented event stream and calling `SessionService` / `submit_instruction` — with **no
> further refactor of core**.

---

## 9. Explicitly deferred / rejected (carry-forward)

Deferred (build when the feature actually arrives): WebSocket gateway & HTTP routes,
ACP/A2A bridges, supervisor agents & workflow engine, transport/role/prompt/tool
registries, native mobile.
Rejected (no current pain, speculative): new Task/Run/Review/Handoff tables, full
`interfaces/api/core/adapters` directory re-org, plugin SDK.

## 10. Next smallest useful step after this spec is approved

Implement **Move A** (backend registry) — it is pure consolidation, lowest risk, and
de-risks Moves B and C by giving them a single backend vocabulary to depend on.

---

## 11. Adversarial review log (v1 → v2)

Defects found reviewing v1 against the actual code, and how v2 fixes them:

| # | v1 defect | Why it was wrong | v2 fix |
|---|---|---|---|
| F1 | `SessionService.create_session` omitted `node_id` and `model` | The real `_create_and_bind_session` (`interface.py:1063`) pins `session.machine_id = node_id` and `session.model`. Omitting them would **silently break mesh/remote sessions and model pinning.** | `create_session` now takes `node_id`/`model` and reproduces the original's set-then-single-save semantics. |
| F2 | Invented `switch_backend()` with `backend_session_id=""` reset | **No switch-backend flow exists.** The only such reset (`:2772`) is session *close*. Pure hallucination — adding a feature, not refactoring. | `switch_backend` deleted; B.3 states explicitly it must not be added. |
| F3 | Audit framed lifecycle as "scattered across Telegram handlers" | It is **one** method called from 3 sites — already consolidated, just not transport-neutral. Overstated premise. | G1 reworded: the issue is *ownership* (on the Telegram class), not duplication. |
| F4 | Said node selection was "out of scope for B" while B extracted the method that does node pinning | Internal contradiction. | Node pinning is now in scope as a `create_session` parameter; only the picker *UI* stays out of scope. |
| F5 | `CommandResult.message` carried user-facing text | Pushes prose into core — violates the transport boundary the spec itself preaches. | Replaced with `reason` (stable machine code) + the affected `Session`; transports own wording. |
| F6 | Move A added `icon`/`label` to the registry with a "byte-identical icons" invariant | **Scope creep.** Display is the surface's concern, not the registry's; the invariant was also false (opencode renders 🧠 today, not a dedicated glyph). | Registry reduced to name→factory only; icon branches left untouched; invariant removed. |
| F7 | "behind tests" asserted without confirming coverage | Unverified claim. | Verified: 4 named tests in `test_telegram_session_flow.py` cover create/node-pin/close; cited in B.4. |

Net effect: Move B shrank to a single-method body change behind an unchanged signature
(callers and their tests untouched), and Move A shrank to a name→factory table. Both are
now smaller and lower-risk than v1 claimed to be.

---

## 12. Milestone M1 — "Transport-neutral session core"

**Outcome:** the gateway gains a documented control contract and a transport-neutral
`SessionService`, so a second surface (Web UI) can create/bind sessions and consume events
**without touching Telegram code or core internals**. Telegram behavior byte-identical. No
DB migration. Fully revertible. Scope = Move D + Move B (Move A folded in). C/E(code)/F/G out.

**The build plan is a separate, tickable execution doc: [`docs/M1_CHECKLIST.md`](./M1_CHECKLIST.md).**
That checklist is the single source of truth for *how* M1 is built and is the anti-scope-escape
mechanism — see §13.

Size: ~3 new files (~180 LOC) + 1 modified method + ~2 serialization tweaks + 2 test files.
One commit per step; each step independently revertible.

---

## 13. Scope discipline — how this work stays inside the desired state

The risk in this kind of refactor is not the plan; it's an implementer "improving" past it
(building the deferred UI, adding scoping modes, refactoring neighbors while a file is open).
This is controlled by the *form* of `docs/M1_CHECKLIST.md`, not by exhortation:

1. **The checklist is the scope boundary.** It is a finite, ordered list of `- [ ]` boxes.
   *If a change is not a box, it is out of scope by definition.* "Deferred" never means
   "do a little of it."
2. **Self-healing, not improvising.** If a box is wrong or a detail is missing, the rule is
   *edit the checklist, then implement* — never code around a gap silently. A doc edit shows
   up in the diff and is reviewable; silent drift is not.
3. **Per-step fences.** Every step carries a "Done = exactly" line and a "Do NOT touch" line,
   plus a one-line `Revert`. The "exactly" closes the unstated space where escape happens.
4. **One machine-checkable gate.** Step 4's acceptance is *Telegram test output identical to
   the Step 0 baseline.* Behavior drift fails a test, not a vibe check.
5. **Surfacing, not deciding.** A strong reason to pull M2+ work forward is recorded under
   that step's `Notes` as a proposal to the operator — it is not a license to build it.

**To enforce this in an implementing session:** load `docs/M1_CHECKLIST.md` into context (e.g.
reference it from `CLAUDE.md` or paste its "How to use this doc" header at the top of the
working prompt) so the invariant — *M1 is an extraction + a doc, not a feature* — is always
present, not something to remember.
