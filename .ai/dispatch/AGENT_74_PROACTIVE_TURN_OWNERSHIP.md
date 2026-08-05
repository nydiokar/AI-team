```yaml
job_id: AGENT_74_PROACTIVE_TURN_OWNERSHIP
created_at: "2026-08-05T09:34:34.482453+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-05T09:34:34.482453+00:00"
```

# DISPATCH — AGENT_74_PROACTIVE_TURN_OWNERSHIP

**Level:** 2 (defense-in-depth, small gateway-side PR) · **Type:** session-ownership binding on the
proactive-turn write path.
**Status of this packet:** ready (authored, not executed)
**Depends on:** — (P2-6 from A67; operator confirmed the vector is real and agreed the same shared
identity root cause as A71, so this is the cheap incremental closing of the pinned-session case
while A71 lands the full per-node credential model).

> **Why this packet exists.** P2-6: the proactive-turn write path accepts a **self-reported**
> `node_id` with no check that the caller actually owns the target session. Under the shared
> `WORKER_TOKEN`, any worker (or anyone who has leaked the token) can inject a fabricated turn into
> a session pinned to another node. The full fix is per-node credentials (A71). This job lands the
> cheap, safe half: when a session is **pinned** to a `machine_id`, only that node may post a
> proactive turn to it. Unpinned sessions stay open to any worker (legit workers may run any
> unpinned task), so no legit flow can break.

## Task

1. In the proactive-turn handler (`src/control/task_server.py` ~1025), load the target session's
   `machine_id`. If it is set (pinned), require `payload.node_id == machine_id`; otherwise reject
   with 403. If `machine_id` is empty (unpinned), allow as today.
   - Keep the existing message-boundedness/context behavior untouched.
   - Do not alter the session-side `machine_id` derivation used elsewhere (`session.get("machine_id")
     or payload.node_id`) — that is a different seam and part of the A71 identity fix.
2. Tests (plain `pytest`, touched modules only): proactive turn to a pinned session from a
   mismatched node ⇒ 403; from the owning node ⇒ accepted; unpinned session from any node ⇒
   accepted (regression: no change for the normal flow).
3. Docs: one line in `docs/MESH_SECURITY.md` "Limits by design" if a "bounded execution" note is
   warranted.

## Constraints / hard rules

- Branch policy: `feat/<slug>` + PR + self-merge; non-disclosing PR description.
- `pytest` on touched modules only; never the full/e2e suite.
- Gateway-side only: restarting the gateway after merge is fine; never touch the worker.
- Do not carry other loops' uncommitted edits into your merge.
- Must not break unpinned-session proactive turns (the normal case in this repo).

## Done when

- Pinned-session proactive-turn forgery rejected (403); owning node and unpinned flows unaffected.
- Targeted `pytest` evidence recorded; PR merged to `main`; gateway restarted and `/health` ok.
- Set `evidence:` to the test report + PR ref and `status: done`.
