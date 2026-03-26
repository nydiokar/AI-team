# Next Tasks

This file is the short operational backlog for getting the gateway from "usable locally" to "ready to extend with Codex and deploy".

## 1. Codex Support Validation

Goal:
- make Codex a real supported backend, not just a code path that exists

Tasks:
- run a real Telegram session with `/session_new codex <repo>`
- verify first-turn output arrives cleanly in Telegram
- verify `backend_session_id` is persisted
- verify second turn resumes the same Codex conversation
- compare Codex artifact/output shape against Claude and normalize any mismatches
- confirm Codex failure messages surface the real backend error

Definition of done:
- two-turn Codex session works end-to-end with no manual intervention

## 2. Telegram Command Polish

Goal:
- make the bot feel like a product, not a debug console

Tasks:
- tighten `/session_status` formatting
- tighten `/git_status` formatting
- shorten noisy error replies while preserving backend-specific detail
- review whether `/commit_all` should remain public
- decide whether compatibility-only methods for `/run`, `/say`, `/progress`, and `/cancel` should be deleted entirely

Definition of done:
- command replies are compact, readable, and consistent

## 3. Backend Contract Hardening

Goal:
- stop backend CLI changes from silently breaking the gateway

Tasks:
- pin supported Claude Code and Codex CLI versions, or document an explicit version policy
- add one real smoke path per backend using the exact command shape the gateway runs
- fail fast on startup or doctor output when backend contract checks fail
- capture backend version information in diagnostics

Definition of done:
- CLI regressions are caught before normal Telegram use

## 4. Legacy Compatibility Cleanup

Goal:
- reduce confusion and dead surface area

Tasks:
- decide whether `.task.md` watcher ingestion remains supported
- if not, remove watcher-first docs and stale bridge-era tests
- if yes, label it clearly as compatibility-only in docs and status output
- remove dead `ClaudeBridge` and task-runner call paths that are no longer on the primary runtime path

Definition of done:
- the repo describes one primary product shape, not two competing ones

## 5. Deployment Readiness

Goal:
- make deployment predictable

Tasks:
- confirm `.env` scope values and repo boundaries
- review startup/doctor output for operator clarity
- verify session state, logs, and artifacts are written to the expected locations
- document the minimum deploy checklist in `docs/QUICK_START.md` or `docs/README.md`

Definition of done:
- a new machine can be configured and checked without guessing
