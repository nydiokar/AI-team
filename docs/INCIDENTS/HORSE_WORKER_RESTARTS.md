# Incident ledger — Horse worker daemon self-restarts

**Purpose:** a triangulation ledger for the recurring failure mode "the `ai-team-worker`
daemon on node **Horse** exits and PM2 auto-restarts it, bumping its incarnation and
marking all co-resident continuous-driver sessions `lost`." Each occurrence nukes every
persistent session on Horse (a pinned Manager cannot be resumed → `error_class=session_lost`).

This is **not** a hotfix log. It exists so that when this happens again we can compare
occurrences and separate a one-off from a pattern. **Append every new occurrence to the
table + add a dated record. Do not delete old records.**

Owner: gateway on-call · Created: 2026-08-22 · Last updated: 2026-08-22

---

## Occurrence queue (append newest at the bottom)

| Date (UTC) | Restarts | Trigger task (suspected) | Exit signature | OOM? | OS crash? | Root cause | Ref |
|---|---|---|---|---|---|---|---|
| 2026-08-14 01:11 → 04:43 | ≥2 (+ 08-15 22:34 count=8) | — (SDK stream-json poison era) | `Command failed with exit code 129` / `sdk_reader_stream_ended` | no | no | SDK stream-json buffer poisoning (partial); PR #92 mitigations | CONTEXT 2026-08-14 note |
| 2026-08-22 09:38:22 → 09:46:41 | 2 in ~8 min | `task_99d385ab` (opus `analyze`, RERUN_1) | exit_code=1, **no traceback**, silent | no (16 GB free) | no (no WER/Event 1000/dump) | **UNCONFIRMED** — silent exit-1; strongest lead = claude.exe subprocess fault during a long opus turn (structural match to 08-15 exit-129) | this doc §2026-08-22 |

---

## 2026-08-22 — two restarts in 8 minutes while one opus task ran

### What broke (operator-visible)
Manager session `aadc24894dc1` (pinned to Horse, repo `C:\Users\Cicada38\Projects\tokens_ingest`,
Case `dd283438162546459a929d1fba1916df`) ran two clean turns (09:12 ✓, 09:31 ✓), then every
subsequent turn failed instantly with
`[Horse] Claude session was lost after a worker restart and cannot be resumed by the continuous driver.`
The operator had started nothing — this was a worker-side self-restart.

### Gateway-side evidence (`logs/pm2-out.log`, `logs/events.ndjson`, `state/mesh.db`)
- **Two full process restarts**, each an incarnation bump (`src.control.node_registry`):
  - `09:38:22Z` `49b2f403… → 04222d9d…` — `driver_sessions_marked_lost count=6`, `orphaned_claims_released task_ids=['task_99d385ab']`
  - `09:46:41Z` `04222d9d… → c6b609e88b51…` — `orphaned_claims_released task_ids=['task_99d385ab']`
- **The same task, `task_99d385ab`, was the orphaned claim in BOTH restarts.** Last live
  activity `09:44:48` "Using Bash"; no `task_result_posted` ever emitted for it.
- Gateway declared `task_99d385ab` **timeout** at `09:46:42` (`elapsed=600s`), which finally
  made the poison task terminal and stopped the re-claim loop (it was *not* re-claimed after
  restart 2).
- Horse re-registered healthy at `09:52:31Z`, `online`, `live_sessions: 0`, incarnation `c6b609e88b51…`.
- The gateway host's own `kanebra-worker` had **14 days uptime** — the fault is isolated to Horse.

### Horse-box evidence (collected by a diagnostic agent dispatched onto Horse, session `808afc069336`, task `task_eeb974b3`)
- **Supervisor:** PM2 fork mode, `ai-team-worker` (pm2 id 15), script `worker_main.py` via
  `.venv\Scripts\pythonw.exe`. `autorestart:true`, `max_restarts:10`, `restart_delay:5000`,
  **`max_memory_restart` NOT set**. `restart_time:2`, **`exit_code:1`** both times, `unstable_restarts:0`.
- **Worker logs:** stdout shows a clean **startup banner** (`event=mesh_db_ready` → `driver_selected`
  → `registered`) at each restart, preceded by a **multi-minute gap of total silence** (7m26s
  before restart 1; ~1m53s before restart 2). **No Python traceback**, no error lines before death.
  `pm2-worker-error.log` (today) contained only `Loaded environment from …\.env` — **no traceback**.
- **After restart 1** the worker immediately **re-claimed `task_99d385ab`** and did
  `action=create_session session_id=d9ca6aa7e4db` (a *fresh* session for the same id), installed
  `sdk_stream_resync_installed max_buffer_bytes=16777216`, spawned the bundled claude.exe — then
  died again ~8 min later.
- **Exit cause:** clean-ish `exit_code=1`, **no** Application Error / .NET Runtime / WER / faulting
  module for pythonw.exe in the Windows Event Log window; no crash dump written today. (Two
  `LiveKernelEvent` GPU-watchdog WER entries at 09:32 UTC referenced **stale** dumps from
  2026-04-12 / 2026-06-10 — unrelated.)
- **Resource state:** 31.5 GB RAM, ~16 GB free, 48.6% util. **OOM ruled out.**
- **CLI:** SDK-bundled `claude.exe 2.1.191`, `claude_agent_sdk 0.2.110`. `CLAUDE_SDK_CLI_PATH` /
  `CLAUDE_SDK_CLI` **not set** (uses the bundled June CLI, per the 08-14 note).

### Ruled OUT
- Operator/gateway action (no interrupt/kill events; gateway wasn't restarted).
- OOM (16 GB free, no memory limit, no OOM record).
- OS-level crash / access violation (no WER Event 1000, no dump, no faulting module).
- A single session's SDK-reader error propagating to process exit **via `_reader_loop`** — that
  path (`src/backends/claude_driver.py:948`) catches `except Exception`, logs
  `event=sdk_reader_stream_ended`, and fails only the pending turns; it does not re-raise.

### Live hypotheses (ranked)
1. **claude.exe subprocess fault during the long opus turn takes the worker down (PRIMARY).**
   Structural match to the 2026-08-15 `exit code 129` cluster. The opus RERUN_1 turn ran a long
   Bash command (multi-minute silence right before each death). A subprocess exit/hang that
   surfaces *outside* the guarded `_reader_loop` (e.g. during SDK client teardown or on the
   Windows Proactor subprocess transport) could terminate the process with no Python traceback.
   *Against:* no `Fatal error in message reader` line today (the resync guard may have changed
   error surfacing), and a normal unhandled exception would have printed a traceback.
2. **External termination of the process (SECONDARY).** exit_code=1 with **no** traceback is not
   the signature of a normal Python unhandled exception (which prints one). Something may have
   terminated pythonw.exe (a Windows-side kill / PM2 God restart / transient). *Against:* PM2 only
   restarts *after* exit and no kill record was found; two deaths correlated with the same task
   argue against pure coincidence.

### The real blocker: **the death is silent.** We cannot confirm the proximate trigger because no
traceback / subprocess exit code was captured. That logging gap is what turns a diagnosable crash
into an "unconfirmed." Fix it first (see §Fixes) so the *next* occurrence self-diagnoses.

### Amplifier (independently real, worth fixing): **poison-task re-claim.** An in-flight task whose
worker died mid-run is re-claimed on restart and can kill the worker again — a crash loop bounded
only by PM2 `max_restarts:10` and the 600s gateway dispatch-timeout. Each cycle wiped 6 then 8
co-resident driver sessions.

---

## Fixes (proposed 2026-08-22)

**Fix A — make the death observable (minimal, low-risk).** In `src/worker/agent.py`:
`faulthandler.enable()` at process start (writes native tracebacks to stderr even for hard
faults) and wrap `asyncio.run(agent.run())` in `main()` to `logger.exception(...)` +
flush on any `BaseException` before exit; log the backend subprocess **return code** on
`sdk_reader_stream_ended`. Effect: the next occurrence leaves a traceback + claude.exe exit code.
No happy-path behavior change. **Worker-side code — goes live only on the next Horse worker
redeploy (operator-gated; do NOT restart the worker reflexively).**

**Fix B — resilience (larger, needs approval + redeploy).** (i) **Poison-task quarantine:** if a
task is re-claimed after a worker death and the worker dies again, mark it suspect and refuse to
auto-re-claim (bound the crash loop before it nukes co-resident sessions). (ii) Harden task
supervision so a backend subprocess fault can only fail *that* task, never exit the process.
Do not apply blind — confirm the crash path with Fix A's traces first.

---

## How to investigate the NEXT occurrence (checklist)
1. `state/mesh.db` `nodes` row for Horse: `incarnation_id`, `last_heartbeat`, `live_state.live_sessions`.
2. `logs/pm2-out.log`: grep `node_id=Horse` for `node_registered` / `driver_sessions_marked_lost`
   / `orphaned_claims_released` — count restarts, note the orphaned `task_ids` (the suspect task).
3. On Horse: `pm2 jlist` → worker `exit_code`, `restart_time`, `pm_uptime`; read
   `logs/pm2-worker-error.log` **and its rotated files** for a traceback / `faulthandler` dump /
   `exit code 129` / `sdk_reader_stream_ended`.
4. Windows Event Log (Application) for `pythonw.exe` Application Error / WER around the death time;
   check `%LOCALAPPDATA%\CrashDumps`.
5. Compare against this ledger's table. Two+ occurrences on the same trigger-task class (long opus
   turn) confirms the poison-task hypothesis and justifies Fix B.
