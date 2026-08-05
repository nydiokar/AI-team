```yaml
job_id: AGENT_73_NODE_ENV_FILE_GUARD
created_at: "2026-08-05T09:34:34.482453+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-05T09:34:34.482453+00:00"
```

# DISPATCH — AGENT_73_NODE_ENV_FILE_GUARD

**Level:** 2 (defense-in-depth, small additive script guard) · **Type:** worker-side deploy-time
permission guard.
**Status of this packet:** ready (authored, not executed)
**Depends on:** — (P1-4 node-side leg from A67; the gateway `.env`/`state/mesh.db` were already
chmod 600 by A67 — this extends the discipline to every node via the deploy path).

> **Why this packet exists.** A67 fixed the gateway's `.env`/`state/mesh.db` modes to 600 but the
> node-side `.env` files (Horse, etc.) are not hardened and the agent has no direct access to those
> boxes. The durable fix is a startup/deploy guard: the worker deploy script refuses to proceed
> when its `.env` is group- or other-readable, so the next natural redeploy enforces 600 on every
> node automatically. This file ships a **script-only** change — it alters nothing about the running
> worker until the operator next runs `safe_worker_deploy.py`, so merging it disturbs nothing.

## Task

1. In `scripts/safe_worker_deploy.py`, before `_load_env()` actually loads, run a POSIX file-mode
   check on the env file (`AI_TEAM_ENV_FILE` or `<repo>/.env`): if the file exists and its mode has
   any group/other read bits, **fail the deploy** with an explicit message, unless the operator
   explicitly sets `AI_TEAM_ALLOW_LOOSE_ENV=1` (an acknowledged override). Windows: no-op.
   - Extract the check as a small pure function (e.g. `_assert_env_file_private(env_file: str)`)
     so it is unit-testable.
   - Recommend `chmod 600 <env_file>` and `chown` in the failure message, and mention backups must
     preserve modes (`tar --preserve-permissions`) since root-owned 600 breaks non-root backups.
2. Mirror the same guard in `scripts/auto_deploy.sh` (or the worker setup entrypoint) only if it
   reads `.env` directly; otherwise leave it to `safe_worker_deploy.py`.
3. Test the pure guard (plain `pytest`, touched module only): mode 600 ⇒ passes; mode 644 ⇒ fails
   unless override set; nonexistent file ⇒ passes (gateway-agnostic). Windows path no-op.
4. Docs: one line in `docs/MESH_SECURITY.md` storage/file-modes section.

## Constraints / hard rules

- **Never restart a worker/node-carrier reflexively.** This lands as a script change; enforcement
  happens on the operator's next explicit deploy (Horse surfaced then). Do not deploy to Horse.
- Branch policy: `feat/<slug>` + PR + self-merge; non-disclosing PR description.
- `pytest` on touched modules only; never the full/e2e suite.
- Do not carry other loops' uncommitted edits into your merge.
- Must remain byte-identical in behavior when `.env` is already 0600 (i.e. zero risk for the
  gateway box itself).

## Done when

- `safe_worker_deploy.py` refuses a loose-mode `.env` unless the override is set; pure guard
  function unit-tested green.
- Targeted `pytest` evidence recorded; PR merged to `main`.
- Set `evidence:` to the test report + PR ref and `status: done`.
