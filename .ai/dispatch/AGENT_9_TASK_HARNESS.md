# AGENT 9 — Task Harness Workflow Kernel (v1)

**Dispatch created:** 2026-07-03
**Author:** adversarial pass over `docs/Task_harness_workflow.md` v0.4 → v0.5
(review: `.ai/dispatch/AGENT_9_TASK_HARNESS_REVIEW.md`)
**Branch to cut:** `feat/task-harness` off `main`
**Theme:** Stand up the v0.5 task-quality loop as **prompt-and-artifact
discipline** — templates, generators, and the Dispatch Pipeline — with **zero new
gateway state**. This is authoring + light tooling, not a workflow engine.

> **Test cost guard (READ FIRST).** Normal test command is plain `pytest`.
> Tests/flow must NOT invoke the paid Claude/Codex CLI. Never run the full e2e
> suite "to verify." Never run `python main.py status` (kills the live PM2
> gateway). Check a running gateway with `curl http://127.0.0.1:9003/health`.
> Real e2e is OpenCode-only (`AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`).

> **Read before editing:** `docs/Task_harness_workflow.md` (v0.5 — the spec you
> are implementing), this review's sibling `AGENT_9_TASK_HARNESS_REVIEW.md`, the
> reference dispatch `AGENT_8_OPERATOR_SIGNAL.md` (+ its `_REVIEW.md`) for the
> house F-tag/scope-guard/implementation-log style, `src/services/task_parser.py`
> and `src/services/file_watcher.py` (the `.task.md` auto-pickup primitive),
> `src/orchestrator.py::load_compact_context` (~:1661) and `_handle_new_task_file`
> (~:1673), and `.ai/CONTEXT.md` / `.ai/NEXT_TASKS.md`.

---

## The decision you are building against (do not relitigate)

v0.5 §0 locks it: **v1 adds ZERO new gateway state.** No `flow_runs` table, no
stage machine, no orchestrator changes to carry flow state. The XML task packet +
milestone file + the dispatch convention ARE the *workflow-orchestration* state. If
you feel the urge to add a migration or a `current_stage` column, STOP — that is
Phase 2 (§16) and is explicitly out of scope. The whole point of this dispatch is
that it is cheap and un-platformy.

> **CLARIFICATION — this does NOT sideline the database. Two different "states":**
> 1. **Conversation / task / artifact state stays DB-canonical** (`mesh_tasks`
>    ledger, migration 17 — spec §7). The harness's resume memory is
>    `load_compact_context(task_id)`, which reads *from the DB*. Nothing here
>    competes with or bypasses the DB system of record.
> 2. **Workflow-orchestration state** (`flow_run_id`, `current_stage`,
>    `plan_review`, a stage machine) is **not built at all in v1** — not "in files
>    instead of the DB," just *deferred* (§16), because the task model is
>    single-turn and the discipline hasn't yet proven it needs a flow engine.
>
> The `docs/harness/` files are **templates + prompt artifacts** (authoring
> material), NOT a state store. A milestone `.md` is a per-task scratchpad, not the
> system of record. So: DB = truth for work; files = the reusable *loop discipline*.

---

## Why these deliverables, in this order

Ranked so each rung is usable on its own and nothing depends on unbuilt gateway
state.

### T1 — Templates + Level rubric (LOW risk — ship first)

The loop is only as good as its artifacts. Produce the canonical, reusable files
the rest of the pipeline fills in. These are docs/templates, not code — safe,
immediately useful, and the substrate T2/T3 generate into.

### T2 — Generators (the DRAFT / REVIEW / CLOSE roles) (MEDIUM — ship second)

The prompts/skills that turn intent → packet, run the adversarial review → F-tags,
and produce the closure summary. These encode §14's pipeline steps 1–3 and 6–7.

### T3 — Dispatch Pipeline wiring + auto-pickup guard (MEDIUM — ship last)

Wire the end-to-end handoff onto the **existing** `.task.md` auto-pickup primitive,
and enforce the safety boundary: **Level 3 requires operator approval before
dispatch; auto-enqueue is allowed only for Level ≤ 2.**

---

## Execution plan

### T1 — Templates + Level rubric

Create under `docs/harness/` (new dir) — pure authoring, no code:

1. `packet_template.xml` — the §2.1 XML Task Packet skeleton with inline
   `<!-- guidance -->` comments per field (what "objective_lock" vs
   "literal_request" mean; how to phrase non-goals/drift-risks).
2. `milestone_template.md` — the §2.2 burndown file (Objective / Current Status /
   Burndown / Live Log / Blockers / Next Action). State the update rule at top:
   *"Executor updates this after every meaningful step; on resume this file +
   `load_compact_context(task_id)` is ground truth, not model memory."*
3. `level_rubric.md` — the §3 level selector as a short decision checklist. Lead
   with the **Level-3 triggers** (DB migration; security/secrets; mesh/worker;
   trading; agent-behavior/autonomy; destructive op; > ~5 files / service
   boundary) and "when in doubt, escalate one level." Include the **cost cap**
   (review off for Level ≤ 1; plan↔review loop ≤ 2 rounds).
4. `README.md` for `docs/harness/` — one screen: what the harness is, the level
   ladder, which template to use when, and a pointer to the spec.

   > **[F1] Do NOT add gateway state.** These are files. No migration, no
   > orchestrator edit, no `flow_runs`. If a template references "state," it means
   > the milestone file + `mesh_tasks` ledger that already exist.

**Verify:** the four files render; a human can pick a level and fill a packet from
them with no other context. No tests needed (docs). `git add` under `docs/harness/`.

### T2 — Generators (DRAFT / REVIEW / CLOSE)

Implement as **prompt artifacts / skills**, not services. Prefer authoring them as
reusable prompt docs under `docs/harness/generators/` (and, if a skill is the
right home, a thin skill that loads them). Each is a role from §4.

1. `draft_packet.md` — the DRAFT prompt ("text engine" role, §14 step 1): input =
   intent + curated `<context_snippets>` (§8) + level; output = a filled
   `packet_template.xml` + an initialized milestone file. It must curate snippets
   (small, source-tagged, relevance stated), never dump raw context.
   > **[F2] Memory = existing systems.** The DRAFT prompt pulls resume context from
   > `load_compact_context(task_id)` and file-memory (`MEMORY.md`), NOT a new
   > store. `<memory_entry>` (§7) is a file-memory *write format* only. Do not
   > build a memory service or an async-compression job.
2. `adversarial_review.md` — the REVIEW prompt (§14 step 2): challenge assumptions,
   find P0/P1, emit **F-tagged findings** in the house style (stable `[Fn]`,
   one-line defect, concrete failure scenario). Output feeds the inline FIX loop
   (step 3), **capped at 2 rounds** (§3 cost cap); unresolved items become explicit
   non-goals or logged risks — never silently dropped.
   > **[F3] Onboarding smoke is NOT here.** The provider/model smoke (§9) is
   > provider-onboarding only and cost-guarded — do NOT put a model smoke in the
   > per-task review prompt. Implementation review uses `/code-review` +
   > `/security-review` on the committed diff (§5), which cost nothing extra.
3. `closure_summary.md` — the CLOSE prompt (§14 step 7): what changed, what
   follows, F-tag outcomes (`fixed`/`accepted`/`no change needed`), and the
   `.ai/CONTEXT.md` / `.ai/NEXT_TASKS.md` update stub. Level-3 wiki is OPTIONAL and
   never a gate.
   > **[F6] No parser, no gate.** The XML packet is model-facing prose — do NOT
   > write a validator/parser for it. The wiki is optional; Markdown is source of
   > truth; closure never blocks on it.

**Verify:** dry-run each generator by hand on one real small task from
`.ai/NEXT_TASKS.md` (e.g. a doc tweak) end-to-end: DRAFT → a packet + milestone,
REVIEW → at least one plausible F-tag, CLOSE → a summary. No paid CLI. If any
generator is backed by a skill file, `--collect-only`/import-smoke it; do not
execute a paid backend.

### T3 — Dispatch Pipeline wiring + auto-pickup guard

Wire §14 onto the **existing** primitive; add the safety boundary. Minimal code,
only where a doc can't enforce behavior.

**Read:** `src/services/task_parser.py` (`.task.md` YAML-frontmatter format),
`src/services/file_watcher.py`, `orchestrator.py::_handle_new_task_file`.

1. `docs/harness/dispatch_pipeline.md` — the §14 runbook: DRAFT → REVIEW → FIX →
   DISPATCH (`.ai/dispatch/<NAME>.md` + optional `.task.md`) → EXECUTE (burndown +
   milestone + checkpoint commits) → CHECKPOINT review (§5) → CLOSE. Reference the
   auto-pickup path by file:function.
2. **Auto-pickup safety guard.** Confirm in code exactly where a `.task.md` becomes
   an enqueued task, then enforce: **auto-enqueue only for Level ≤ 2; Level 3
   requires the operator-approval stage first.** Prefer a *convention* (a required
   `harness_level` field in the `.task.md` frontmatter + a documented rule the
   dispatch prompt obeys). Only if a convention can't hold it, add a **minimal,
   flag-guarded** check in the pickup path that refuses to auto-enqueue a
   `harness_level: 3` file without an explicit `approved: true` field — smallest
   possible change, off by default, no behavior change when the field is absent.
   > **[F4] Sequential, not concurrent.** The checkpoint reviewer runs AFTER an
   > executor commit, against the committed diff — do NOT design a live tailer or
   > two agents on one working tree.
   > **[F5] Level is deterministic.** The pipeline selects level via
   > `level_rubric.md` triggers, not vibes; "when in doubt, escalate."
   > **[F1-again] No new gateway state.** If step 2 needs code, it is a *guard* in
   > the existing pickup path, not a flow table. Keep it under a feature flag so
   > `MESH_ENABLED`/default behavior is byte-identical when the field is absent.
3. Update `docs/Task_harness_workflow.md` §13 checklist to tick what shipped, and
   add a short "how to run the harness" pointer to `.ai/CONTEXT.md`.

**Verify:** if you added a pickup guard, a `pytest` unit test: a `harness_level: 3`
`.task.md` without `approved: true` is NOT auto-enqueued; a Level-2 file is; absent
field ⇒ unchanged legacy behavior. If it stayed convention-only, state that
plainly and show the doc rule. `curl http://127.0.0.1:9003/health` if a gateway is
up — do not restart it.

---

## Sequencing & guardrails

- Land T1 → T2 → T3 as separate commits/PRs on `feat/task-harness`. T1 is pure
  docs; T2 is prompt artifacts; T3 is docs + at most one tiny flag-guarded guard.
- **No new gateway state, no migration, no stage machine** (that is Phase 2, §16).
- No paid CLI in any stage. No memory service. No packet parser/validator. No live
  tailing reviewer. Wiki stays optional and un-automated.
- Every rung ends green: `pytest` targeted (only if you added code), and each doc
  is self-sufficient for a fresh agent.
- Auto-pickup of **Level 3** work is forbidden without operator approval.

---

## Definition of done

1. `docs/harness/` holds the four T1 files + T2 generators + T3 pipeline runbook.
2. A fresh executor can, from `dispatch_pipeline.md` alone, run intent → packet →
   review → dispatch → execute → checkpoint → close on a real small task.
3. The Level-3 auto-pickup guard holds (test if code; documented rule if
   convention).
4. `docs/Task_harness_workflow.md` §13 ticked; `.ai/CONTEXT.md` +
   `.ai/NEXT_TASKS.md` updated with a harness pointer and this dispatch's outcome.
5. Zero new gateway state; zero paid-CLI calls; default gateway behavior unchanged.

---

## Implementation log

### T1 — Templates + Level rubric — SHIPPED (2026-07-03)

Pure docs under `docs/harness/` (new dir), zero code. Files:
- **`packet_template.xml`** — §2.1 skeleton with inline `<!-- guidance -->` per
  field (real vs literal vs interpreted objective; non_goals/drift_risks as
  first-class), plus a `<meta><harness_level>` field that mirrors the rubric and,
  for a dispatched `.task.md`, must match the frontmatter. Header states plainly:
  nothing parses this — **no validator** (F6).
- **`milestone_template.md`** — §2.2 burndown (Objective / Current Status /
  Burndown / Live Log / Blockers / Next Action). **Update rule stated at top:**
  executor updates after every meaningful step; on resume this file +
  `load_compact_context(task_id)` is ground truth, not model memory.
- **`level_rubric.md`** — §3 as a deterministic checklist. **Leads with the
  Level-3 triggers** (migration/security/mesh/trading/autonomy/destructive/>~5
  files/service-boundary) + "when in doubt, escalate one level" + the cost cap
  (review off for Level ≤ 1; plan↔review loop ≤ 2 rounds; no paid-CLI verify).
- **`README.md`** — one screen: what the harness is, the level ladder,
  which-file-when table, cost guard, spec pointer.

**[F1] outcome — `no change needed` (honored):** every T1 file is a document. No
migration, no orchestrator edit, no `flow_runs`. Where a template says "state" it
means the milestone file + the existing `mesh_tasks` ledger.

**Verification:** four files render as markdown/xml; a human can pick a level and
fill a packet from them with no other context. No tests (docs). Commit 1.

### T2 — Generators (DRAFT / REVIEW / CLOSE) — SHIPPED (2026-07-03)

Prompt artifacts under `docs/harness/generators/` (not services). Files:
- **`draft_packet.md`** — §14 step 1 "text engine" role: intent + curated
  `<context_snippets>` + level → filled packet + initialized milestone. Explicitly
  curates snippets (small, source-tagged, relevance stated); never dumps context.
- **`adversarial_review.md`** — §14 step 2: challenge assumptions, P0/P1 only,
  F-tags in the house style (stable `[Fn]`, one-line defect, concrete failure
  scenario). Documents the inline FIX loop **capped at 2 rounds**; unresolved →
  explicit non-goal / logged risk, never dropped.
- **`closure_summary.md`** — §14 step 7: what changed / verification / F-tag
  outcomes / what follows, plus the `.ai/CONTEXT.md` + `DISPATCH_LOG.md` update
  stub. Level-3 wiki optional, never a gate.

**F-tag outcomes:**
- **[F2] `fixed`** — memory rule points ONLY at `load_compact_context` +
  file-memory (`MEMORY.md`); `<memory_entry>` framed as a *write format*. No memory
  store, no async-compression job.
- **[F3] `fixed`** — no per-task model smoke; `adversarial_review.md` states the
  provider smoke (§9) is onboarding-only and cost-guarded, and that implementation
  review uses `/code-review` + `/security-review` on the committed diff.
- **[F6] `fixed`** — no parser/validator; the packet is called out as model-facing
  prose; the wiki is optional and never blocks closure.

**Verification:** dry-ran the loop by hand on one real small task (this dispatch's
own §13 checklist tick — the worked example in `dispatch_pipeline.md`): DRAFT → a
packet + milestone, REVIEW → one plausible F-tag, CLOSE → a summary. No paid CLI;
no skill-backed executable to import-smoke. Commit 2.

### T3 — Dispatch Pipeline + auto-pickup guard — SHIPPED (2026-07-03)

- **`docs/harness/dispatch_pipeline.md`** — the §14 runbook, self-sufficient for a
  fresh executor: the seven steps, the auto-pickup primitive referenced by
  file:function (`file_watcher._is_task_file → _handle_new_task_file →
  task_parser.parse_task_file → _enqueue_task`), the `.task.md` frontmatter shape
  (incl. `harness_level` + `approved`), the guard truth-table, and an end-to-end
  worked example.
- **Auto-pickup guard (code, minimal, flag-guarded).** Convention alone can't
  *stop* a mis-declared file (the watcher enqueues any `*.task.md`), so per the
  packet's "only if a convention can't hold it" clause, added the backstop:
  `orchestrator.py::_harness_level3_allows_autopickup` (a `@staticmethod`), called
  in `_handle_new_task_file` between `parse_task_file` and `_enqueue_task`. A
  blocked file emits a `task_blocked` event and is left un-enqueued (re-writable
  with `approved: true`). Opt-in via `HARNESS_LEVEL3_GUARD`.
- **`docs/Task_harness_workflow.md` §13** ticked (all 9 items + the guard);
  **`.ai/CONTEXT.md`** got the harness pointer (Priorities row → built, a Shipped
  Ledger entry, a Key-files row, and a "how to run the harness" pointer).

**F-tag outcomes:**
- **[F4] `no change needed`** — checkpoint reviewer is documented as sequential on
  the committed diff; no live tailer, no two agents on one tree.
- **[F5] `fixed`** — the pipeline selects level via `level_rubric.md` triggers,
  not vibes; guard coerces `harness_level` deterministically.
- **[F1-again] `fixed`** — the guard is a pure pass-through when the flag is unset
  OR the field is absent OR the level is ≤ 2 OR unparseable; `MESH_ENABLED`/default
  behavior is byte-identical. No flow table.

**Verification (no paid CLI):**
- `pytest tests/test_harness_level3_guard.py -q` → **18 passed** (guard off ⇒
  allow; on: absent-field ⇒ allow, level ≤ 2 ⇒ allow, level 3 unapproved ⇒ BLOCK,
  level 3 approved ⇒ allow, unparseable ⇒ allow, falsey flag values ⇒ off).
- `pytest tests/test_compact_context_injection.py -q` → **11 passed** (adjacent
  `_handle_new_task_file` region unbroken).
- No gateway restart; `python main.py status` NOT run (Test Cost Guard). Commit 3.

**Definition-of-done check:** ✅ `docs/harness/` holds the four T1 files + 3
generators + the pipeline runbook; ✅ a fresh executor can run intent→close from
`dispatch_pipeline.md` alone; ✅ the Level-3 guard holds (18 unit tests + documented
convention); ✅ spec §13 ticked, CONTEXT.md + DISPATCH_LOG.md updated (note:
`.ai/NEXT_TASKS.md` no longer exists — used CONTEXT.md + DISPATCH_LOG.md per the
current doc layout); ✅ zero new gateway state; ✅ zero paid-CLI calls; ✅ default
gateway behavior unchanged.

**Operator follow-ups (not code):**
1. To enable the hard Level-3 boundary on a host, set `HARNESS_LEVEL3_GUARD=1`
   (e.g. in `ecosystem.config.js` env). Left OFF by default so this pass changes no
   running behavior; the convention is the primary control until then.
2. Adversarial build-review → `DISPATCH_LOG.md` A9H `built` → `reviewed`, then
   merge `feat/task-harness` → `main` → `merged`.
