# AGENT 18 — Worker Affinity Fallback: offline pinned node hard-ERRORs the session (no controlled fallback)

**Dispatch created:** 2026-07-05
**Owner:** build agent (Horse) in relay-cooperation with the gateway agent (kanebra).
**Branch to cut:** `fix/worker-affinity-fallback` off `main`
**Level:** 2 build, with **one Level-3 policy fork** (see §Operator decision) — do NOT
implement a fallback policy that runs a pinned turn on another host without operator sign-off.
**Theme:** A11 correctly closed the *silent local fallback* (a remote-pinned session must never
execute on the wrong host — `backend_session_id` is machine-local). But it left the **offline
path undefined**: when the pinned worker is down, the gateway **immediately hard-fails** the
turn and drives the session to `ERROR` with `retries=0`. A transient worker outage (e.g. Horse
rebooting for 30 s) therefore *permanently kills an otherwise-healthy session* with no wait, no
requeue, and no operator recovery affordance. This packet defines the **controlled** fallback
policy for the offline-pinned-worker case — the third path between "run local" (wrong) and
"instant ERROR" (current) — without reopening the A11 correctness hole.

> **Test cost guard (READ FIRST).** Design + implementation here is DB reads, a config check,
> unit tests, and code review — **NO paid CLI turn** is required. Only a final optional
> re-validation would submit ONE turn (reuse the A10 §T1 procedure, pinned to Horse, with Horse
> deliberately offline then online). Never loop it, never run the full e2e suite, never run
> `python main.py status` (kills the live gateway). Check the live gateway with
> `curl -s 127.0.0.1:9003/health`.

---

## Evidence (current behavior on `main`, code-grounded 2026-07-05)

Routing decision — `src/orchestrator.py:2242`:

```python
_host = socket.gethostname()
_pinned_elsewhere = bool(session and session.machine_id and session.machine_id != _host)
route_remote = bool(config.mesh.enabled and _pinned_elsewhere)

if _pinned_elsewhere and not route_remote:      # A11 guard — refuse local execution
    logger.error("event=affinity_unrouted ...")
    self._emit_event("affinity_unrouted", task, {...})
    if config.mesh.enabled:
        route_remote = True                     # honor the pin via the remote path
    else:
        last_result = TaskResult(success=False, errors=[".. mesh disabled .."])  # honest fail
```

Remote path, offline branch — `src/orchestrator.py:2792`:

```python
if not node_online:
    result = _routing_failure(f"Node {session.machine_id!r} is offline; cannot continue "
                              f"session (no local fallback — affinity is required)")
    session.status = SessionStatus.ERROR      # <-- terminal; session is now dead
    self.session_store.save(session)
    result.error_class = self._classify_error(result)
    result.retries = 0                         # <-- no retry, no requeue, no hold
    return result
```

Node liveness is already checked in two places (in-memory registry → DB fallback,
`orchestrator.py:2776-2790`), so "is the pinned node online right now" is a cheap, reliable
signal. The task-server claim model already enforces affinity at claim time
(`src/control/db.py:1029` — `machine_id=NULL` ⇒ any node; `machine_id=<node>` ⇒ that node only;
`task_server.py:491` `accept_unpinned`). So the mesh layer *can* hold a pinned task pending a
claim — the gateway just doesn't give it the chance; it fails synchronously the instant the
node reads offline.

**Gap in one sentence:** there is no bounded "wait for the pinned node to come back / requeue"
state — offline at dispatch time == permanent session death.

---

## The contradiction, from three standpoints

**Dev / correctness.** A11's invariant is non-negotiable and MUST be preserved: *a pinned turn
executes on its pinned host or not at all — never on a substitute host.* `backend_session_id`
continuity (Claude/Codex/OpenCode native session resume) is machine-local; running turn N on a
different box silently forks the conversation. Any fallback that moves a pinned turn to another
host is a **correctness regression**, not a feature. (This is exactly the class of bug A11 was
created to kill — see `AGENT_11_MESH_AFFINITY_ROUTING.md`.)

**Architecture.** `CONTEXT.md:206` ("the gateway keeps its own embedded worker capacity that
runs tasks when no remote node is available; prefer remote nodes when online") is written for
**unpinned** work and must be read that way. The doc never states the pinned/unpinned split
explicitly — that silence is what let the A11 bug exist. **Deliverable includes making the
distinction explicit in the architecture rules**, so "gateway has local capacity" can never
again be misread as license to break affinity. Two task classes, two policies:
- **Unpinned** (`machine_id IS NULL`): may run anywhere, local capacity is a legitimate
  fallback. *No change.*
- **Pinned** (`machine_id = <node>`): host-or-nothing. Fallback means **wait/requeue/re-pin**,
  never **relocate**.

**Application / operator UX.** Today a 30-second worker blip converts to a dead `ERROR` session
the operator must notice and manually rebuild. That is brittle and opaque. The application-level
fix is: distinguish *transient* (node will likely be back) from *terminal* (node gone / retries
exhausted), hold briefly on transient, and when it does become terminal, surface an **honest,
actionable** state — "pinned node `Horse` offline, session paused, [retry] / [re-pin to another
node]" — instead of a bare `ERROR`. This mirrors the existing honest-state posture
(`task_state_truth.py`, the #39 honest worker/session reporting work already shipped).

---

## Design options

**Option A — Bounded hold-and-requeue (RECOMMENDED).**
When the pinned node is offline at dispatch: do **not** immediately ERROR. Instead enqueue the
task to the mesh pending table (it's already affinity-scoped so only `Horse` can claim it) and
enter a bounded wait: `PAUSED_PINNED_NODE_OFFLINE` for up to `AFFINITY_OFFLINE_GRACE_SEC`
(default e.g. 120 s), polling node liveness. If the node re-registers within the grace window,
the pinned worker claims and runs the turn normally — the blip is invisible to the operator. If
the window expires, transition to a terminal-but-honest state (below) with `retries` reflecting
the wait, and emit `event=affinity_offline_timeout`. **No turn ever runs off-host.** Preserves
A11 exactly; adds resilience to transient outages only.

**Option B — Immediate honest-fail + operator re-pin affordance (minimum viable).**
Keep the immediate failure but replace the terminal `ERROR` with a distinct, resumable
`PINNED_NODE_OFFLINE` session state and an explicit event, and expose a control-API/Web action to
**re-pin** the session to an online node (operator-initiated; re-pin is honest — it starts a
*new* native backend session on the new host, clearly marked as a continuity break). Smaller
blast radius than A; no bg-wait machinery; but every blip still interrupts the operator.

**Option C — Do nothing / document only.** Reject: leaves the brittleness in place; the only
merit is zero code risk, which §Test-plan already gives us via flag-gating.

**Recommendation: Option A, flag-gated OFF by default, with Option B's honest terminal state as
its expiry behavior.** A subsumes B: A's timeout path *is* B's honest-fail + re-pin affordance.
Ship them together. Gate the whole thing behind `AFFINITY_OFFLINE_GRACE_SEC` (0 ⇒ current
byte-identical A11 behavior: immediate fail). That makes the change **fully reversible by config**
and keeps `MESH_ENABLED=false` and unpinned paths byte-identical.

---

## Recommended policy spec (Option A)

Decision table at the offline branch (`orchestrator.py:2792`), pinned session, mesh enabled:

| Condition | Action | Session state | Event |
|---|---|---|---|
| Node online | dispatch remote (unchanged) | `BUSY` | `mesh_dispatch` |
| Node offline, `grace=0` | fail now (A11 legacy) | `ERROR` | `mesh_routing_failed` |
| Node offline, within grace | requeue affinity-scoped + hold, poll liveness | `PAUSED_PINNED_NODE_OFFLINE` | `affinity_hold_started` |
| Node returns within grace | worker claims + runs | `BUSY`→`IDLE` | `mesh_dispatch` |
| Grace expires | honest terminal + re-pin affordance | `PINNED_NODE_OFFLINE` (resumable) | `affinity_offline_timeout` |

Invariants (assert in review): a pinned turn's `execution_node_id` is **always** its
`machine_id` or the turn does not execute; local worker pool never claims a pinned task
(already true via `db.py:1029` claim filter — add a defense-in-depth assertion, don't rely on
it alone); `MESH_ENABLED=false` and unpinned sessions unchanged.

---

## Scope guard

Routing/session-state/observability + the two-class architecture doc note only. **Do NOT**
touch telemetry adapters, the turn schema, backend drivers, or the claim SQL semantics. Keep
local (unpinned) and `MESH_ENABLED=false` paths byte-identical. `grace=0` ⇒ byte-identical to
today's A11 behavior.

---

## Test plan (no paid CLI)

1. `test_pinned_offline_grace_zero_fails_immediately` — `grace=0` reproduces current A11 ERROR
   path exactly (regression lock).
2. `test_pinned_offline_within_grace_holds_then_dispatches` — node offline at dispatch, comes
   online before expiry → task claimed by the pinned node, never by local pool.
3. `test_pinned_offline_grace_expires_honest_terminal` — node stays offline → session ends in
   `PINNED_NODE_OFFLINE` (resumable), `affinity_offline_timeout` emitted, NOT bare `ERROR`.
4. `test_local_pool_never_claims_pinned_task` — assert the claim filter + defense-in-depth guard.
5. `test_unpinned_unchanged` / `test_mesh_disabled_unchanged` — byte-identical legacy paths.
6. Run `pytest tests/test_session_service*.py tests/test_control_api.py
   tests/test_mesh_dispatch_timeout.py -q` and the new file. No `--run-e2e`.

Optional T-final (ONE paid turn, operator-scheduled): A10 §T1 smoke pinned to Horse with Horse
stopped for < grace then restarted → turn lands on Horse (`execution_node_id=Horse`), no
`affinity_unrouted`, no off-host execution. Only after that: mark PASSED, update `CONTEXT.md`
+ `DISPATCH_LOG.md`.

---

## Operator decision (Level-3 fork) — needed before build

1. **Approve Option A** (bounded hold-and-requeue) vs **B** (immediate honest-fail + re-pin only)
   vs **park (C)**.
2. **`AFFINITY_OFFLINE_GRACE_SEC` default** — proposed 120 s; 0 preserves today's behavior.
3. **Re-pin semantics** — confirm that operator re-pin starts a NEW native backend session on the
   new host (honest continuity break, clearly labeled) and is never automatic.

No code lands until 1–3 are answered. This packet is the resolution artifact; implementation is
gated on operator sign-off per CLAUDE.md (routing change on a live production gateway = ask-first,
reversible-by-config required).

---

## Operator decision — recorded 2026-07-06

Operator directive: *"Follow agent 18 and start working on the issue, fix it the best
and most professional way."* Resolved the §Operator-decision fork as follows — the
maximally-reversible reading that lets code land now without changing any live behavior:

1. **Option A — bounded hold-and-requeue** (with Option B's honest terminal state as the
   expiry behavior). Approved as the RECOMMENDED design.
2. **`MESH_AFFINITY_OFFLINE_GRACE_SEC` default = `0` (DISABLED).** The feature ships OFF:
   a redeploy is byte-identical to today's A11 behavior until the operator opts in (e.g.
   `=120`). This satisfies CLAUDE.md's "routing change on a live gateway must be
   reversible-by-config" — no live behavior change on deploy. Poll cadence:
   `MESH_AFFINITY_OFFLINE_POLL_INTERVAL_SEC` (default 5s, clamped to the grace window).
3. **Re-pin is operator-initiated and never automatic.** The grace-expiry state
   `PINNED_NODE_OFFLINE` is honest + resumable; re-pinning to a new host starts a NEW
   native backend session there (labeled continuity break). A pinned turn is NEVER
   relocated automatically — the hold only ever waits for the *same* node to return.

> A single background process co-authored the routing block during build (relay with the
> gateway agent, per the header); the final code was reviewed line-by-line against this
> spec and the A11 invariant before tests.

## Milestone

- [x] Operator decision §1–3 recorded here
- [x] Branch `fix/worker-affinity-fallback` cut off `main` (working branch)
- [x] Decision-table implemented behind `MESH_AFFINITY_OFFLINE_GRACE_SEC` (default 0 ⇒ legacy)
      — `src/orchestrator.py:_process_task_remote`, `config/settings.py` (`MeshConfig`),
      new `SessionStatus.{PAUSED_PINNED_NODE_OFFLINE,PINNED_NODE_OFFLINE}` in `interfaces.py`
- [x] `CONTEXT.md` architecture rules: pinned/unpinned two-class distinction made explicit
- [x] Web surfacing: `sessionAdapter.ts` folds paused→running, terminal→failed_attention
- [x] Tests 1–6 green (no paid CLI) — `tests/test_affinity_fallback.py` (8 passed: the six
      spec cases + cancel-during-hold + defense-in-depth target-mismatch); regression sweep
      `test_mesh_dispatch_timeout / test_mesh_self_awareness / test_telemetry_mesh_integration /
      test_session_service{,_lifecycle} / test_control_api / test_claim_reaper /
      test_mesh_reconcile_spool` (104 passed total); web `adapters.test.ts` (21 passed); `tsc` clean
- [ ] Build-review folded (operator review)
- [ ] (optional) T-final one-turn re-validation, operator-scheduled
- [ ] Gateway redeploy on kanebra (operator) + `CONTEXT.md`/`DISPATCH_LOG.md` updated

## Closure

*(fill at ship)* **Root cause:** the mesh remote path had no bounded "wait for the pinned
node" state — offline-at-dispatch == permanent session death (`ERROR`, `retries=0`).
**What shipped (feature OFF by default):** a config-gated hold-and-requeue at the offline
branch of `_process_task_remote` — `PAUSED_PINNED_NODE_OFFLINE` while polling liveness
within the grace window, resolving to normal dispatch (`mesh_dispatch`) if the node
returns, else to the honest, resumable `PINNED_NODE_OFFLINE` (events
`affinity_hold_started` / `affinity_hold_resolved` / `affinity_offline_timeout`). A11
invariant preserved: the claim filter (`db.py:get_pending_tasks`) already keeps the local
pool from claiming a pinned task, and a defense-in-depth assert at the dispatch site fails
closed if the resolved target ever differs from the pinned host. **Verification:** 6 unit
tests + regression suites green, no paid CLI turn. **Deploy note:** ships inert; enable per
node with `MESH_AFFINITY_OFFLINE_GRACE_SEC=120` after operator review + optional T-final.
