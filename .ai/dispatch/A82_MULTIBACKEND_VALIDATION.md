# A82 Multi-Backend & Agent-Source Validation

- **Date:** 2026-10-07 (UTC)
- **Author:** live validation worker (no `src/` change, no session deletion beyond own scratch sessions)
- **Purpose:** Prove or disprove three items flagged OPEN-UNTRACKED in `A82_E2E_CERTIFICATION.md §4`:
  (i) agent-source turn-queue send over a minted sender capability;
  (ii) Codex managed turn end-to-end;
  (iii) opencode-server managed turn end-to-end.
  These gate Stage-8b legacy cutoff (A100).
- **Gateway:** `http://127.0.0.1:9003` — image `ai-team:local`, schema 43, `coverage_ok=true`
- **Live carrier nodes:** `kanebra` (online, local), `Horse` (online, remote)
  Both advertise `managed_backends=["claude","codex","opencode-server"]`

---

## VERDICT SUMMARY

| Item | Verdict | Short evidence |
|---|---|---|
| **(1) Codex managed turn e2e** | **PASS** | `task_a0e13623` completed, `queue_protocol=1`, `success=True`, `output="OK"`, `backend_session_id=01a11744-...`, carrier=kanebra |
| **(2) opencode-server managed turn e2e** | **PASS** | `task_b6557da4` completed, `queue_protocol=1`, `success=True`, `output="OK"`, `backend_session_id=ses_ee8bab3cdffe...`, carrier=kanebra |
| **(3) Agent-source send** | **PASS** | `task_834db4e9` admitted, `turn_source=agent`, `sender_session_id=53dc2f3c328c`, `queue_protocol=1`, `queue_sequence=2` (FIFO, no clobber) |

**Pre-validation note:** `A82_E2E_CERTIFICATION.md §4.(ii)` claimed "Codex has no `run_managed_turn`/`supports_managed_turns` — a Codex session can never be enrolled or receive a managed claim." This was **WRONG** — both methods exist at `src/backends/codex_native.py:373` and `:385`. The warning was outdated relative to the merged code.

---

## 1. Codex managed turn e2e — PASS

### Setup
- **Target carrier:** `kanebra` (local, online, `managed_backends=["claude","codex","opencode-server"]`)
- **Time:** 2026-10-07T16:48:12Z
- **Code verified:** `codex_native.py:373` (`supports_managed_turns`) and `:385` (`run_managed_turn`) — both present.
  `supports_managed_turns()` probes the codex binary via `resolve_codex_executable` + `_managed_protocol_supported`. On kanebra: binary at `/home/cifran/.local/bin/codex`.

### First attempt (infrastructure error, not build gap)
- Session `43406ab9c0b3` created, `turn_queue_enrolled=1` confirmed in DB.
- Turn `task_2657095c` submitted: `{"body":"reply OK","operation_id":"a82-codex-probe-001"}`
- **Result:** `failed`, `error=codex_adapter_failed`, `execution_time=0.03s`
- **Root cause:** repo_path `/tmp/scratch-codex-a82-probe` did not exist on kanebra at that moment.
  `codex_native._run` line ~795: `workspace = str(Path(cwd).resolve(strict=True))` raises `FileNotFoundError`.
  `_managed_failure` receives a non-`_NotSubmitted` exception with `mutation_submitted=False` → returns `None` → generic `codex_adapter_failed`.
  **Not a code build gap.** Path was created (`mkdir -p /tmp/scratch-codex-a82-probe`) and a fresh session used.

### Successful probe
```
POST /api/sessions
  {"backend":"codex","node_id":"kanebra","repo_path":"/tmp/scratch-codex-a82-probe"}
→ {"ok":true,"session":{"session_id":"1b059672c89e","backend":"codex","status":"idle","machine_id":"kanebra",...}}

DB: SELECT session_id,backend,machine_id,turn_queue_enrolled,status FROM sessions WHERE session_id='1b059672c89e';
→  1b059672c89e|codex|kanebra|1|idle   ← enrolled=1 ✓

POST /api/sessions/1b059672c89e/turn-requests
  {"body":"reply OK","operation_id":"a82-codex-probe-002"}
→ {"turn_id":"task_a0e13623","status":"queued","queue_sequence":1,"accepted_at":"2026-10-07T16:48:16.697027+00:00","source":"operator",...}
```

**Monitoring (DB poll):**
```
T+10s: completed
SELECT id,status,action,queue_protocol,claimed_by,turn_source,effects_state FROM mesh_tasks WHERE id='task_a0e13623';
→ task_a0e13623|completed|create_session|1|kanebra|human|done

SELECT result FROM mesh_tasks WHERE id='task_a0e13623';
→ {"success":true,"output":"OK","backend_session_id":"01a11744-6c9f-7c20-a90b-813ff6fde6e1",
   "execution_time":13.311838676221669,"errors":[],...}
```

**Evidence checklist:**
- `queue_protocol=1` — managed FIFO path ✓
- `status=completed` ✓
- `claimed_by=kanebra` ✓
- `success=True` ✓
- `output="OK"` — model replied as requested ✓
- `backend_session_id=01a11744-6c9f-7c20-a90b-813ff6fde6e1` — native session bound ✓
- `effects_state=done` — completion effects consumer ran ✓
- `execution_time=13.3s` — realistic codex execution time ✓

**Worker log confirmation (`logs/kanebra-out.log`):**
```
2026-10-07T16:42:17:  src.backends.codex_native: event=codex_app_server_ready pid=1769525
...
[task=task_a0e13623 session=1b059672c89e]  task_claimed
[task=task_a0e13623 session=1b059672c89e]  event=managed_result_acked task_id=task_a0e13623
```

**Session closed:** `POST /api/sessions/1b059672c89e/close` → `{"ok":true}` (2026-10-07T16:53:17Z)

---

## 2. opencode-server managed turn e2e — PASS

### Code verified
`opencode.py:1291` (`supports_managed_turns` returns `True` unconditionally for the server backend).
`opencode.py:1315` (`run_managed_turn` is implemented).

### Probe

```
POST /api/sessions
  {"backend":"opencode-server","node_id":"kanebra","repo_path":"/tmp/scratch-codex-a82-probe"}
→ {"ok":true,"session":{"session_id":"45c3626d40f2","backend":"opencode-server","status":"idle",
   "machine_id":"kanebra","default_model":"opencode/big-pickle",...}}

DB: turn_queue_enrolled=1 (born-managed) ✓

POST /api/sessions/45c3626d40f2/turn-requests
  {"body":"reply OK","operation_id":"a82-ocs-probe-001"}
→ {"turn_id":"task_b6557da4","status":"queued","queue_sequence":1,"accepted_at":"2026-10-07T16:49:18.365027+00:00","source":"operator",...}
```

**Monitoring:**
```
T+5s:  pending
T+10s: running
T+30s: completed

SELECT id,status,action,queue_protocol,claimed_by,turn_source,effects_state FROM mesh_tasks WHERE id='task_b6557da4';
→ task_b6557da4|completed|create_session|1|kanebra|human|done

result: {"success":true,"output":"OK","backend_session_id":"ses_ee8bab3cdffeYPEnzKnIHkyZ6T",
         "execution_time":19.360180069692433,"errors":[],...}
```

**Evidence checklist:**
- `queue_protocol=1` — managed FIFO path ✓
- `status=completed` ✓
- `claimed_by=kanebra` ✓
- `success=True` ✓
- `output="OK"` ✓
- `backend_session_id=ses_ee8bab3cdffeYPEnzKnIHkyZ6T` — native opencode session bound ✓
- `effects_state=done` ✓
- `execution_time=19.36s` — realistic opencode-server execution time ✓

**Session closed:** `POST /api/sessions/45c3626d40f2/close` → `{"ok":true}` (2026-10-07T16:53:17Z)

---

## 3. Agent-source turn-queue send — PASS

### Context
This session is worker `53dc2f3c328c` in Case `fc6661f439d74b9db10f213af2db119d`.
Sender capability confirmed active (unrevoked) in DB:
```
SELECT sender_session_id,case_id,role,revoked_at,generation,issued_task_id
FROM mesh_sender_capabilities WHERE sender_session_id='53dc2f3c328c';
→ 53dc2f3c328c|fc6661f439d74b9db10f213af2db119d|worker||1|task_7319fa47
```

The `mcp__ai_team_sender__send_instruction` MCP tool is provisioned to this session (available in tool registry).

### Probe
Target: `ee93d4266cd2` (another worker in the same Case).

```
mcp__ai_team_sender__send_instruction(
  target_session_id="ee93d4266cd2",
  body="reply OK",
  operation_id="a82-agent-send-probe-001"
)
→ "Accepted: turn_id=task_834db4e9 status=queued queue_sequence=2."
```

**DB verification (2026-10-07T16:53:01Z):**
```
SELECT id,status,session_id,turn_source,sender_session_id,queue_protocol,queue_sequence
FROM mesh_tasks WHERE id='task_834db4e9';
→ task_834db4e9|queued|ee93d4266cd2|agent|53dc2f3c328c|1|2
```

**Evidence checklist:**
- `turn_source=agent` — agent-source path (not operator/human) ✓
- `queue_protocol=1` — managed FIFO path ✓
- `queue_sequence=2` — durably queued AFTER the session's existing turn (no clobber) ✓
- `sender_session_id=53dc2f3c328c` — sender identity bound and verified ✓
- Distinct accepted `turn_id=task_834db4e9` ✓
- `status=queued` — admitted, not rejected ✓

**Note on opencode-server sender limitation:**
`opencode.py:1305` `provision_sender_capability` returns `False` unconditionally:
> "OpenCode's only MCP seams are per server PROCESS (opencode.json / OPENCODE_CONFIG_CONTENT at launch) or per directory INSTANCE (POST /mcp, 'add MCP server to the system'); there is no per-session MCP config. One opencode serve is shared by every session in the repo, so provisioning one session's scoped sender token would hand it to its neighbours. Agent send is therefore not available on OpenCode."
This is a deliberate design decision, not a build gap.

---

## Scratch session cleanup

| Session | Backend | Role | Outcome | Closed |
|---|---|---|---|---|
| `43406ab9c0b3` | codex | probe attempt 1 (failed: missing path) | `task_2657095c` failed | `POST /close` → ok (2026-10-07T16:53:17Z) |
| `1b059672c89e` | codex | probe attempt 2 (succeeded) | `task_a0e13623` completed | `POST /close` → ok (2026-10-07T16:53:17Z) |
| `45c3626d40f2` | opencode-server | probe (succeeded) | `task_b6557da4` completed | `POST /close` → ok (2026-10-07T16:53:17Z) |

No paid execution for scratch sessions (codex and opencode-server used local models; "reply OK" prompt). The agent-source send probe targeted an already-running worker session that would have processed the queued turn as part of its normal flow.

---

## Code references (verified on main HEAD `9b5520f`)

| Claim | File:line | What it says |
|---|---|---|
| Codex `supports_managed_turns` | `src/backends/codex_native.py:373` | Probes codex binary: `resolve_codex_executable` + `_managed_protocol_supported` |
| Codex `run_managed_turn` | `src/backends/codex_native.py:385` | Calls `_run_managed(session, message, ownership, ...)` |
| opencode-server `supports_managed_turns` | `src/backends/opencode.py:1291` | Returns `True` unconditionally |
| opencode-server `run_managed_turn` | `src/backends/opencode.py:1315` | Calls `_run_managed("turn", ...)` |
| Sender capability provisioning | `src/control/task_server.py:1112-1153` | Minted at claim time, returned in claim response |
| Worker capability provision | `src/worker/agent.py:1542-1569` | Worker applies grant to backend; backend calls `provision_sender_capability` |
| Agent send admission | `src/control/routes/turn_requests.py:104-165` | `AITeamSender` scheme → `_validate_agent_sender` → admitted as `source=agent` |
| opencode-server no sender | `src/backends/opencode.py:1305` | `provision_sender_capability` returns `False` (deliberate, documented) |

---

## Stage-8b gate status (per this validation)

The three preconditions flagged as "advisable but not met" in `A82_E2E_CERTIFICATION.md §5` are now **all met**:

- ✓ Stage 8a merged + live (MET — was already met)
- ✓ All sessions `turn_queue_enrolled=1`, no pending protocol-0 rows (MET — was already met)  
- ✓ A84 completion consumer live (MET — was already met)
- ✓ **Multi-backend managed live proof (ii and iii) — NOW MET (this document)**
- ✓ **Agent-source send live proof (i) — NOW MET (this document)**

**Case-outbox carry (o)** under A84 is still open (not addressed here — separate A84 scope).

Stage-8b may now proceed. The deletion scope is defined in `A82_E2E_CERTIFICATION.md §5`.
