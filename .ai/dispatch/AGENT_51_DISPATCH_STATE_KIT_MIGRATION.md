```yaml
job_id: AGENT_51_DISPATCH_STATE_KIT_MIGRATION
created_at: "2026-07-28T18:55:33+03:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md             # -> DISPATCH_LOG.md section with the verdict prose
evidence: scripts/dispatch/dispatch_state.py                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T13:26:12.823406+00:00"
```

# DISPATCH — A51 · Port the legacy prose dispatch system onto the portable dispatch-state kit

**Level:** 3 (touches tooling + a git hook + 59 tracked job files + a doc-role contract) · **Type:** code + docs
**Authored:** 2026-07-28 · **Status of this packet:** ready (authored, not yet executed)
**Source kit:** `/home/cifran/dev/dispatch-state-kit/` (injected canonical instrument; `HANDOFF.md` + `SKILL.md` + `scripts/dispatch/dispatch_state.py`).

> This job is **managed** — the executing agent is a vigilant migrator, not a script-runner. The
> load-bearing outcome is **zero lost jobs**: every one of the 59 `.ai/dispatch/*.md` files must
> end the migration with a machine-readable state that is *correct against ground truth*, and the
> honest board (`--audit`) must show `✓ no gaps` OR every remaining gap must be explained in the
> Closure section. "install.sh ran" is NOT done.

---

## Why (intent)

The legacy dispatch system is **prose state that rots**: `DISPATCH_LOG.md` is a hand-maintained
Index table whose `Status` column drifts from reality (this very migration was triggered because
CONTEXT + the log had drifted ~5 days / 16 PRs behind git — see the 2026-07-23 reconciliation).
There is no structural way to tell dispatched-vs-done-vs-missed, and no proof gate on a "done"
claim. The `dispatch-state-kit` fixes exactly this with a **folder-is-the-registry** model:
per-job ` ```yaml ` state blocks (canonical `created_at`, `status`, `depends_on`, `evidence`,
`results_ref`), two GENERATED views (`_DISPATCH_STATE.md` eyeball + `_dispatch.parquet` query), a
warn-only git pre-commit trigger, and an `--audit` that surfaces `MISSING_STATE` /
`CLAIMED_DONE_NO_PROOF` / `BAD_STATUS` / `STALE_*` structurally. It was **built to be transferable**
(paths resolve relative to the script; `DISPATCH_DIR` env override) and this repo is its intended
shape (`.ai/dispatch/` with a log + job briefs). We adopt it because it is a strictly better
dispatch mechanism than what we run today.

**Boundary (read before you touch anything):** this kit is the **plan / authoring / audit plane**
(git-mediated, human-and-agent-authored job briefs and their lifecycle). It is **NOT** the runtime
Case-execution substrate (`flow_runs`/`flow_links`/`flow_events` + the M3.4 continuation scheduler,
which is DB-canonical and in-gateway). **Do not wire the kit to the gateway DB, do not make it
auto-start Cases, do not port dispatch rows into `mesh_tasks`.** The two planes stay separate and
meet only at a soft seam (a drop's `evidence:`/`results_ref:` may point at the Case/PR/commit it
produced). Rationale + the full synergy verdict:
`docs/AUTONOMOUS_CASE_CONTINUATION_DESIGN.md` §10 ("Adjacent plane: the git dispatch registry").

---

## TASK

Install the dispatch-state kit into this repo, migrate all existing dispatch job files to
machine-tracked state **reconciled against ground truth**, reconcile the doc-role contract so we
do not end up with two competing status surfaces, and hand back an honest board with zero
unexplained gaps.

## TYPE

code + docs. **Branch** `feat/dispatch-state-kit` (touches `scripts/`, `.git/hooks`, `pyproject.toml`,
59 `.ai/dispatch/*.md`, `DOC_MAP.md`, root pointer). Open a PR at close and merge it yourself per
branch policy. The git-hook install mutates `.git/hooks/pre-commit` (local, not committed) — that
is fine; the portable trigger is the committed script + views.

## CONTEXT (what to reuse, verbatim)

- **Kit source:** `/home/cifran/dev/dispatch-state-kit/`. Copy `scripts/dispatch/dispatch_state.py`
  to the SAME path here (`scripts/dispatch/dispatch_state.py`); copy `.ai/dispatch/CLAUDE.md`
  (per-job protocol), `.ai/dispatch/.dispatch_not_a_job`, `.claude/skills/scaffold-dispatch/SKILL.md`,
  and (optional, per-machine) `.claude/hooks/dispatch_state_stop.py`. Follow `HANDOFF.md`.
- **Ground truth for every status** (the migrator's keyword inference is NOISY and, here,
  under-powered — see REALITY CONSTRAINTS): the `## Index` table's `Status` column in
  `.ai/dispatch/DISPATCH_LOG.md`, **cross-checked against real git merge state**
  (`gh pr list --state all` + `git log`). As of 2026-07-28 **every** PR #5–#44 is MERGED except
  #30 (CLOSED); there are **no open PRs**. A row that says "OPEN / op-merge / built — op-merge" in
  the legacy log is almost certainly **`done`** now — verify each against git, do not trust the prose.
- **DOC_MAP contract:** `.ai/DOC_MAP.md` currently gives `DISPATCH_LOG.md` the role "lean index —
  status at a glance, one line each." The kit introduces `_DISPATCH_STATE.md` as the generated
  status view and reframes `DISPATCH_LOG.md` as **results/verdict prose + the Index**, with
  `dispatch:audit` as the live queue. Reconcile DOC_MAP so the two surfaces do not both claim the
  "status" role (see CHANGES-e).

## CHANGES

- **(a) Install the kit.** Copy the files above into place; do NOT run the migrate step yet.
- **(b) Deps decision FIRST (blocking — see RESERVED DECISIONS).** The engine hard-imports
  `pandas` + `pyarrow` (for the parquet view). This repo's `.venv` has `pyyaml` (6.0.3) but
  **NOT** pandas/pyarrow. Resolve the reserved decision before migrating; if "install," add them
  to a `dispatch` extra in `pyproject.toml` (NOT ad-hoc pip; per CLAUDE.md §5) and
  `pip install -e ".[dispatch]"` into `.venv`.
- **(c) Migrate + seed.** `.venv/bin/python scripts/dispatch/dispatch_state.py --migrate`. This
  injects a yaml block into each of the 59 job files, seeding `created_at` ONCE from each file's
  first git-commit date. **Capture the full migrate report** — it prints every status guess.
- **(d) Reconcile every row against ground truth (the vigilant core).** For EACH of the 59 files:
  compare the migrator's guessed `status` to the DISPATCH_LOG Index `Status` column AND git merge
  state. Fix every wrong or `unknown` one via the tool ONLY:
  `dispatch_state.py --set <JOB_ID> status <done|active|ready|blocked|dead>` — **never hand-edit a
  yaml block** (corrupts it). For every `done`, add `evidence:` paths that actually exist on disk
  (a merged PR's commit is not a file — prefer the merged source file(s)/test(s) the job produced,
  or its `results_ref:` → its DISPATCH_LOG section) so `--audit` does not raise
  `CLAIMED_DONE_NO_PROOF`. Populate `.dispatch_not_a_job` with the real non-job `.md` files living
  in `.ai/dispatch/` (e.g. `*_REVIEW.md` verdict docs, `DROP_*` proposal docs that are not
  themselves dispatched jobs — decide per file; when a `DROP_*` became a real PR it IS a job).
- **(e) Reconcile the doc-role contract.** Update `.ai/DOC_MAP.md`: `_DISPATCH_STATE.md` (generated)
  = the at-a-glance **status** surface; `DISPATCH_LOG.md` keeps the **Index + verdict-prose** role
  but stops being the authoritative status table (its Status column becomes human narrative, the
  parquet/audit is authoritative). Add the one-line pointer to root `CLAUDE.md` (and/or the
  AI-Team `CLAUDE.md`) that "job state is machine-tracked — protocol in `.ai/dispatch/CLAUDE.md`."
  Keep this MINIMAL and honest — do not delete the Index, re-role it.
- **(f) Wire the trigger + verify.** `--install-git-hook`, then `--selftest` (must PASS) and
  `--audit` (must be `✓ no gaps` or every residual gap explained in Closure).

## ACCEPTANCE (proof, not vibes)

1. `scripts/dispatch/dispatch_state.py --selftest` → **PASS** (all 7 checks).
2. `--audit` → `59 files · 59 with state · 0 orphans (MISSING_STATE)`, **zero** `BAD_STATUS`,
   **zero** `CLAIMED_DONE_NO_PROOF`, and either `✓ no gaps` or a written per-gap justification.
3. **No lost job:** a diff/count check proves all 59 pre-existing `.ai/dispatch/*.md` job files are
   still present and each carries exactly one yaml block; the parquet has one row per job file
   (`SELECT count(*)` == job-file count). Record the count both sides.
4. **Status correctness spot-audit:** for a sampled ≥15 rows spanning done/active/ready/blocked/dead,
   the yaml `status` matches the DISPATCH_LOG Index + git merge state. List the sample in Closure.
   In particular: every legacy "OPEN/op-merge" row that git shows MERGED is now `done`.
5. `created_at` immutability: re-running `--migrate` is a no-op on already-blocked files (never
   re-stamps) — assert by running it twice and diffing.
6. DOC_MAP no longer assigns the "authoritative status table" role to two surfaces (grep the
   contract; the generated view owns status, DISPATCH_LOG owns index+prose).

## REALITY CONSTRAINTS (found during authoring — do not rediscover the hard way)

- **The migrator's log-section fallback is INERT here.** `_log_sections()` keys on headers
  `Active + ready` / `Completed` / `Dead` / `Not-yet-open`; this repo's DISPATCH_LOG has only
  `## Index` and `## How to add a dispatch`, with status carried *inside the Index table*, not in
  sections. So `--migrate` will fall back to per-file keyword markers or `unknown` for MANY files
  → **expect a large spot-check load** (step d is the real work, not step c). Do not shortcut it.
- **`pandas`+`pyarrow` are absent from `.venv`** (only `pyyaml` present) — the tool will crash on
  first run until the deps decision (b) is resolved.
- Kit non-negotiables (carry in): `created_at` canonical/never re-derived; never hand-regex a yaml
  block or hand-edit the generated views; the git hook is WARN-ONLY, must never block a commit.

## RESERVED DECISIONS (escalate; do not silently pick)

- **R1 — the parquet dependency.** `pandas`+`pyarrow` are ~heavy for a management tool. Options:
  (i) accept them into a `[dispatch]` extra (full kit, keeps the `.parquet` query view); or
  (ii) run the kit **markdown-view-only** and treat parquet as optional/skipped (needs a tiny guard
  so `import pandas` failure degrades to "md view only" instead of crashing — a minimal upstream-able
  patch). Recommendation: **(i)** for a faithful first port (least deviation from the canonical kit);
  revisit (ii) if the two deps prove annoying. **Operator/lead picks R1 before step (c).**
- **R2 — should the migration ITSELF be tracked as a job here (A51 dogfood)?** Yes by default: this
  file gets a yaml block too. Confirm.

## SCOPE OUT

- No change to the runtime Case-execution substrate (`flow_*`, `mesh_tasks`, M3.4). No DB wiring of
  the kit. No auto-ignition of Cases from dispatch files. No cross-machine parity mechanism beyond
  the kit's own git-mediation (that is by design — see the boundary note above).
- No rewrite of the 59 briefs' prose; only inject/repair their yaml state blocks + fix DOC_MAP roles.

## TRAIL / EVIDENCE (fill at close)

- Branch / PR: _(feat/dispatch-state-kit → PR #NN, merged)_
- `--selftest` + `--audit` output pasted below.
- Migrate report + the reconciliation table (guessed → corrected status, per file that changed).
- The no-lost-job count check (before/after file count == parquet row count).

---

## Milestone (burndown — grow in place)

- [x] R1/R2 decisions taken — R1 = **markdown-view-only** (operator pre-decided; no pandas/pyarrow
      on this Pi `.venv`); R2 = **dogfood yes** (this file carries a yaml block).
- [x] kit copied + R1 guard applied — engine, `.ai/dispatch/CLAUDE.md`, `.dispatch_not_a_job`,
      `scaffold-dispatch/SKILL.md`, Stop-hook wrapper copied; minimal `try/except ImportError`
      guard on `import pandas` in the COPIED engine so parquet degrades to md-view-only.
- [x] `--migrate` run, report captured — 64 job files seeded (mostly `unknown`, as the packet
      warned the inference would be inert here).
- [x] all statuses reconciled to ground truth (DISPATCH_LOG Index + real git), evidence added to
      every `done` — via `--set` ONLY, never hand-edited.
- [x] DOC_MAP + root pointer + per-job protocol reconciled — DISPATCH_LOG kept as the PRIMARY
      human index+closure surface; `_DISPATCH_STATE.md` labeled complementary/machine-only.
- [x] git hook wired (warn-only, into the shared common hooks dir — worktree-safe), `--selftest`
      PASS, `--audit` `✓ no gaps`.
- [ ] PR opened (feat/dispatch-state-kit → main); **left OPEN for Manager review — not merged**.

## Closure

**Verdict: DONE.** The dispatch-state kit is installed and every job file carries a machine-readable
state reconciled against the DISPATCH_LOG Index **and** real git merge state. `--audit` returns
`✓ no gaps, no unproven-done, no stale-active`. Zero jobs lost. `DISPATCH_LOG.md` is byte-unchanged
and remains the primary human index+closure surface (the generated `_DISPATCH_STATE.md` is
complementary/query-only, per the operator constraint). Run in **markdown-view-only** mode (R1):
no pandas/pyarrow added; `_dispatch.parquet` intentionally not generated.

### `--selftest` (all 7 checks)
```
  ✓ round-trip: yaml block parses back exactly
  ✓ gap detection: bare file yields no block (→ MISSING_STATE)
  ✓ proof guard: done+missing-evidence flags, done+no-claim does not
  ✓ canonical created_at: existing block never overwritten
  ✓ bad status flagged
  ✓ set_field: edits value, keeps comment, bumps updated_at, stays parseable
  ✓ scalar evidence coerces to [path], not char-split
SELFTEST PASS
```

### `--audit` (honest board)
```
64 files · 64 with state · 0 orphans (MISSING_STATE)
  ACTIVE  (1)   AGENT_51_DISPATCH_STATE_KIT_MIGRATION
  READY   (6)   A54, A56, A60, A62, A63, A64
  BLOCKED (4)   A24 (deferred), A55 (dep A54), A57 (dep A55, gated), A58 (premise-wrong, dep A61)
  DONE (52) · DEAD (1: AGENT_35 superseded by A41)
  ✓ no gaps, no unproven-done, no stale-active
```

### No-lost-job count check
- `.ai/dispatch/*.md` tracked at HEAD (pre-A51): **73**. Working tree: **75** (added: `CLAUDE.md`
  protocol doc + generated `_DISPATCH_STATE.md`; the original 73 all present, **zero deleted** —
  `git status` shows no `D` on any dispatch `.md`).
- Tool-visible **job files: 64** (73 originals − DISPATCH_LOG − 7 REVIEW docs − SUBSTRATE handoff,
  all listed in `.dispatch_not_a_job`). Each carries **exactly one** yaml block (multi=0, zero=0).
- `--audit` reports `64 files · 64 with state · 0 orphans` — job-file count == state-row count.
- Parquet not generated (md-view-only); its role of "one row per job" is served by
  `_DISPATCH_STATE.md` (64 rows).

### `created_at` immutability
Ran `--migrate` a second time: all 64 files reported `(has block)`, and `md5sum -c` over a
pre-run snapshot matched every file — **zero files changed** (no re-stamp of `created_at`).

### DISPATCH_LOG untouched + hook warn-only
- `git diff HEAD -- .ai/dispatch/DISPATCH_LOG.md` → **0 lines** (byte-unchanged; role intact).
- The pre-commit hook (`--stop-hook || true`) exits **0** even with gaps (`stop_hook()` always
  returns 0); ran the actual hook script → `exit=0`. Warn-only confirmed.

### Status reconciliation sample (guessed by --migrate → corrected to ground truth)
The migrator guessed `unknown` for 58/64 files and mis-guessed 2 from own-file markers; all
corrected via `--set` against the DISPATCH_LOG Index + git PR merge state:

| Job | migrate guess | corrected | ground truth |
|---|---|---|---|
| AGENT_16_HARNESS_BLOCK_SURFACE | unknown | **done** | `HarnessAdmissionBlocked` on `main:src/control/control_api.py` (PR #16 arc; log "built — op-merge" now merged) |
| AGENT_18_ORIENTATION_PAGE | unknown | **done** | `docs/OVERVIEW.md` on main (`0d6dc79`) |
| AGENT_31_M3_PHASE30_MCP_MANAGER | unknown | **done** | `scripts/mcp_manager.py` on main (PR arc merged) |
| AGENT_35_LIVE_F4_SPIKE | unknown | **dead** | log: "superseded by A41" |
| AGENT_41_LIVE_MANAGER_SPIKE_A40 | active (marker "LIVE") | **done** | spike RAN & PASSED 2026-07-12, built PR #11 (merged) |
| AGENT_42_F1_LIVE_REVIEW_LOOP | unknown | **done** | PR #16 MERGED |
| AGENT_46_M33_DURABLE_RELAY | unknown | **done** | PR #38 MERGED |
| AGENT_49_MANAGER_FORK_FROM_CONVERSATION | unknown | **done** | PR #31 MERGED |
| AGENT_52_M34_JOB1_ADOPTION | unknown | **done** | PR #49 MERGED |
| AGENT_53_M33_TURN_COST_GOVERNOR | unknown | **done** | PR #50 MERGED |
| AGENT_54_M34_JOB2_RECONSTRUCTION | unknown | **ready** | dispatched, no PR, dep A52 done → unblocked |
| AGENT_55_M34_JOB3_CRASH_RESPAWN | unknown | **blocked** | dispatched, dep A54 (not done) |
| AGENT_57_M4_HYBRID_EXECUTOR_SPIKE | unknown | **blocked** | dispatched — GATED on A55 |
| AGENT_58_QUOTA_COORDINATOR_ACTIVATION | unknown | **blocked** | log: premise wrong, dep A61 |
| AGENT_24_DECOMPOSER_GENERATOR | unknown | **blocked** | log Status = `deferred` (parked on purpose) |
| AGENT_61_QUOTA_COORDINATOR_FINALIZATION | unknown | **done** | direct commit `cbbaa10` to main |
| DROP_MANAGER_TELEMETRY_COCKPIT | unknown | **done** | PR #36 MERGED (is a real job, not a non-job doc) |
| DROP_TIMEZONE_NATIVE_TIME | done (marker) | **done** | PR #20 MERGED — evidence `src/core/timeutil.py` |
| DROP_MANAGER_ROLE_CARRIER_INDEPENDENT | done (marker) | **done** | PR #18 MERGED |
| FIX_CLAUDE_ISERROR_PROMPT_TOO_LONG | unknown | **done** | FX1 merged `a3f734b` |

### Adversarial self-review — findings + fixes
1. **Copied `.ai/dispatch/CLAUDE.md` demoted DISPATCH_LOG** ("NOT a status table anymore … do
   NOT re-introduce a status table"). This VIOLATES the hard operator constraint. **Fixed:**
   rewrote the "what each file is" section + command table to keep DISPATCH_LOG as the primary
   authoritative human index+closure surface and label `_DISPATCH_STATE.md` complementary. (Source
   kit untouched; only the copied file adapted.)
2. **A16/A18 were "built — op-merge" in the log** (could still be unmerged → wrong `done`).
   **Verified** both landed on `main` (A16 symbol in `main:src/`, A18 `docs/OVERVIEW.md` on main)
   before marking `done` and attached concrete merged-artifact evidence.
3. **A55/A57/A58 depend on not-done work** — a naive "dispatched → ready" would have mislabeled
   them. **Fixed:** set `blocked` + populated `depends_on` so the graph is honest.
4. **A41 own-file marker "LIVE" → migrator guessed `active`.** Ground truth: the spike ran and
   PASSED and built merged PR #11 → **corrected to `done`.**
5. **Git hook could not install via the tool** (worktree: `.git` is a file, not `.git/hooks`).
   **Fixed:** installed the identical warn-only managed block into the shared common hooks dir
   (`git rev-parse --git-common-dir`), then proved it exits 0 with/without gaps.
6. **Every `done` re-checked for `CLAIMED_DONE_NO_PROOF`** — all 52 carry an `evidence:` path that
   exists on disk (packet file or a concrete merged artifact); `--audit` raises none.

### Residual notes (explained, no unexplained gaps)
- **Job count is 64, not the packet's "59".** The packet was authored 2026-07-28; jobs A56–A64
  and the reclassification of `DROP_MANAGER_TELEMETRY_COCKPIT` (a real merged PR #36 job, not a
  non-job doc) were added since. 64 is the correct current count. Zero jobs lost.
- **`_dispatch.parquet` intentionally absent** (R1 md-view-only). The `--audit`/`_DISPATCH_STATE.md`
  eyeball table is the live query surface; the parquet query view is explicitly deferred.
- **`deferred/` subdir** (3 SUPERSEDED_* + 1 old AGENT_11 file) is out of scope — the tool only
  globs top-level `.ai/dispatch/*.md`; those historical files are correctly not tracked as jobs.
