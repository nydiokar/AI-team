# A41 — Combined A35+3.1 LIVE manager spike, run against a REAL task (A40 slice 1)

**Level:** 3 (live/paid) · **Branch (code under test):** `feat/m3-phase31-manager-role` (A38, PR #10)
· **Deliverable branch (worker writes here):** `feat/m3.2-review-emitter` · **Date:** 2026-07-12
**Reads with:** `AGENT_35_LIVE_F4_SPIKE.md` (base mechanics + rollback this reuses),
`AGENT_38_MANAGER_ROLE_WIRING.md` (the role path it exercises), `AGENT_40` row (the real work),
`docs/M3_MANAGER_INVOCATION_SPEC.md` §6 Phase 3.1, `tests/test_manager_loop_integration.py` (the
cheap A39 proof that must be GREEN before this runs).

> ⛔ **NOT executed by the coding agent.** Flips the same three CLAUDE.md guardrails as A35 —
> **paid Claude CLI** (scar #1), a **live PM2 restart** ("Ask First"), a **global `~/.claude.json`
> mutation** — and additionally has a **real Claude worker write code** (to a dedicated branch,
> never `main`). Operator runs it; the agent hands it over and OVERSEES quality from the read model.

---

## Why this shape (the operator's intent, distilled)

The paid live run is expensive, so it must **buy two things at once**:
1. **Prove the M3.1 loop for real** — the only thing the A39 integration proof cannot: the real
   Claude Manager boot (`system_prompt` preset+append took, `manager_v1` tools granted), and real
   dispatch → JOIN → `wait_for_worker` → `close_case` against a live gateway.
2. **Ship real work** — the Manager drives **A40 slice 1** (the `review.*` verdict emitter). The run
   is therefore a *stress test against a genuine task*, not a throwaway "write the date to /tmp".

This is bounded and supervised on purpose. The scar it avoids is NOT "spending tokens" — it is
**unbounded, unsupervised** spend (the ~100 parallel 10k-upfront sessions). Here: **one** manager
session, **one** worker session, **≤ one** rework cycle, single-track, every step gated by a human/
agent reading the committed diff in git. Tokens are authorized; the firehose is not.

---

## The real objective the Manager is given (A40 slice 1 — bounded + checkable)

> **Objective (goes into `POST /api/manager`):** "Implement M3.2 slice 1 — the `review.*` verdict
> emitter — on branch `feat/m3.2-review-emitter`. Add a `record_review` tool to `manager_v1`
> (`scripts/mcp_manager.py`) and a `POST /api/cases/{case_id}/review` seam that appends
> `review.accepted` | `review.rework_requested` | `review.waived` to `flow_events` (vocab already
> reserved in `db.py:140-143`). Add a close-gate: `close_case` refuses while the latest review for
> the Case is `rework_requested` and unresolved. Behavior behind a new default-OFF flag
> `REVIEW_EMITTER_ENABLED` ⇒ byte-identical when off. Unit tests only — no live wiring, no schema
> change (the event vocab already exists). Dispatch ONE worker to implement it; review its committed
> diff in git; rework at most ONCE; then close the Case."

**`completion_criteria` (passed to `/api/manager`, reconciled at close — use the A39-corrected
shape `{"criterion": "...", "status": "met"|"waived", "reason": "..."}`):**
1. `record_review` is in `manager_v1` (`manager_tool_names()`) and `_TOOLS`.
2. `POST /api/cases/{id}/review` appends the correct `review.*` event to `flow_events` (new test asserts).
3. `close_case` refuses on an unresolved latest `rework_requested`; allows after `review.accepted`/`waived` (new test asserts).
4. Targeted pytest green; `REVIEW_EMITTER_ENABLED` OFF ⇒ byte-identical.
5. The worker's diff is reviewed in git (by Manager + operator) before close — no self-report trust.

> **`review.*` is the SUBJECT of the work, not a capability used during the run.** The Manager
> cannot emit `review.*` yet (that is what slice 1 builds) — so expect NONE in this Case's timeline,
> exactly as A35 warned. Do not flag its absence as a bug.

---

## Preconditions (verify read-only first)

| Precondition | Check | Expected |
|---|---|---|
| **A39 integration proof green** | `pytest tests/test_manager_loop_integration.py` | 4 passed (no paid CLI) |
| PR #10 (A38) merged/deployed OR branch deployed | `git -C ~/dev/AI-team log --oneline -1` | A38 code is the running code |
| `MAX_CONCURRENT_TASKS ≥ 2` | gateway env | manager holds a slot while waiting; worker needs another |
| Worker node online | `GET /api/nodes` | `Horse`/`kanebra-worker` online |
| Deliverable branch exists | `git -C ~/dev/AI-team branch` | `feat/m3.2-review-emitter` cut from the A38 base |

---

## Flag matrix (exact — verified against `claude_driver.py` + `orchestrator.py`)

| Flag / config | Why | Set by |
|---|---|---|
| `HARNESS_FLOW_DRIVE=1` | Case attach / JOIN / timeline / lineage only persist with the drive on | gateway `.env` |
| `MANAGER_ROLE_ENABLED=1` | `invoke_manager` works **and** driver `_role_boot` loads the role prompt + scopes `manager_v1` to the `case_role=="manager"` session | gateway `.env` |
| `"manager"` in `~/.claude.json` | driver `_mcp_manager_configured()` gate for the tool grant | `python scripts/setup_mcp.py --with-manager` |
| `MANAGER_TOOLS_ENABLED` | **optional** — legacy A34 process-wide grant; **superseded** by the role path when `MANAGER_ROLE_ENABLED=1`. Leave unset. | — |

## Step 1 — Land code + cut the deliverable branch (operator)

```bash
cd ~/dev/AI-team
# A38 must be the running code (merge PR #10, or deploy the branch to trial):
git checkout feat/m3-phase31-manager-role   # or main after merge
git checkout -b feat/m3.2-review-emitter     # the worker commits HERE, never main
```

## Step 2 — Register manager server + flip flags (operator)

```bash
cd ~/dev/AI-team
python scripts/setup_mcp.py --with-manager       # merges "manager" into ~/.claude.json (keeps "jobs")
# Add to the gateway .env:
#   HARNESS_FLOW_DRIVE=1
#   MANAGER_ROLE_ENABLED=1
```
Rollback: remove those two env lines + delete the `"manager"` entry from `~/.claude.json`.

## Step 3 — Health gate + restart (operator; "Ask First")

```bash
curl -s http://127.0.0.1:9003/health          # {"status":"ok"} BEFORE
pm2 restart ai-team-gateway
sleep 5
curl -s http://127.0.0.1:9003/health          # {"status":"ok"} AFTER
```
> ⛔ **Never** `python main.py status` (grabs the gateway lock → kills the live gateway). Liveness = `curl .../health` only.

## Step 4 — Invoke the Manager on the REAL objective (operator, from `kanebra`)

`T=<DASHBOARD_TOKEN>` from `.env`. This uses the A38 `/api/manager` path (NOT the A35 manual pattern).

```bash
curl -s -X POST http://127.0.0.1:9003/api/manager -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{
    "repo_path":"/home/cifran/dev/AI-team",
    "branch":"feat/m3.2-review-emitter",
    "objective":"<the A40 slice-1 Objective text above, verbatim>",
    "completion_criteria":"[\"record_review in manager_v1 + _TOOLS\",\"POST /api/cases/{id}/review appends the review.* event\",\"close_case refuses unresolved rework_requested\",\"targeted pytest green; flag OFF byte-identical\",\"diff reviewed in git before close\"]"
  }'
# -> { ok, session_id: MGR_SID, case_id: CASE, task_id: MGR_TASK }
```
The Manager session now boots with the role prompt + `manager_v1` tools and receives the objective
as its first turn. It should, on its own, call `dispatch_worker(objective=<bounded impl task>,
case_id="CASE", cwd="/home/cifran/dev/AI-team")` → then `wait_for_worker(task_id=<wt>,
flow_run_id="CASE")`.

## Step 5 — OVERSIGHT protocol (agent + operator — the guardrail, run at EACH checkpoint)

The Manager's self-report is **not** trusted; git + the read model are truth.

1. **After dispatch:** `curl -s http://127.0.0.1:9003/api/work/CASE/timeline -H "Authorization: Bearer $T"`
   — confirm the worker task ATTACHED (`task.attached`, membership worker) and NO child Case was born
   (`curl .../api/flows?limit=10` → still one Case for this objective).
2. **Anti-starvation (while the manager waits):** `curl -s http://127.0.0.1:9003/api/work -H "Authorization: Bearer $T"`
   — the worker task is RUNNING in its own slot/node; the manager is not deadlocked on its own slot.
3. **After the worker's `task.finished`:** review the ACTUAL diff before letting the Manager close:
   ```bash
   git -C ~/dev/AI-team log --oneline -3 feat/m3.2-review-emitter
   git -C ~/dev/AI-team diff --stat main...feat/m3.2-review-emitter
   cd ~/dev/AI-team && .venv/bin/python -m pytest tests/test_mcp_manager.py -q   # + any new review tests
   ```
   Check each `completion_criterion` against the real diff. If unmet → the Manager should
   `dispatch_worker` ONE rework cycle (bounded). If still unmet after that → **stand down** (Step 7),
   do not loop.
4. **At close:** the Manager calls `close_case(case_id="CASE", criteria_reconciliation=[{"criterion":"...","status":"met"}, ...])`.
   Confirm it **refuses** if any criterion is genuinely unmet, and **closes** only when each is
   `met` (or `waived` with a real reason). Verify `GET /api/flows/CASE` → `status="closed"`.

> **A39 finding baked in:** reconciliation entries MUST use `{"status":"met"}` /
> `{"status":"waived","reason":...}` — a `{"met":true}` shape is silently rejected (the Case would
> never close). The tool schema example was corrected on `feat/m3-phase31-manager-role`; make sure
> the deployed code carries that fix, or tell the Manager the exact shape.

## Step 6 — Verdict (fill Closure)

**PASS** = (a) the real Claude Manager booted with role+tools, (b) `/api/manager` opened ONE Case
(`case_role="manager"`), (c) the worker JOINED it (task link, no child Case), (d) `wait_for_worker`
resolved via the Case timeline with no slot starvation, (e) `close_case` gated correctly, AND
(f) the deliverable — A40 slice 1 — is real, reviewed, tests green on `feat/m3.2-review-emitter`.
Any failure IS M3's next job (§4 F4) — capture the `[Fn]` here.

## Step 7 — Rollback / stand-down (ALWAYS, after the run)

```bash
# gateway .env: remove MANAGER_ROLE_ENABLED / HARNESS_FLOW_DRIVE additions (or set to 0)
# remove the "manager" entry from ~/.claude.json
pm2 restart ai-team-gateway && curl -s http://127.0.0.1:9003/health
```
The Case rows are SHADOW (read-only to execution). The deliverable lives on
`feat/m3.2-review-emitter` — keep it (open a PR for A40) or `git branch -D` it if the run aborted.
No schema/migration/destructive change. If a worker task is stuck: cancel it via the control API
(`POST /api/tasks/{id}/cancel` equivalent), never `python main.py status`.

---

## Cost envelope (bounded, single-track)

- 1 manager session + 1 worker session; **≤ 1 rework cycle** (so ≤ 2 worker turns).
- Manager turns: boot + review (+ rework) + close ≈ 3–5. `wait_for_worker` holds NO task slot.
- Estimated paid turns total ≈ 6–8, sequential. Nothing runs in parallel. Hard stop at Step 7 if
  the second worker turn still misses criteria.

## Closure (fill after the run)

**Status: `dispatched — op-gated`.** Awaiting operator go/no-go. On completion: verdict + `[Fn]`
findings here; tick §6 Phase 3.1 in the M3 spec; flip A35/A38 "live proof" items to done; if the
A40 deliverable lands, open its PR and advance the A40 row; update `CONTEXT.md` + `DISPATCH_LOG.md`.
