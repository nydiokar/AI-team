# A35 — M3 Phase 3.0 acceptance: the live F4 spike (OPERATOR-GATED runbook)

**Level:** 3 (live/paid) · **Branch:** `feat/m3-phase30-mcp-manager` · **Date:** 2026-07-10
**Reads with:** `docs/M3_MANAGER_INVOCATION_SPEC.md` §4 F4 / §6 Phase 3.0, `AGENT_34_MANAGER_SESSION_WIRING.md`
(the gates this flips), `AGENT_31/32/33` (the tool + endpoint + sender it exercises).

> ⛔ **This dispatch is NOT executed by the coding agent.** It flips three of `CLAUDE.md`'s
> hardest guardrails at once — **paid Claude CLI** (project scar #1: "burned millions of
> tokens"), a **live PM2 restart** ("Ask First"), and a **global `~/.claude.json` mutation**.
> It is written as a tight operator runbook: exact commands, health gates, and full rollback.
> Everything up to this line is already code-complete and reversible (A31–A34). **Operator
> runs this; the agent hands it over.**

---

## Objective (the proof, §6 Phase 3.0)

Confirm **live** that a gateway-spawned Claude session, given `mcp_manager`, can:
1. **dispatch a child worker session** via `dispatch_worker` (a real, separate gateway task);
2. **block on its outcome** via `wait_for_worker` **without starving the task loop** (the child
   gets a slot while the manager waits — the §6 anti-starvation criterion); and
3. produce a **parent→child lineage edge visible in `/api/flows`** (§6 lineage criterion).

If any part fails, that failure *is* M3's first real job (as §4 F4 warns) — capture it here.

> **⚠️ SCOPE UPDATE (2026-07-12) — run this COMBINED with M3.1 (A38, PR #10).** A38 landed the
> Manager-role boot + `case_id` JOIN + `close_case`, so this spike should now also flip
> `MANAGER_ROLE_ENABLED=1` and additionally verify: (a) a real Claude manager session boots with
> the role prompt (`system_prompt` preset+append took, `manager_v1` tools granted), (b)
> `POST /api/manager` opens ONE Case (`case_role="manager"`), (c) the dispatched worker **JOINS**
> that Case (`task` link, NO child Case), (d) `wait_for_worker(task_id, flow_run_id=<case_id>)`
> resolves via the Case timeline, (e) `close_case` closes it. **`review.*` events are OUT OF SCOPE
> here** — the Manager reviews, but wiring its verdict into `flow_events` is **M3.2**; expect NO
> `review.*` in the timeline and do NOT flag its absence as a bug. **Do the cheap A39 integration
> proof (no paid CLI) BEFORE spending on this run** — see `.ai/CONTEXT.md` M3 sequencing note.

---

## Preconditions (verify first — all currently satisfied on the live box)

| Precondition | Why | How to check (read-only) | Live status 2026-07-10 |
|---|---|---|---|
| `MAX_CONCURRENT_TASKS ≥ 2` | The manager holds ITS slot while blocked in `wait_for_worker`; the child needs a *different* slot or it deadlocks. | gateway env | **4** ✅ (ample) |
| A worker node online | Extra isolation: the child can route off-host entirely. | `GET /api/nodes` | `Horse` + `kanebra-worker` **online** ✅ |
| `HARNESS_FLOW_DRIVE=1` | Lineage + flow rows only persist with the drive flag ON (M1/M2 shadow gate). | your gateway `.env` | CONTEXT says ON — **confirm in `.env`** |
| Branch deployed | A31–A34 code must be the running code. | `git -C ~/dev/AI-team log --oneline -1` after checkout | pending operator merge/deploy |

---

## Step 1 — Land the code (operator)

```bash
cd ~/dev/AI-team
# Option A: review + merge the Phase 3.0 stack to main, then deploy main.
gh pr create --fill --base main --head feat/m3-phase30-mcp-manager   # if not already open
#   ...review, merge...
git checkout main && git pull
# Option B (spike-first): deploy the branch directly to trial before merging.
#   git checkout feat/m3-phase30-mcp-manager
```

## Step 2 — Register the manager MCP server + flip the flag (operator)

```bash
cd ~/dev/AI-team
python scripts/setup_mcp.py --with-manager        # adds "manager" to ~/.claude.json (merges; keeps "jobs")
# Add to the gateway .env (the file the agent may NOT touch):
#   MANAGER_TOOLS_ENABLED=1
# (Confirm HARNESS_FLOW_DRIVE=1 is present too.)
```

Rollback for this step: `MANAGER_TOOLS_ENABLED=0` (or remove the line) and delete the
`"manager"` entry from `~/.claude.json`.

## Step 3 — Health gate + restart (operator; "Ask First" per CLAUDE.md)

```bash
curl -s http://127.0.0.1:9003/health          # expect {"status":"ok"} BEFORE
pm2 restart ai-team-gateway                    # picks up the flag + new code
sleep 5
curl -s http://127.0.0.1:9003/health          # expect {"status":"ok"} AFTER
```
> ⛔ **Never** run `python main.py status` — it grabs the gateway lock and KILLS the live
> gateway (CONTEXT cost-guard). The only liveness check is `curl .../health`.

## Step 4 — Run the spike (operator, from the gateway host `kanebra`)

The control API is loopback-only, so drive this from `kanebra`. `T=<DASHBOARD_TOKEN>` from `.env`.

1. **Spawn a manager session** (Telegram: open a Claude session on a small repo; or):
   ```bash
   curl -s -X POST http://127.0.0.1:9003/api/sessions -H "Authorization: Bearer $T" \
     -H 'Content-Type: application/json' \
     -d '{"backend":"claude","repo_path":"/home/cifran/dev/AI-team"}'
   # -> note the returned session_id (call it MGR_SID)
   ```
2. **Send the manager its bounded instruction.** Ask it to dispatch ONE tiny, safe worker task
   (e.g. "create /tmp/f4_spike_proof.txt containing the current date") and then wait on it:
   ```bash
   curl -s -X POST http://127.0.0.1:9003/api/instructions -H "Authorization: Bearer $T" \
     -H 'Content-Type: application/json' -d '{
       "session_id":"MGR_SID",
       "description":"You are a Manager. Use the mcp__manager__dispatch_worker tool to dispatch ONE small worker task: \"write the current date to /tmp/f4_spike_proof.txt\". Pass parent_flow_run_id=<MGR_FLOW> (given below). Then call mcp__manager__wait_for_worker with the returned task_id and report the terminal status. Do NOT do the work yourself."
     }'
   # -> note the returned task_id (MGR_TASK)
   ```
3. **Get the manager's own flow_run_id** (Phase 3.1 will inject this as `SESSION_ID`; for the
   spike, supply it by hand — re-send step 2 with the real value if needed):
   ```bash
   curl -s "http://127.0.0.1:9003/api/flows?task_id=MGR_TASK&limit=1" -H "Authorization: Bearer $T"
   # -> MGR_FLOW = flows[0].flow_run_id
   ```

## Step 5 — Verify (the three success criteria)

```bash
# (a) Anti-starvation: while the manager waits, the child is RUNNING in its own slot/node.
curl -s http://127.0.0.1:9003/api/work -H "Authorization: Bearer $T"      # manager case + child case both progressing
# (b) Lineage edge: the child flow carries parent_flow_run_id = MGR_FLOW.
curl -s "http://127.0.0.1:9003/api/flows?limit=10" -H "Authorization: Bearer $T"
curl -s "http://127.0.0.1:9003/api/flows/MGR_FLOW" -H "Authorization: Bearer $T"   # child_flow link present
# (c) Result returned: the manager's reply reports the child's TERMINAL status
#     (Telegram reply, or the session timeline):
curl -s "http://127.0.0.1:9003/api/sessions/MGR_SID/timeline" -H "Authorization: Bearer $T"
ls -l /tmp/f4_spike_proof.txt         # the child actually did the work
```

**PASS** = child ran in its own slot (no deadlock), `MGR_FLOW` → child edge visible in
`/api/flows`, and the manager received the child's terminal status. Record the verdict + any
`[Fn]` findings in this file's Closure, and answer A31 open-question item 5 (did the plain
worker dispatch reach a terminal `flow_runs.status`? — if not, `wait_for_worker` needs the
task-level fallback A31 flagged).

## Step 6 — Rollback / stand-down (always, after the spike)

```bash
# In the gateway .env: set MANAGER_TOOLS_ENABLED=0 (or remove it).
# Remove the "manager" entry from ~/.claude.json.
pm2 restart ai-team-gateway
curl -s http://127.0.0.1:9003/health
```
Nothing to clean in the DB — the spike writes only SHADOW flow/lineage rows (read-only to
execution) and one throwaway `/tmp` file. No schema, no migration, no destructive change.

---

## Closure (fill after the run)

**Status: `dispatched — op-gated`.** Awaiting operator go/no-go. On completion, set the verdict
(PASS/FAIL + findings) here, tick §6 Phase 3.0 in the M3 spec, flip the A31/A34 "live spike"
items to done, and update `CONTEXT.md` + `DISPATCH_LOG.md`. If PASS, Phase 3.0 is accepted and
the next milestone is **Phase 3.1** (manager-role session type: role-prompt boot + scope the
manager tools to that session + `SESSION_ID` auto-lineage). If FAIL, the failure mode becomes
Phase 3.1's first job.
