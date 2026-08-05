```yaml
job_id: AGENT_72_SECURITY_INPUT_CAPS
created_at: "2026-08-05T09:34:34.482453+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: opencode-agent
depends_on: []
results_ref: DISPATCH_LOG A72 (PR #73)             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-05T10:21:22.235181+00:00"
```

# DISPATCH — AGENT_72_SECURITY_INPUT_CAPS

**Level:** 2 (defense-in-depth, small gateway-side PR) · **Type:** server-side input bounds on the
control-API write surface.
**Status of this packet:** ready (authored, not executed)
**Depends on:** — (P2-3 from A67; operator approved light server-side bounds in follow-up Q&A —
see `AGENT_67_MESH_SECURITY_REVIEW_THREAT_MODEL.md` closure and `docs/MESH_SECURITY.md`).

> **Why this packet exists.** P2-3 (unbounded `/api/instructions` `description`, `objective`, and
> no size bounds on Manager/Case bodies) means one runaway or accidental oversized post can balloon
> the sessions DB and trip downstream engines. The mesh's token is shared and the dispatch
> interface is intentionally unrestrained (a real attacker does not need a big string), so the
> operator approved **generous** server-side caps that blunt accidents without ever throttling
> legitimate work (the MCP tool caps `objective` at 8k; the compact-context budget is 48k — the
> cap below is 32x that).

## Task

1. Add one module-level generous bound, e.g. `_MAX_INSTRUCTION_CHARS = 262144` (256 KB), and apply
   it as `Field(max_length=...)` to:
   - `InstructionBody.description` (`src/control/control_api.py` ~106)
   - `ManagerInvokeBody.objective` (~148) and its `completion_criteria`
   - `CaseOpenBody.objective` (~254) and its `completion_criteria`
2. **Do NOT add a request rate limit in this job.** The Manager can legitimately dispatch several
   workers in parallel; a naive per-token limiter risks throttling real orchestration. Rate limits
   (if ever) must be a separate, flag-gated, operator-confirmed change.
3. Tests (plain `pytest`, touched modules only): oversized field ⇒ 422 from pydantic; a normal
   dispatch body still succeeds (no behavior change at realistic sizes).
4. Docs: no public doc change needed beyond a `docs/MESH_SECURITY.md` "Limits by design" note that
   the control-API write surface is bounded.

## Constraints / hard rules

- Branch policy: `feat/<slug>` + PR + self-merge; non-disclosing PR description.
- `pytest` on touched modules only; never the full/e2e suite.
- Gateway-side only: restarting the gateway after merge is fine; never touch the worker.
- Do not carry other loops' uncommitted edits into your merge.
- Caps must be generous enough that no realistic existing caller (web composer, MCP Manager,
  Manager-internal dispatches) can hit them.

## Done when

- Three body classes bounded server-side; oversized → 422; normal dispatch unaffected.
- Targeted `pytest` evidence recorded; PR merged to `main`; gateway restarted and `/health` ok.
- Set `evidence:` to the test report + PR ref and `status: done`.

## Milestone

- 2026-08-05: PR #73 merged (`feat/security-api-input-caps`, `b5d9ad1`). 4 control-api suites green
  (write + fork + wait_group + flows). Gateway restarted; `/health` ok.

## Closure

**Verdict: done.** All five write-surface fields bounded at `_MAX_INSTRUCTION_CHARS = 262144`
(`InstructionBody.description`, `ManagerInvokeBody.objective`/`completion_criteria`,
`CaseOpenBody.objective`/`completion_criteria`). Oversized ⇒ pydantic 422, verified **live** on the
restarted gateway with a 262145-char description probe. Under-cap dispatch flows untouched (existing
suites green). No rate limit shipped, per plan (Manager parallel-dispatch risk). Evidence:
`tests/test_control_api_write.py` (3 new tests) + live 422 probe. `docs/MESH_SECURITY.md` note
updated in the A73 commit batch.

