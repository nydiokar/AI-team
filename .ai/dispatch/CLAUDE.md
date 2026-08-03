# Dispatch protocol — how job state works here (read this before touching a dispatch file)

Job STATE is machine-tracked. You do **one thing**: keep the `status:` in a job's OWN
`.md` file correct. Everything else (rendering the board, gap/proof checks) is automatic.

## The one rule
Each `.ai/dispatch/<job>.md` has a ` ```yaml ` block at the top = its state. **That block is
the source of truth.** The folder is the registry — a file with no block is a detected gap.

> **This project (AI-Team):** invoke the tool with the repo venv on Linux —
> `.venv/bin/python scripts/dispatch/dispatch_state.py <cmd>`. There is no pnpm wrapper here.
> The `.parquet` query view is intentionally NOT generated (no pandas/pyarrow in this `.venv`);
> the tool runs **markdown-view-only** and `_DISPATCH_STATE.md` + `--audit` are fully live.

## The 3 commands (this is the whole workflow)
| When | Run |
|---|---|
| **Start a new job** | `.venv/bin/python scripts/dispatch/dispatch_state.py --new JOB_ID` (stamps a canonical `created_at`, writes the brief stub). Then fill the brief. |
| **Change a job's state** | `.venv/bin/python scripts/dispatch/dispatch_state.py --set <JOB_ID> status done` — statuses: `ready` \| `active` \| `blocked` \| `done` \| `dead`. Also `--set <ID> owner <name>` / `depends_on` / `results_ref` / `evidence`. |
| **See the honest board** | `.venv/bin/python scripts/dispatch/dispatch_state.py --audit` — the query view + any gaps. |

You do **not** run `--render` by hand — the **git pre-commit hook** re-renders `_DISPATCH_STATE.md`
on every commit and warns you (never blocks) if state is dirty; it also auto-stages the fresh view
so it never drifts. (A Claude Code Stop hook does the same at session end where wired.) One-time
setup per repo: `.venv/bin/python scripts/dispatch/dispatch_state.py --install-git-hook`.

## Non-negotiables
- **NEVER hand-edit the yaml block with a regex/sed** — it corrupts them (control chars, mojibake).
  Use `pnpm dispatch:set`. It round-trips through yaml and verifies the block still parses.
- **NEVER hand-edit `_DISPATCH_STATE.md` or `_dispatch.parquet`** — generated, hook-denied.
- **`created_at` is CANONICAL** — written once at dispatch, never changed, never re-derived from git.
  Only `updated_at` bumps (automatically, on `--set`).
- **`status: done` needs proof.** Set `evidence:` to the artifact paths that prove the job ran
  (parquet/json/script). The audit flags `CLAIMED_DONE_NO_PROOF` if a `done` job's evidence is
  missing on disk. No evidence path = no automated proof; add one when you can.

## What each file is (don't conflate them)
- `<job>.md` — the BRIEF (what to do) **+** its yaml state block. Your edit surface.
- `DISPATCH_LOG.md` — **the primary, authoritative human-readable dispatch Index + per-job
  closure line** (operator-loved, grep-searchable). It STAYS the human status+closure surface —
  do NOT demote, gut, or delete it, and keep its Index current. Point a job's `results_ref:` at
  its row/section here.
- `_DISPATCH_STATE.md` — a GENERATED, **complementary** machine/query view (the `--audit` board).
  Read, never write. It is an ADDITIVE query convenience, **never a replacement** for
  DISPATCH_LOG. (`_dispatch.parquet` is not generated in this repo — md-view-only.)

## Resolving a gap the audit shows you (on the go)
- `MISSING_STATE J` → `pnpm dispatch:set J status <s>` (or `--new` if it's brand new).
- `CLAIMED_DONE_NO_PROOF J` → the `done` job's `evidence:` path doesn't exist. Fix the path in
  `J.md`, or set `status` back if it isn't actually done.
- `BAD_STATUS J` → the status isn't one of the 5 valid words. `--set` it to a real one.
- `STALE_<n>d J` → an `active` job untouched >14d. Finish it, block it, or mark it dead.
