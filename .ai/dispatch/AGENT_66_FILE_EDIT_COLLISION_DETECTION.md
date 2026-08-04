```yaml
job_id: AGENT_66_FILE_EDIT_COLLISION_DETECTION
created_at: "2026-08-03T18:16:59.626024+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T18:16:59.626047+00:00"
```

# DISPATCH — A66 · File-edit collision detection for concurrent workers

**Level:** 3 (code) · **Type:** backend read-model + orchestrator seam (flag-gated) · investigation-first
**Authored:** 2026-08-03 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (independent). Overlaps the M4 parallel-executor track (A56/A57) — read its scope so this doesn't collide with it.

> **Read this first — why this packet exists.** The operator borrowed the idea from **hcom**
> (`github.com/aannoo/hcom`), a peer-to-peer agent bus. hcom ships **file-edit collision
> detection**: *"if two agents edit the same file within 30 seconds, both get notified"*
> (README "How it works"). Today in AI-Team the operator must **manually** tell the Manager to
> spawn each worker in a separate git worktree and merge afterward, so two concurrent workers
> don't clobber the same file. That's discipline, not a mechanism — one forgotten instruction
> and two workers silently corrupt each other's edits. This job asks: **should the gateway
> detect and surface same-file concurrent edits itself, and at which seam.**
>
> **This is NOT "port hcom."** hcom detects collisions by streaming per-tool file-edit *hook*
> events into its local SQLite and running a windowed query (`src/hooks/*.rs`). We already
> capture per-turn `files_changed`/`files_modified` in `mesh_tasks` and `TaskResult`. The
> reference is the **idea and the window shape**, not their Rust. Our transport (SDK persistent
> sessions + DB-canonical) is different and stays.

## INVESTIGATION FIRST (gate — do this before any feature code)
Do NOT assume a detector is needed as-built. Verify against OUR code and decide the real seam.
Produce a written finding (`docs/collision_detection_investigation.md`) answering:

1. **What we capture, and WHEN.** Trace where file-change data lands: `TaskResult.file_changes`/
   `files_modified` (`src/core/interfaces.py`), per-turn audit in `mesh_tasks` (migration 17),
   how `src/backends/claude_driver.py` derives changed files (post-turn parse vs live tool
   events), existing `flow_events`. **Decisive question:** is our file-change signal only at
   **end-of-turn** (post-hoc), or can we see edits **mid-turn** like hcom's hooks? This decides
   whether we can *prevent* a collision or only *report* one. State it honestly.
2. **Who can actually collide, today.** §7 concurrency reality-check: under what dispatch shapes
   do two workers run against the **same repo_path at the same time**? Pinned vs unpinned
   (CONTEXT.md two-class routing), warm workers (A48/A60), the M4 intra-task executor (A56/A57).
   If the only safe path today is worktree-per-worker, is the higher-value fix actually **making
   worktree isolation the default** rather than a detector? Compare and recommend.
3. **hcom's exact mechanism, from source.** Read the collision path in the clone (operator has it
   at `/tmp/hcom_inspect`; else `git clone --depth 1 https://github.com/aannoo/hcom`). Extract:
   the 30s window, that it's advisory-only (notify, never block), how it dedups, how it
   attributes the two editors. Note which properties map to a DB-windowed query over
   `mesh_tasks`/`flow_events` and which depend on their mid-turn hooks (which we lack).
4. **Seam decision (surface, do not guess — see RESERVED).** Detection as: a read-model query
   over already-captured data (post-turn, advisory) / an orchestrator pre-flight check at
   worker-claim time (overlap warning) / a real worktree-isolation default. Different blast
   radii. Recommend one; name the runner-up and why not.

## TASK (only after the gate; land as its own `feat/<slug>` branch + PR + self-merge)
Scope follows the seam decision. DEFAULT shape (advisory, additive, flag-gated):

1. **Detection** as a bounded read-model query: given repo_path + window, find sessions/tasks
   whose `files_changed` sets intersect within the window. Mirror the batched aggregation of
   `db.get_session_token_totals` — **no N+1**, hard limit, one bounded SQL. Window is a knob via
   the A62 `/api/flags` non-boolean registry (`COLLISION_WINDOW_SEC`) — justify the value from
   OUR turn cadence (turns are minutes, not seconds), don't cargo-cult hcom's 30s.
2. **Surface** where the operator/Manager already looks: an append-only `flow_events`
   `collision.detected` event (mirror the `review.*`/`task.*` emitters) and/or the Work/Case
   read-model, plus existing push subscriptions (`/api/push/subscribe`). Do NOT invent a new
   channel.
3. **Governance boundary (§7 + trust model).** Advisory by default (notify, never auto-kill).
   Enforcement (refuse overlapping dispatch, force worktree) is a SEPARATE flag defaulting OFF,
   routed through the existing dispatch/governor seam — never a new kill path. Preserve the
   canonical-system-of-record + control invariants (CONTEXT.md "Architecture rules").
4. **Flag OFF ⇒ byte-identical.** Zero added I/O on the default path.

## TYPE
Level 3 (code). Investigation artifact rides the feature branch, or lands docs-only on `main`
first if the recommendation is "don't build the detector, change worktree defaults." Don't dangle
a branch; don't merge over another loop's edits. Gateway restart only if merged code needs it.

## CONTEXT (reuse, don't reinvent)
- `src/core/interfaces.py` — `TaskResult.file_changes`/`files_modified`.
- `src/backends/claude_driver.py` — per-turn changed-file derivation (post-turn vs live).
- `src/control/db.py` — `mesh_tasks` per-turn audit (mig 17); `get_session_token_totals` = the
  batched, dedup'd, no-N+1 pattern to mirror.
- `flow_events`/`flow_links` (A25/A26) — append-only events + authoritative case membership;
  `review.*`/`task.*` emitters = the precedent for `collision.detected`.
- A62 `/api/flags` non-boolean registry — `COLLISION_WINDOW_SEC` home.
- `/api/push/subscribe` (`src/control/control_api.py`) — existing notification fan-out.
- CONTEXT.md two-class routing + A48/A60 warm workers + A56/A57 M4 executor — the collision-
  possible shapes.
- **hcom reference:** `src/hooks/*.rs`, README "How it works" (30s advisory notify). Clone:
  `https://github.com/aannoo/hcom`.

## ACCEPTANCE (proof, not vibes)
1. `docs/collision_detection_investigation.md`: four items answered from OUR code (esp. item 1 —
   mid-turn vs post-turn signal), plus seam recommendation + runner-up.
2. If a detector ships: a test builds two overlapping-file tasks in-window → exactly one
   `collision.detected` with both editors attributed; non-overlapping pair → none; the window
   knob changes the result. Bounded SQL asserted (no N+1).
3. Flag OFF ⇒ byte-identical (test or explicit diff argument).
4. `pytest` on touched modules only (TEST COST GUARD — never full/e2e).

## RESERVED DECISIONS (surface, do not guess)
- **R1 — detector vs worktree-default.** If the fix is "worktree-per-worker by default," that
  changes the dispatch contract — STOP and escalate with the trade-off; don't silently pick.
- **R2 — advisory vs enforcing.** Enforcement default OFF; enabling it is an operator decision.
- **R3 — window value.** Justify from our turn cadence or make it purely knob-driven.

## SCOPE OUT
- Porting hcom's hooks/PTY/transcript machinery (different transport).
- Auto-kill/auto-revert of a worker on collision by default.
- A new notification channel (reuse push + flow_events).
- Redesigning dispatch / DISPATCH_LOG / CONTEXT.
- This job's own dispatch files stay point-in-time records.

## TRAIL / EVIDENCE (fill at close)
- `docs/collision_detection_investigation.md` · detector tests (if built) · flag-OFF argument ·
  PR number · `results_ref` → DISPATCH_LOG row.

---
## Milestone (burndown)
- [ ] Investigation gate: four items answered from our code; seam recommended
- [ ] (if built) detection query + `collision.detected` event + tests
- [ ] flag-gated surfacing via push/flow_events; enforcement OFF
- [ ] flag-OFF byte-identical verified

## Closure (fill on completion)
(fill when executed)
