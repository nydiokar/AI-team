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

- [ ] R1/R2 decisions taken
- [ ] kit copied + deps resolved
- [ ] `--migrate` run, report captured
- [ ] all 59 statuses reconciled to ground truth, evidence added to every `done`
- [ ] DOC_MAP + root pointer reconciled
- [ ] git hook wired, `--selftest` PASS, `--audit` clean/explained
- [ ] PR opened + merged

## Closure (fill on completion)

_(verdict prose, the acceptance evidence, and any residual explained gaps go here)_
