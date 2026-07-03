> ⏸ **DEFERRED 2026-07-03 (manager call).** Not in the v1 spec. A "human dashboard"
> is a §12 *optional adapter* (Required: no); an interactive/stage-driving graph is the
> §16 Phase-2 driver the spec says *"do not build speculatively."* The §16 trigger —
> the file-and-dispatch discipline proving insufficient — has not been tested yet.
> Un-defer ONLY after real tasks have run through the hand-driven loop and hit a concrete
> need (lost handoffs / no queryable cross-task status). Good idea, wrong week.
> See `../AGENT_11_HARNESS_SELFTEST.md`.

# WEBUI Harness Node-Graph (UI-7) — DEFERRED

**Level:** 2 (new UI surface, read-only, zero backend/orchestrator change)
**Branch:** stay on the current branch (`feat/task-harness`). **Do NOT create a new
branch.** All work is additive under `web/`; it shares the branch with the A9 gate it
surfaces. New branches for every dispatch fragment the history and are forbidden here.
**Depends on:** A9 gate only for the GATE node's *blocked* state wiring — build the graph
against fixtures first so this is not a hard block.

---

## STEP 0 — REVIEW THIS PACKET BEFORE EXECUTING (mandatory, do not skip)

Do NOT start building on read. First run a grounded, assumptions-free review of THIS
packet against the actual code, exactly as the harness REVIEW stage would:

1. Verify every primitive this packet names actually exists as described — the adapters
   (`taskAdapter`, `eventAdapter`, `approvalAdapter`, `nodeAdapter`), the domain files,
   the fixtures dir, the shell registration path, the `task_blocked` event + its payload
   (`orchestrator.py:1831`), and the 7 stages in `dispatch_pipeline.md:28-34`. Open them.
2. For each, confirm the packet's claim matches reality. If a named file/function/event
   is missing, renamed, or carries a different shape than assumed, that is a finding.
3. Check the packet's assumptions about the hot path / data source for each node — do
   NOT trust a node's data binding from its stage name (see the Trap section below).
4. Report findings as F-tags (P0 = would build the wrong thing / break; P1 = friction),
   with file:line evidence. If the packet is wrong, SAY SO with evidence — do not
   silently "fix" it by building around it, and do not invent problems that aren't there.

Only AFTER this review, execute the packet **taking the findings into account** — adjust
the plan where a finding warrants it, and note in your report what you changed and why.
This mirrors the A9 course-correction lesson: a packet naming a primitive is not proof
the primitive is on the hot path — verify before wiring.

---

## Intent

Build a **node-graph view of the harness pipeline** as a new WebUI screen. Each of the
7 canonical stages is a node; a task flows left→right through them. **Read-only now,
node contract designed so per-node "drive" actions bolt on later** (edit packet, run
review, approve/block gate). This is the human-driven, operator-gated form of the
Phase-2 driver — visible stages, not an autonomous loop.

The 7 stages are canonical — do not invent them. Source of truth:
`docs/harness/dispatch_pipeline.md:28-34`:

```
(1) DRAFT      intent + curated context → XML packet + milestone file
(2) REVIEW     adversarial pass → F-tagged P0/P1 findings
(3) FIX        inline resolution (≤2 rounds) → locked packet
(4) DISPATCH   write .ai/dispatch/<NAME>.md (+ optional .task.md)
(5) INGEST     submit_instruction / file_watcher → Task object   [CODE]
(6) GATE       Level-3 admission gate in _enqueue_task           [CODE]
(7) CLOSE      closure summary + milestone→closed + ledger update
```

Nodes 1-4 + 7 are **doctrine** (run by hand, no engine). Nodes 5-6 are **live code**.
The graph must visually distinguish these two zones (e.g. a "handoff into real code"
divider between DISPATCH and INGEST) so operators see where automation actually exists.

---

## Grounding — real primitives (verified against `main`, do not re-derive)

- Screens live in `web/src/screens/*.tsx`; register the new one the same way
  `SessionsScreen`/`SystemScreen` are registered (follow the existing shell wiring in
  `web/src/components/shell/`).
- **Data comes from existing adapters — no new backend, no WebSocket.** Polling posture
  is canonical (see `webui-frontend-backend-sync` memory). Reuse:
  - `web/src/transport/taskAdapter.ts` — task state / which stage a task is in
  - `web/src/transport/eventAdapter.ts` — `task_created`, `parsed`, `task_blocked`
    (the GATE `reason=harness_level3_needs_approval` event) drive node lighting
  - `web/src/transport/approvalAdapter.ts` — Level-3 approval state for the GATE node
  - `web/src/transport/nodeAdapter.ts` — only if you surface which mesh node executes
- Domain types in `web/src/domain/{models,status,events,transitions}.ts` — **extend
  these, don't fork**. Add a `HarnessStage` union + a stage-derivation function here so
  the mapping from Task/events → current stage is testable in isolation.
- Fixtures in `web/src/fixtures/` — add fixtures for a task at each stage + a
  GATE-blocked task, so the whole graph renders and is tested with zero live backend.

---

## What backend exists (verified — read before assuming "no API")

The `web/` app talks to a **real embedded Control API** (`src/control/control_api.py`,
Bearer `DASHBOARD_TOKEN`, mounted in the gateway). It already serves everything this
graph needs — and, for ONE node, a write path too:

- **Read (all nodes):** `/api/tasks`, `/api/events?since=` — poll these to derive stage
  + status. No new endpoint needed for the read-only graph.
- **Write (GATE node only):** `/api/approvals` + `/api/approvals/{id}/resolve` **already
  exist** (`apiClient.ts:292,306`; wired via `approvalAdapter.ts`). So the GATE node's
  approve/block control is NOT a new-backend project — it is wiring an endpoint that
  ships today.

**Consequence — match each node to the control surface that already exists:**
- **GATE node → INTERACTIVE in this dispatch.** When a Level-3 task is blocked, the node
  offers approve/reject via `approvalAdapter` → `/api/approvals/{id}/resolve`. This is
  the one control the whole harness hinges on and its API is already live.
- **Doctrine nodes (DRAFT/REVIEW/FIX/DISPATCH/CLOSE) → READ-ONLY, correctly.** They have
  no engine and no API (`dispatch_pipeline.md:15`) — do NOT fake buttons for them. Their
  `actions` stay empty; that is honest, not a limitation.
- **INGEST node → READ-ONLY.** Shows the Task landing; no per-node control.

This is the debug-lens AND the one real operator control, together — not a mockup.

## Node contract (the load-bearing design decision)

Define ONE `HarnessNode` interface now that read-only consumes and "drive" extends later:

```ts
interface HarnessNode {
  stage: HarnessStage;              // DRAFT | REVIEW | FIX | DISPATCH | INGEST | GATE | CLOSE
  zone: 'doctrine' | 'code';        // drives the divider + styling
  status: 'idle' | 'active' | 'blocked' | 'done';
  summary?: string;                 // e.g. "2 F-tags open", "Level-3 awaiting approval"
  actions?: HarnessNodeAction[];    // EMPTY in this dispatch; the extension seam
}
```

`actions` is populated ONLY on the GATE node in this dispatch (approve/reject via the
existing `/api/approvals/{id}/resolve`). It stays EMPTY on every doctrine node — those
have no backend to drive, so faking a button would lie about what exists. The contract
lets UI-8 add actions to more nodes later WHEN their backend exists, without reshaping
the graph.

---

## Scope — DO

1. `HarnessStage` type + `deriveStage(task, events)` in `web/src/domain/` (unit tested).
2. `HarnessNode` contract + read-only graph screen (7 nodes, left→right, doctrine|code
   divider, per-node status + summary).
3. GATE node lights `blocked` when a `task_blocked reason=harness_level3_needs_approval`
   event is present; shows approval state via `approvalAdapter`; and offers approve/reject
   wired to the EXISTING `/api/approvals/{id}/resolve` (reuse `apiClient` write path +
   its Idempotency-Key; surface `{ok:false, reason}` rejections as UI copy).
4. Fixtures for every stage + a blocked task; tests for `deriveStage` and node status.
5. Register the screen in the shell; match existing polling cadence.

## Scope — DO NOT
- No backend/orchestrator/DB change. No NEW endpoint (the GATE approve/reject uses the
  approvals endpoint that already exists — do not add another). No WebSocket.
- No action UI on doctrine nodes (DRAFT/REVIEW/FIX/DISPATCH/CLOSE) — no packet editor,
  no "run review" button. They have no backend; that is UI-8+ and only if an engine lands.
- No `flow_runs` table / stage column — doctrine + convention are the state
  (`dispatch_pipeline.md:15`). Deriving stage from existing Task/events is the point.
- Do not invent stages or reorder them.

---

## Definition of done
- New screen renders the 7-node graph from fixtures with no live backend.
- `deriveStage` unit-tested across all stages incl. GATE-blocked.
- `vitest` green; typecheck clean; no changes outside `web/`.
- Report the screen (screenshot via existing `screenshot-pages.cjs`) + test output.
- Commit on `feat/task-harness` (the current branch). Do not merge to `main` and do not
  open a new branch — operator reviews the layout before UI-8 (drive) is scoped.

---

## Trap to avoid (grounding discipline — this is why A9 needed course-correction)
Do NOT assume a stage's data source from the stage name. Verify which adapter/event
actually carries each stage's signal before wiring a node to it — e.g. GATE's blocked
signal is a `task_blocked` **event**, not a task-status field. Confirm the event name
and payload in `eventAdapter.ts` / the orchestrator emit (`orchestrator.py:1831`) before
lighting the node. Ground every node's data binding in the code, not in this packet's prose.
