```yaml
job_id: AGENT_71_MESH_PER_NODE_CREDENTIALS
created_at: "2026-08-05T09:34:34.482453+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-05T09:34:34.482476+00:00"
```

# DISPATCH — AGENT_71_MESH_PER_NODE_CREDENTIALS

**Level:** 3 (security remediation, multi-touch) · **Type:** design + implementation of per-node
mesh credentials to replace the single shared `WORKER_TOKEN` identity model.
**Status of this packet:** ready (authored, not executed)
**Depends on:** — (operator decision from A67 R2 escalation; read `.security/mesh_findings_2026-08.md`
first — PRIVATE, do not copy its contents into this file or any commit).
**Also read:** `docs/MESH_SECURITY.md` (the current public model this changes) and
`AGENT_67_MESH_SECURITY_REVIEW_THREAT_MODEL.md` (closure records which P1s this resolves).

> **Why this packet exists.** The A67 mesh security review concluded that the single shared
> `WORKER_TOKEN` is effectively a full-admin credential across the whole mesh: node identity is
> **self-reported** on register/heartbeat/claim/result, so any holder of the shared secret can
> impersonate a node, claim tasks destined for other nodes and forge results, and re-register under
> another node's id to knock its in-flight claims loose (incarnation-bump DoS). The operator chose to
> fix this properly rather than keep the shared-token model. This job designs and lands per-node
> credentials. **The fix must never ship a regression for the mesh's task flow — worker-side code
> only lands on a surfaced worker redeploy (Horse).**

## Task

1. **Design** a per-node credential model (produce a short design note first, ~1 page):
   - Each enrolled node gets a unique `NODE_<id>_TOKEN` (or equivalent) minted/stored where the
     gateway can verify it, independent of the shared `WORKER_TOKEN`.
   - AuthZ binding: `register`, `heartbeat`, `claim`, `release`, `result`, and the nudge-trigger
     paths must bind the caller's claimed `node_id` to a credential actually issued to that node.
   - `/tasks/pending` and `/tasks/claim` must refuse a node claiming a task pinned to a different
     node's `machine_id`.
   - Registry protection: a re-register that would bump the incarnation of a node must fail unless
     it is authenticated as that node (this is what stops the spoofed re-register DoS).
   - Backward-compat path for rollout (single shared token may remain as a fallback keyed by an
     explicit flag) so the mesh does not hard-break between gateway and node deploys.
2. **Implement** gateway-side enforcement (`src/control/task_server.py`, `node_registry.py`,
   `config/settings.py::MeshConfig`) behind a flag (default: preserve current behavior ⇒
   byte-identical until the operator turns the flag on, matching repo convention).
3. **Implement** worker-side provisioning (`src/worker/agent.py`, setup scripts): the worker
   reads its node credential and authenticates with it; `scripts/` deploy path updated.
4. **Tests** (plain `pytest`, touched modules only — TEST COST GUARD): identity binding on
   claim/result, pinned-task refusal for wrong node, spoofed re-register does NOT bump a live
   node's incarnation, flag-off ⇒ existing tests byte-identical.
5. **Docs**: update `docs/MESH_SECURITY.md` "What the tokens mean" + "Limits by design" to the
   new model once landed. No exploit detail, consistent with A67 Part B rules.

## Constraints / hard rules

- **Never restart a worker/node-carrier reflexively** — worker redeploy (e.g. Horse) is surfaced
  to the operator as a decision, not done silently. Gateway restart after gateway-side merge is
  fine.
- Branch policy: any `src/`/config change = `feat/<slug>` + PR + self-merge; non-disclosing PR
  description (reference finding IDs, no recipes).
- `pytest` on touched modules only; never the full/e2e suite.
- The `DASHBOARD_TOKEN` control-API surface is in scope only insofar as it must keep working
  unchanged; it is not the target of this job.
- Do not carry other loops' uncommitted edits into your merge.

## Done when

- Per-node credentials enforced on register/heartbeat/claim/result with pinned-task refusal, and a
  spoofed re-register cannot bump a live node's incarnation (flag-gated; default off).
- Worker-side provisioning + tests green; targeted `pytest` evidence paths recorded.
- `docs/MESH_SECURITY.md` updated to the new model.
- Operator-decision items (worker redeploy) surfaced, not done silently.
- Set `evidence:` to the design note + test report paths and `status: done`.
