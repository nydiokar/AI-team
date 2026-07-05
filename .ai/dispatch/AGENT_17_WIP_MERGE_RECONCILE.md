# A17 — Reconcile the `d1556ad` WIP-snapshot merge on `main`

> ⚠️ **TEST COST GUARD.** No paid Claude/Codex CLI in tests. Do **not** run the full
> e2e suite. Do **not** run `python main.py status` (it kills the live PM2 gateway).
> Check the running gateway with `curl http://127.0.0.1:9003/health`.

**Theme:** A "safety commit" (`d1556ad`, *"WIP snapshot before main merge"*) landed
**~593 lines across 17 files** on `main`, co-mingling the **locked A16 admission-block
scope** (legit, matches `AGENT_16_HARNESS_BLOCK_SURFACE.md`) with a pile of **orphan,
undispatched, unreviewed WIP** — including a **live** `_ActivityForwarder` class wired
into the mesh worker daemon (`src/worker/agent.py:896`). The docs
(`CONTEXT.md`/`DISPATCH_LOG.md`) mention **only A16** — they are silent on the orphan
code. This is the exact "co-mingled git indexes" hazard the harness's own driver rule 3
warns against, now sitting on `main` unreviewed.

**Level:** 2 (standard). This dispatch is an **audit + documentation reconcile**: it
reads code, runs `/code-review` on an already-committed diff, and writes docs. It
**changes no code behavior** and is fully reversible (git revert of doc commits).
The *remediation* of the orphan code (revert vs keep) touches worker/mesh code and is
therefore a **Level-3 follow-up requiring operator approval** — explicitly a non-goal
here (see `<non_goals>`).

```xml
<task_packet>
  <meta>
    <task_name>A17-wip-merge-reconcile</task_name>
    <harness_level>2</harness_level>
    <continues></continues>
  </meta>

  <objective_lock>
    <real_objective>`main`'s history is honest: every change that landed in commit
      d1556ad is accounted for — either as the reviewed A16 scope, or as an explicitly
      catalogued orphan cluster with a completeness/safety verdict and a keep-vs-revert
      recommendation. The operator can make one informed decision about the orphan code
      instead of discovering unreviewed live worker changes by accident.</real_objective>
    <literal_request>"This WIP that got merged is something to pay attention to. Are we
      to test out the task harness? How we do it properly? Carry it out."</literal_request>
    <interpreted_task>Run one real harness loop whose target is the d1556ad merge:
      (1) produce a git-grounded inventory of d1556ad splitting it into the A16-locked
      scope vs orphan clusters; (2) run /code-review on the COMMITTED orphan diff for
      correctness/safety; (3) for each orphan cluster, state whether it is wired/live or
      inert, whether it has tests, and whether it is complete; (4) reconcile the docs
      (this dispatch doc + DISPATCH_LOG row + CONTEXT ledger) so they reflect reality;
      (5) give a per-cluster keep-and-document vs revert recommendation. Change NO
      product code.</interpreted_task>
    <constraints>
      - CHANGE NO CODE under src/, config/, web/, tests/. Doc-only writes:
        this dispatch doc (.ai/dispatch/AGENT_17_*.md), .ai/dispatch/DISPATCH_LOG.md,
        .ai/CONTEXT.md. Nothing else may be edited.
      - Ground EVERY claim in git (git show / git log / grep / file read). Do not trust
        the A16 closure prose or any summary — verify against the tree on main HEAD.
      - Test Cost Guard: no paid CLI, no full e2e, no `python main.py status`.
      - Do NOT falsely "close" or mark "reviewed" any orphan cluster in the docs — an
        honest reconcile records that the code landed WITHOUT a dispatch/review, it does
        not retroactively legitimize it.
    </constraints>
    <non_goals>
      - Reverting, modifying, or "finishing" ANY orphan code. The orphan clusters touch
        worker/mesh + telemetry (Level-3 triggers); remediation is a SEPARATE Level-3
        dispatch that needs operator approval. This loop only makes the situation
        visible and assessed.
      - Modifying A16's shipped code (it is reviewed and closed).
      - Building anything new (no Phase-2 machinery, no packet validator).
      - Telegram surfacing / approval workflow (unrelated deferred work).
    </non_goals>
    <assumptions>
      - The A16-locked scope = `mark_idle` (src/services/session_service.py),
        `harness_level3_needs_approval:409` + both submit lanes wrapped
        (src/control/control_api.py), Composer copy (web/.../Composer.tsx),
        control-API tests (tests/test_control_api_write.py), apiClient unwrap
        (web/.../apiClient.ts). VERIFY each against d1556ad's diff, don't assume.
      - Everything else in d1556ad (config/models.py, src/control/db.py additions,
        src/control/task_server.py, src/core/observability.py,
        src/services/backend_usage.py, src/worker/agent.py, SystemScreen.tsx,
        BackendUsagePanel.tsx) is candidate ORPHAN scope — CONFIRM per file whether it
        belongs to A16 or is unrelated.
      - `_ActivityForwarder` is instantiated at src/worker/agent.py:896 (live on remote
        workers). VERIFY it is reachable and note its runtime guard, if any.
    </assumptions>
    <drift_risks>
      - Scope-creeping into fixing/reverting the orphan code (forbidden — it's Level-3).
      - Trusting the A16 closure summary instead of the actual diff.
      - Over-documenting: dumping paragraphs into DISPATCH_LOG (it is an INDEX — one
        line; detail belongs in this dispatch doc, per DOC_MAP).
      - Running a paid CLI or the e2e suite "to verify" the orphan code works.
    </drift_risks>
  </objective_lock>

  <approved_plan>
    <steps>
      1. INVENTORY. `git show d1556ad --stat` then per-file `git show d1556ad -- <path>`.
         Classify each of the 17 files: A16-scope | orphan-code | doc. Record a table in
         this doc's `## Milestone` → findings.
      2. VERIFY A16 on main HEAD. Confirm the A16-scope hunks match AGENT_16's closure
         (mark_idle wired, 409 map, both submit lanes, Composer). If they match, A16 is
         validated-as-shipped; note any drift.
      3. ASSESS each orphan cluster. For each: (a) wired/live or inert? (git grep for the
         symbol's call sites); (b) has tests? (grep tests/); (c) complete or half-built?
         (d) any runtime guard/flag gating it? Name the cluster (e.g. "activity-forwarder",
         "backend-usage-ext", "observability-ext").
      4. CODE-REVIEW the orphan diff. Run `/code-review` scoped to the orphan hunks of
         d1556ad (committed diff). Capture P0/P1 findings only. This is the harness's
         adversarial-review stage on a REAL code diff.
      5. RECONCILE DOCS (doc-only writes):
         - This dispatch doc: fill `## Milestone` (inventory table + per-cluster verdicts)
           and `## Closure` (honest summary + recommendation).
         - DISPATCH_LOG.md: append ONE row for A17 (Level 2, status `reviewed`), one line.
         - CONTEXT.md: add an honest note that d1556ad co-mingled orphan code onto main
           (in Current Focus or a short "Known drift" line) — NOT a Shipped-Ledger entry
           (it isn't cleanly shipped). Keep it factual.
      6. RECOMMEND. Per orphan cluster: keep-and-retroactively-dispatch (if it code-reviews
         clean, is complete, and is worth keeping) vs revert (if incomplete/unsafe/unwanted).
         Surface this as an operator decision — do not act on it.
    </steps>
    <validation>
      - `git show d1556ad --stat` reproduces the 17-file surface (inventory grounded).
      - Every orphan verdict cites a git command / file:line (no ungrounded claims).
      - `git status` shows ONLY the three doc files modified (proves "no code changed").
      - `/code-review` ran on the orphan diff; findings (or "none") recorded.
      - DISPATCH_LOG row is exactly one line; CONTEXT note is factual, not a false close.
    </validation>
    <definition_of_done>
      - Inventory table: all 17 files classified A16 | orphan | doc.
      - A16 scope verified present-and-matching on main HEAD (or drift noted).
      - Each orphan cluster has: wired?/tests?/complete?/guarded? + a /code-review verdict.
      - Docs reconciled honestly (dispatch doc + 1 DISPATCH_LOG row + CONTEXT note).
      - A clear per-cluster keep-vs-revert recommendation for the operator.
      - `git status`: only AGENT_17 doc, DISPATCH_LOG, CONTEXT changed. Zero code files.
    </definition_of_done>
    <risks>
      - Low. Read + review + doc writes only; reversible (revert doc commits). The one
        real hazard is scope-creep into code remediation — pinned shut by <non_goals>.
    </risks>
  </approved_plan>

  <execution_rules>
    <do>Ground every claim in git. Update this doc's `## Milestone` Live Log after each
      step. Keep DISPATCH_LOG to one line. Report in closure_summary shape with F-tag
      outcomes from the /code-review.</do>
    <do_not>Touch any file under src/ config/ web/ tests/. Run a paid CLI or the e2e
      suite. Run `python main.py status`. Revert or "fix" the orphan code. Mark orphan
      work "reviewed/closed" as if it went through the harness.</do_not>
    <report_format>closure_summary.md shape: what you found (inventory + verdicts),
      verification commands + results, /code-review F-tag outcomes, the per-cluster
      recommendation, and what follows (the Level-3 remediation fork for the operator).</report_format>
  </execution_rules>

  <context_snippets>
    <snippet id="S1" source="git show d1556ad --format=%B">
      <quote>feat(harness): A13 admission-block surfacing — WIP snapshot before main merge.
        Safety commit preserving in-progress work exactly as it stood locally before
        merging origin/main.</quote>
      <why_relevant>Confirms d1556ad was a safety snapshot, not a scoped change — the
        source of the co-mingling. Its own message admits a "follow-up commit" was owed.</why_relevant>
    </snippet>
    <snippet id="S2" source="src/worker/agent.py (main HEAD)">
      <quote>class _ActivityForwarder: (l.789) ... self._activity_forwarder =
        _ActivityForwarder(self._http, self.cfg.node_id) (l.896)</quote>
      <why_relevant>The largest orphan cluster is WIRED and live on remote workers — a
        worker/mesh (Level-3) surface that landed unreviewed. Highest-attention cluster.</why_relevant>
    </snippet>
    <snippet id="S3" source="docs/harness/manager_invocation.md rule 3">
      <quote>Two workers on one tree co-mingle git indexes (this actually happened: A12
        committed A11's work). Never carry another loop's unmerged edits onto your branch.</quote>
      <why_relevant>This dispatch exists because that exact rule was violated; the audit
        is the harness applied to its own scar.</why_relevant>
    </snippet>
    <snippet id="S4" source=".ai/DOC_MAP.md">
      <quote>DISPATCH_LOG stays an index — one row per dispatch. Forward priorities +
        current focus live in CONTEXT. A dispatch's blow-by-blow lives in its dispatch doc.</quote>
      <why_relevant>Tells the executor WHERE each reconcile write goes — prevents dumping
        detail into the wrong surface.</why_relevant>
    </snippet>
  </context_snippets>
</task_packet>
```

---

## Milestone

**Status:** reviewed (executor complete — awaiting operator decision on orphan clusters)

**Burndown:**
- [x] 1. Inventory d1556ad — classify all 17 files (A16 | orphan | doc)
- [x] 2. Verify A16 scope present-and-matching on main HEAD
- [x] 3. Assess each orphan cluster (wired? tests? complete? guarded?)
- [x] 4. `/code-review` the orphan diff — P0/P1 findings
- [x] 5. Reconcile docs (this doc + DISPATCH_LOG A17 row + CONTEXT note)
- [x] 6. Per-cluster keep-vs-revert recommendation for the operator

**Live Log:**
- 2026-07-05 — Manager drafted packet (Level 2) + self-reviewed (F-tags below), locked, appended DISPATCH_LOG row. Grounding already done by Manager: A16 scope confirmed on main (`mark_idle` @ session_service.py:194, `409` @ control_api.py:58/789/803); orphan `_ActivityForwarder` confirmed wired @ agent.py:896; stale A13 sibling docs already renumbered to A16 (not on main).
- 2026-07-05 — Executor ran the audit. `git show d1556ad --stat` reproduced the 17-file surface (593 insertions). All 17 files classified (table below). A16 scope re-verified against main HEAD (not the closure prose). Six orphan clusters assessed with call-site + test grounding. P0/P1 review of the committed orphan diff done (no P0/P1; one P2/observation each on two clusters). Docs reconciled honestly. `git status` clean of code files (only the 3 doc files touched).

**Blockers:** none.

### 1. Inventory of `d1556ad` — all 17 files classified

`git show d1556ad --stat` → 17 files, 593 insertions(+), 36 deletions(-). Classification:

| # | File | Class | Cluster | Evidence |
|---|---|---|---|---|
| 1 | `src/services/session_service.py` | **A16** | admission-block | `mark_idle` @ :194 — inverse of `mark_busy` (`git grep -n "def mark_idle"`) |
| 2 | `src/control/control_api.py` | **A16** | admission-block | `harness_level3_needs_approval:409` @ :58; `_harness_blocked_http` @ :69; both submit lanes wrapped @ :789/:803 |
| 3 | `web/src/components/timeline/Composer.tsx` | **A16** | admission-block | `blockedMessage` on 409 @ :101; `ApiError` import @ :4 |
| 4 | `tests/test_control_api_write.py` | **A16** | admission-block | `block_task_id` stub + 2 tests @ :45/:173/:191 |
| 5 | `web/src/transport/apiClient.ts` | **orphan** | backend-usage-ext | Diff adds `usage_aggregation?: string` to `BackendUsageRow` @ :465 — NOT the A16 "apiClient unwrap" the packet assumed; it belongs to the backend-usage cluster. (The A16 unwrap path already existed pre-d1556ad; no apiClient change was needed for A16.) |
| 6 | `config/models.py` | **orphan** | models-default | Flips Claude default `sonnet`→`opus` (`is_default=True` moved). 2-line behavior change. |
| 7 | `src/control/db.py` | **orphan** | mesh-fleet-count | `_count_fleet_nodes` + `_NODE_FLEET_RETENTION_SEC` @ :2047/:2050; `_mesh_load_stats` tz-aware fix; wired @ :1918 |
| 8 | `src/control/task_server.py` | **orphan** | activity-forwarder | `ActivityPayload` @ :174 + `POST /events/activity` @ :301 (gateway receive-side) |
| 9 | `src/core/observability.py` | **orphan** | activity-forwarder | `register_event_forwarder` @ :52; best-effort fan-out in `emit_event` @ :361 |
| 10 | `src/services/backend_usage.py` | **orphan** | backend-usage-ext | `_CUMULATIVE_TOKEN_BACKENDS={"codex"}` @ :42; `_aggregate_usage(...cumulative=)` @ :66; `usage_aggregation` field @ :112 |
| 11 | `src/worker/agent.py` | **orphan** | activity-forwarder | `_ActivityForwarder` @ :789; wired live @ :896 via `_setup_activity_forwarding` @ :876 |
| 12 | `web/src/components/system/BackendUsagePanel.tsx` | **orphan** | backend-usage-ext | `usageParts()` @ :25; " peak" suffix on `usage_aggregation` @ :65 |
| 13 | `web/src/screens/SystemScreen.tsx` | **orphan** | mesh-fleet-count | Reworded "scheduler-invisible" banner → "online … not reporting live state" @ :377 |
| 14 | `.ai/CONTEXT.md` | **doc** | — | Stale A13/PR#8 ledger prose (later superseded on main) |
| 15 | `.ai/dispatch/DISPATCH_LOG.md` | **doc** | — | Stale A13 row (later renumbered to A16) |
| 16 | `.ai/dispatch/AGENT_13_HARNESS_BLOCK_SURFACE.md` | **doc** | — | The A16 packet under its old A13 name; renamed to `AGENT_16_*.md` on main |
| 17 | `.ai/dispatch/AGENT_13_HARNESS_BLOCK_SURFACE.milestone.md` | **doc** | — | Old sibling `.milestone.md` (violates one-dispatch-one-file; folded into AGENT_16 on main) |

**Summary:** 4 A16-scope + 9 orphan-code + 4 doc. (The `d1556ad` commit's own subject line calls it "A13" — that dispatch number was later reconciled to **A16** on main; the code hunks are the same admission-block scope.)

### 2. A16 scope verified on main HEAD (not the closure prose)

All four A16 files present-and-matching on `main` HEAD, at the Manager-cited lines:
- `git grep -n "def mark_idle" -- src/services/session_service.py` → `:194`
- `git grep -n "harness_level3_needs_approval" -- src/control/control_api.py` → `:58` (status map) + `:76`
- both submit lanes wrapped: `except HarnessAdmissionBlocked` @ `:789` (session lane, followed by `mark_idle` @ :791) and `:803` (one-off lane)
- `HarnessAdmissionBlocked` raise-source exists: `src/orchestrator.py:44`
- Composer: `blockedMessage` on `status === 409` @ Composer.tsx:101–102, rendered @ :123
- control-API tests: `block_task_id` stub + `test_blocked_oneoff...409` + `test_blocked_session...reverts_busy_to_idle` @ test_control_api_write.py:45/173/191

**Verdict: A16 is validated-as-shipped. No drift** between the committed d1556ad hunks and main HEAD for the admission-block scope. (The only "drift" is documentary: the dispatch number A13→A16 rename, which main already reflects.)

### 3. Orphan cluster assessment (grounded)

**Cluster A — `activity-forwarder`** (src/worker/agent.py, src/control/task_server.py, src/core/observability.py)
- **Wired/live?** YES — live on remote workers. `_ActivityForwarder` instantiated @ agent.py:896 inside `_setup_activity_forwarding()` (called from `WorkerAgent.__init__` @ :876); registers into `observability.register_event_forwarder` @ :898; the gateway receive-side `POST /events/activity` is a live route @ task_server.py:301.
- **Guarded?** Runtime-guarded by **co-location detection**, not a feature flag: forwarding is skipped when the controller host resolves to loopback / own tailscale IP / hostname (agent.py:878–896). So it only activates for genuinely remote workers. `emit_event` fan-out is best-effort (try/except, drops under backpressure) — cannot break local NDJSON logging (observability.py:359–366).
- **Tests?** NONE. `git grep "_ActivityForwarder\|events/activity\|register_event_forwarder" -- tests/` → no matches. Zero coverage on a live worker/mesh path.
- **Complete or half-built?** Structurally complete (bounded queue, daemon thread, double-emit guard, receive-side validation requiring session_id|task_id). But no test proves the end-to-end pill actually updates, and it is untested on the remote path.
- **Contract sanity:** `_HTTP.post` accepts `timeout=` (agent.py:333) so `post("/events/activity", body, timeout=3)` is valid.

**Cluster B — `backend-usage-ext`** (src/services/backend_usage.py, web/.../BackendUsagePanel.tsx, web/.../apiClient.ts)
- **Wired/live?** YES — `_aggregate_usage` replaces `_sum_usage` @ backend_usage.py:141 in the live `build_backend_usage` path (feeds `/api/backends/usage`); the UI reads `usage_aggregation` and shows a " peak" suffix + per-part cache breakdown.
- **Guarded?** No flag. Behavior change is data-driven: only `codex` is in `_CUMULATIVE_TOKEN_BACKENDS` (frozenset @ :42), so Claude/others keep additive `sum`; only Codex switches to `max`/peak.
- **Tests?** NONE for the new behavior. `test_backend_usage.py` never asserts `usage_aggregation`/peak/cumulative — it only checks null/no_data (:74–76). The `test_aggregate_usage_*` hits in tests/ are false positives (telemetry-projection concept, not this helper).
- **Complete or half-built?** Complete + self-documenting (author flags it a "stopgap keyed by backend name" pending durable `counter_semantics`). Corrects a real bug (Codex cumulative counters were being summed → "166,700,822 tok" nonsense).

**Cluster C — `mesh-fleet-count`** (src/control/db.py, web/.../SystemScreen.tsx)
- **Wired/live?** YES — `_count_fleet_nodes` wired @ db.py:1918 in `stats()`; `_mesh_load_stats` now skips offline nodes and is tz-aware. `nodes` is in scope (defined @ :1905 via `self.list_nodes()`), so no NameError.
- **Guarded?** No flag; unconditional behavior change to `nodes_total`/`nodes_online`/slot aggregation.
- **Tests?** No NEW coverage. `test_mesh_health_samples.py:34` asserts `nodes_total == 1` but with a single **online** node — passes trivially under both old and new code; it does NOT exercise the offline-retention path in `_count_fleet_nodes`.
- **Complete or half-built?** Complete; fixes two real bugs: (1) the tz-aware/naive `utcnow()` subtraction that raised TypeError and silently marked every fresh online node stale (zeroing slots), and (2) dead inventory inflating "N/M online". The riskiest orphan by blast radius (touches live mesh scheduling visibility) but the tz fix is a genuine correctness win.

**Cluster D — `models-default`** (config/models.py)
- **Wired/live?** YES — `BACKEND_MODELS["claude"]` default flips `sonnet`→`opus`.
- **Guarded?** No. Immediate: new Claude sessions without an explicit model now default to **opus** (higher cost). Small diff, large operational/cost implication.
- **Tests?** N/A (config constant).
- **Complete?** Complete but is a **policy/cost decision**, not a feature — most likely an accidental local tweak snapshotted, not intended for main.

### 4. `/code-review` — committed orphan diff (P0/P1 focus)

Reviewed the isolated orphan diff (`git show d1556ad -- <9 orphan files>`, 532 diff lines). Correctness/safety pass:

- **No P0.** No crashes, data loss, injection, or auth bypass introduced.
  - `/events/activity` is auth-gated (`Depends(_require_auth)`, task_server.py:301) and validates `session_id|task_id` (422 otherwise).
  - `emit_event` fan-out is fully guarded (try/except, best-effort) — a forwarder fault cannot lose the local line or break the caller.
  - `_count_fleet_nodes` / `_mesh_load_stats` parse timestamps defensively (try/except per row, tz-normalize naive).
- **No P1.** The activity-forwarder's double-emit hazard is explicitly guarded (co-location skip), the queue is bounded, the thread is a daemon.
- **P2 / observations (not blocking, recorded for honesty):**
  - **[O1] activity-forwarder — zero tests on a live remote worker/mesh path.** Correctness rests on co-location detection and an untested end-to-end SSE re-emit. This is a Level-3 surface (worker/mesh) that landed with no coverage.
  - **[O2] backend-usage peak-aggregation & mesh-fleet-count — new branches untested.** Existing suites pass trivially (single-online-node / null-usage cases) without touching the new offline-retention or peak paths.
  - **[O3] `register_event_forwarder` is process-global "last registration wins."** Fine for one worker per process; if multiple `WorkerAgent`s were ever constructed in one process the last would silently win. Not reachable today (one worker/process), noted only.
  - **[O4] config/models.py opus-default is a cost/policy change riding in a "WIP snapshot"** — almost certainly unintended for main.

**No orphan cluster was code-reviewed as "clean-and-intended-for-main."** Each is technically well-formed but landed with **no dispatch, no review, and (A/B/C) no tests**.

### 6. Per-cluster keep-vs-revert recommendation (operator decision — NOT acted on)

| Cluster | Recommendation | Rationale |
|---|---|---|
| **C — mesh-fleet-count** | **Keep + retroactively dispatch** (add tests first) | Fixes a real tz-aware TypeError that was zeroing live slots; highest correctness value. But it's a live-mesh path with no new test — retro-dispatch to add the offline-retention + tz test, then bless. |
| **B — backend-usage-ext** | **Keep + retroactively dispatch** (add a peak-vs-sum test) | Fixes the Codex "166M tok" cumulative-sum bug; self-scoped to `codex`. Low blast radius. Wants one test asserting `usage_aggregation=="peak"` and max-not-sum. |
| **A — activity-forwarder** | **Retro-dispatch as its own Level-3** (do NOT keep silently) | Largest surface, live on remote workers, zero tests. It's the exact worker/mesh (Level-3) code the packet flags. Either formally dispatch + test + review it, or revert until it can be. Should not remain on main as unreviewed live worker code. |
| **D — models-default (opus)** | **Revert** (unless operator intends opus default) | A 2-line cost/policy flip snapshotted by accident. Reverting is trivial and safe; keeping silently changes default spend on every new Claude session. |

All four are an **operator fork**. Remediation (revert or retro-dispatch) touches worker/mesh + telemetry + config → a **separate Level-3 dispatch requiring operator approval**, explicitly out of scope for this audit (see `<non_goals>`).

---

## Closure

**What this audit establishes (honest reconcile — nothing "closed" that wasn't):**

`d1556ad` ("WIP snapshot before main merge") co-mingled two things onto `main`:
1. **The locked A16 admission-block scope** (4 files) — *legitimately reviewed and shipped* as A16; verified present-and-matching on main HEAD. No action needed.
2. **Nine files of orphan code across four clusters** (activity-forwarder, backend-usage-ext, mesh-fleet-count, models-default) that landed **without a dispatch, without review, and mostly without tests.** The live `_ActivityForwarder` (worker/mesh, Level-3 surface) is the highest-attention item; the opus-default flip is the most likely accident.

The orphan code is **technically well-formed** (guarded fan-out, bounded queue, defensive timestamp parsing, data-driven cumulative handling) and two clusters (B, C) fix **real bugs**. But well-formed ≠ blessed: this is exactly the "co-mingled git indexes" hazard `docs/harness/manager_invocation.md` rule 3 warns against, now sitting on `main` unreviewed. This audit makes it **visible and assessed**; it does **not** retroactively legitimize it.

**Recommendation (operator decision):** open a **Level-3 remediation dispatch** to (a) keep+test+retro-dispatch B and C, (b) formally dispatch/test/review or revert A, and (c) revert D unless opus-default is intended. Do not let the orphan clusters keep riding on main as un-owned code.

**This dispatch changed NO product code.** `git status` shows only the three doc files (`AGENT_17_WIP_MERGE_RECONCILE.md`, `DISPATCH_LOG.md`, `CONTEXT.md`). Reversible by reverting the doc commit.

**Follow-up = separate Level-3 (operator approval).** Not started here — that's the fork above.

---

## Manager self-review (F-tags on the packet, ≤2 rounds)

- **[F1] (P1 — scope leak) "reconcile the docs" could tempt the executor to
  retroactively write a clean closure for the orphan code, legitimizing unreviewed work.**
  Failure: orphan `_ActivityForwarder` gets a fake "reviewed/merged" row and nobody ever
  re-reviews it. *Fixed inline:* `<constraints>` + `<do_not>` now forbid marking orphan
  work reviewed/closed; step 5 says CONTEXT gets a factual "known drift" note, NOT a
  Shipped-Ledger entry.
- **[F2] (P1 — the deferred trap) the audit invites fixing the code it finds.**
  Failure: executor reverts the forwarder "to be safe," making a Level-3 worker change
  with no approval. *Fixed inline:* `<non_goals>` + `<do_not>` pin code untouched;
  remediation is an explicit operator fork; validation greps `git status` for zero code files.
- **[F3] (P2 — verifiability) "assess completeness" is vibe-based.**
  Failure: executor asserts "looks complete" ungrounded. *Fixed inline:* step 3 + `<do>`
  force each verdict to cite a git command/file:line; validation requires it.
- **Round-2 re-review:** no new P0/P1. Locked (1 round, under the 2-cap).

_No unresolved findings spilled to non-goals._
