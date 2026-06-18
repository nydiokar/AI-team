# Watched Jobs — Notify-on-Completion for Long-Running Scripts

> **Status:** Implemented. T3.1 resilience follow-up complete: running jobs are
> probed by PID + process identity and stale/reused PIDs are marked `lost`.
> **Created:** 2026-06-11
> **Owner concept:** a long-running script/process started on a worker machine
> can be *watched* so its completion is pushed to Telegram — **without** abusing
> the task/session lifecycle and **without** firing on every script an agent runs.

---

## 1. Problem Statement

An agent on a worker machine (e.g. `Horse`) is told to start a long-running
script (build, training run, data job, `pm2`-managed process). The script runs
for minutes-to-hours, often **detached** — the agent reports "script is running"
and the task turn ends. The script then runs owned by nobody: nothing is
subscribed to its completion, so when it finishes (or fails) **the user is never
told**.

The user wants to be notified via the Telegram gateway when such a script
finishes, and to have basic visibility (running / done / failed, exit code, tail
of output) — ideally as the seed of a future dashboard.

### Why this feels like it "violates the gateway direction"

The mental model "phone → server → worker" looks one-way. It is not: the gateway
**already** performs a server-initiated push on task completion
(`orchestrator` → `_session_reply_text` → `app.bot.send_message(chat_id=...)`).
That same return channel is the one we reuse here. We are **not** building a new
reverse portal; we are making a long script terminate in something the worker is
still subscribed to, so it lands on the push channel that already exists.

---

## 2. The Core Distinction (read this before designing anything)

A **task** (`mesh_tasks` row) is **one synchronous turn**: claim → execute →
post result. The worker holds a `max_concurrent` semaphore slot for its entire
duration (`src/worker/agent.py` `_handle_task`). A task is meant to be short and
to *end*.

A **watched job** is a **long-lived external process** whose lifecycle is
*orthogonal* to any task turn. It must NOT occupy a task slot, must NOT keep a
session `BUSY`, and must NOT enter the stale-busy reattach loop. It is a
**separate first-class entity** that merely *emits a completion event*.

> **The one-line rule:** a watched job is **not** a task turn. Model it as its
> own `jobs` table, never as a `mesh_tasks` row.

---

## 3. Goals (what we aim at)

- **G1 — Opt-in watching only.** A script is watched **only** when something
  explicitly registers a watch for it. Default behaviour for every other script
  an agent runs is **unchanged and silent.** (This is the "don't fire on every
  run" requirement — see No-Goal NG1.)
- **G2 — Notify on terminal state.** When a watched job exits, the user gets one
  Telegram message: job label, `done`/`failed`, exit code, and the last N lines
  of its log. Delivered through the existing `app.bot.send_message` push path.
- **G3 — Orthogonal lifecycle / zero blast radius on the gateway.** Watching a
  job for hours must not consume a task semaphore slot, must not hold any session
  `BUSY`, and must not feed the stale-busy reattach loop. A job that runs for 3
  days cannot starve coding tasks or jam a session.
- **G4 — Survives a worker/daemon restart.** The job runs detached; the watcher
  reconciles from persisted state (PID + start marker + log path) after a restart
  rather than losing the job.
- **G5 — Visibility / dashboard seed.** The `jobs` table is the backing store for
  a future dashboard: `running | done | failed`, exit code, started/finished
  timestamps, tail-of-log. (Aligns with the pre-declared `agent_runs` ambition in
  `src/control/db.py` header.)
- **G6 — Optional agent follow-up.** On completion, the control plane MAY
  auto-dispatch a normal follow-up task to the originating session ("your script
  finished, exit 0, here's the tail — proceed"). This is a config-gated extra,
  not the default.

---

## 4. No-Goals / How **NOT** to resolve this (explicit anti-solutions)

These are wrong on purpose-stated so a future agent doesn't "helpfully" build them.

- **NG1 — Do NOT auto-watch every script.** No global hook that fires a
  notification on every process an agent spawns. That would spam the user on
  every `npm test`, `git status`, `ls`. Watching is **always explicit
  registration** (G1). If in doubt, the default is **silent**.
- **NG2 — Do NOT model the job as a `mesh_tasks` row.** This is the trap. A task
  that stays `claimed` for hours pins a `max_concurrent` slot, keeps the session
  `BUSY`, and the gateway's stale-busy reattach loop will try to poll it to
  terminal on every restart. It conflates two lifecycles and is the exact state-
  management bug this spec exists to avoid. **Separate `jobs` table, always.**
- **NG3 — Do NOT keep the agent babysitting in the foreground as the solution.**
  A foreground wait + heartbeat keepalive (to dodge the inactivity timeout) is an
  acceptable *one-off manual workaround today*, but it is **not** the design: it
  still pins a task slot + session for the whole duration, it's fragile (wrapper
  dies but script lives, or vice-versa), and it drags in every edge case from the
  timeout problem. Do not enshrine it.
- **NG4 — Do NOT build a new inbound "notify my phone" endpoint that any process
  can hit freely.** A raw `POST /notify {chat_id, text}` open to arbitrary local
  processes IS the "portal" the user is rightly nervous about. Any completion
  signal must go through the **same control plane, same Tailscale perimeter, same
  `WORKER_TOKEN`**, addressed by `session_id`/`job_id` — one more authorized
  message on the existing return channel, not a second uncontrolled path.
- **NG5 — Do NOT mimic Claude/Codex in-process completion hooks.** Those are
  single-machine, in-process subprocess hooks; they cannot cross the mesh
  boundary (agent on `Horse`, gateway on the Pi5) to reach the phone. Reusing
  them here would die at the machine edge.
- **NG6 — Do NOT make completion depend on the script cooperating.** The watcher
  determines completion by **observing the process** (PID/pgid exit), not by
  trusting the script to call back. A script that crashes, is killed, or never
  got a "notify" line still produces a correct terminal event. (A script may
  *optionally* register itself via the agent's tooling, but detection must not
  *require* the script's cooperation.)

---

## 5. Approved Design (how we resolve it properly)

A new **`jobs`** table + a **job-watcher loop on the worker**, fully parallel to
the existing task path. Nothing in the current task/session lifecycle changes.

### 5.1 Flow

1. **Register (opt-in, G1/NG1).** Something explicitly registers a watch:
   `POST /jobs {session_id, label, command|attach_pid, cwd, notify: true}`.
   The worker spawns the command **detached** (own process group), or attaches to
   an existing PID, and writes a `jobs` row `status=running` with `pid`/`pgid`, a
   `started_at` marker, and a `log_path`. **The registering task turn returns
   immediately** — the agent is free, the session goes back to IDLE (G3).
2. **Watch (G4/NG6).** A worker **job-watcher loop** (sibling of `_poll_loop` /
   `_heartbeat_loop` in `src/worker/agent.py`) reaps finished PIDs by
   *observation* and on exit POSTs
   `POST /jobs/{id}/done {exit_code, tail}` to the control plane. After a daemon
   restart it reconciles `running` rows from `pid`+`started_at` (guard against PID
   reuse with pgid/start-time).
3. **Notify (G2/NG4).** The control plane sets the `jobs` row terminal; the
   gateway notices (same poll-or-push pattern that already drives task
   completion) and calls `app.bot.send_message(chat_id)` for the owning session.
4. **Optional follow-up (G6).** If `notify_agent` is set, the orchestrator
   dispatches a normal `resume_session` task to the originating session carrying
   the exit code + tail.

### 5.2 Schema (new table; use the existing migration framework)

`src/control/db.py` already has a numbered migration framework (`_get_migrations`,
`_CURRENT_VERSION`). Add `jobs` as the next migration — **do not** touch
`mesh_tasks` (NG2).

```
CREATE TABLE jobs (
    id            TEXT PRIMARY KEY,
    session_id    TEXT,                 -- owning session (for notify routing); may be NULL
    node_id       TEXT NOT NULL,        -- worker that owns the process
    label         TEXT NOT NULL,        -- human label for the Telegram message
    command       TEXT,                 -- the command line (NULL if attached to an existing PID)
    pid           INTEGER,
    pgid          INTEGER,              -- process-group id; guards PID reuse
    started_at    TEXT NOT NULL,
    started_epoch REAL,                 -- start time; second guard against PID reuse
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | lost
    exit_code     INTEGER,
    log_path      TEXT,                 -- file the worker tails for the summary
    tail          TEXT,                 -- last N lines, filled on completion
    last_checked_at TEXT,               -- latest worker liveness probe
    last_probe_error TEXT,              -- probe failure detail, if identity couldn't be read
    last_seen_command TEXT,             -- command observed on the worker host
    last_seen_started_epoch REAL,       -- process creation/start epoch observed on the worker
    notify        INTEGER NOT NULL DEFAULT 1,       -- send Telegram on completion
    notify_agent  INTEGER NOT NULL DEFAULT 0,       -- also dispatch a follow-up task (G6)
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
```

`status=lost` = a `running` row whose PID/pgid no longer matches after a restart
and can't be confirmed dead-or-alive; surfaced, not silently dropped.

### 5.3 Endpoints (control plane — same auth as everything else, NG4)

- `POST /jobs` — register/spawn a watch (Bearer `WORKER_TOKEN`).
- `POST /jobs/{id}/done` — worker reports terminal state.
- `POST /jobs/{id}/probe` — worker reports the latest process-identity probe.
- `GET  /jobs` / `GET /jobs/{id}` — list/inspect (dashboard + `/jobs` Telegram cmd).

---

## 6. Blast-Radius Summary (why this design, in the user's three cost terms)

| Concern | Approved design (jobs table + watcher) |
|---|---|
| **New machinery** | **Medium.** 1 migration, ~3 endpoints, 1 watcher loop, 1 notify branch. All **additive and parallel** — no restructuring of the task/session path. |
| **Interference with the live gateway** | **Low by construction.** Jobs never touch the semaphore, never hold a session `BUSY`, never enter the stale-busy reattach loop. A multi-day job cannot starve tasks or jam a session (G3). |
| **Future fuckups** | **Lowest of the options.** Contained failure modes: worker dies → watcher reconciles from PID/`jobs`; control plane dies → worker retries `done` POST (same posture as task results). No fragile heartbeat (NG3), no timeout coupling. Main edge = PID reuse → guarded by pgid + start-epoch. |

Rejected alternatives and why: **§4 NG2** (script-as-task: HIGH gateway blast
radius), **§4 NG3** (foreground babysit: fragile + pins slot/session), **§4 NG4**
(open notify endpoint: the portal).

---

## 7. Acceptance Criteria

- **A1 (G1/NG1):** Running an unwatched script produces **no** Telegram message.
  Only an explicitly-registered job notifies. Covered by a test that runs a
  process without registration and asserts zero notifications.
- **A2 (G2):** A registered job that exits 0 produces exactly **one** Telegram
  message with label, status, exit code, and a non-empty tail. Exit ≠ 0 →
  `failed`. No duplicate messages.
- **A3 (G3):** While a job runs for an extended period, `max_concurrent` task
  slots stay free and the owning session is **not** `BUSY` — assert a coding task
  can be claimed and a session conversation can proceed concurrently.
- **A4 (G4):** Kill + restart the worker daemon while a job is running; the
  watcher reconciles the `running` row and still reports the correct terminal
  state (or marks it `lost`, never silently drops it).
- **A5 (NG2):** No `mesh_tasks` row is created for a job. The stale-busy reattach
  loop is never handed a job. (Assert by inspection + test.)
- **A6 (G5):** `GET /jobs` (and a `/jobs` Telegram command) lists running and
  recent jobs with status/exit/tail.
- All tests honor the **test cost guard** — no paid Claude/Codex CLI invoked
  (`AI_TEAM_TEST_MODE`); use a trivial real process (e.g. `sleep`, a script that
  exits N) as the watched job, never a backend.

---

## 8. Where to look (implementation pointers)

- `src/control/db.py` — `_get_migrations` / `_CURRENT_VERSION` (add `jobs`
  migration here; **do not** alter `mesh_tasks`); mirror `enqueue_task` /
  `complete_task` style for `register_job` / `complete_job` / `get_jobs`.
- `src/worker/agent.py` — add a `_job_watcher_loop` sibling to `_poll_loop` /
  `_heartbeat_loop`; spawn detached (own pgid); reap by observation (NG6);
  reconcile on startup (G4). Register it in `run()` alongside the other loops.
- `src/control/task_server.py` — add `/jobs` endpoints (same Bearer auth).
- `src/orchestrator.py` — completion-notice branch that turns a terminal `jobs`
  row into `app.bot.send_message` via the existing `_session_reply_text`-style
  path; optional `notify_agent` → dispatch a `resume_session` task (G6).
- `src/telegram/interface.py` — `/jobs` command for visibility (G5).

---

## 9. Build order (each piece independently shippable + testable)

1. **Schema** — `jobs` migration only. Ship, verify `schema_version` bumps, no
   effect on running system.
2. **Register + spawn detached** — `POST /jobs` + worker spawns + writes row.
   No notify yet. Test: row appears `running`, agent/session free (A3).
3. **Watcher + done** — `_job_watcher_loop` reaps + `POST /jobs/{id}/done`.
   Test: exit code captured, restart reconciliation (A4).
4. **Notify** — orchestrator terminal-row → Telegram push. Test: A1, A2.
5. **Visibility** — `GET /jobs` + `/jobs` Telegram command (A6, G5).
6. **Optional** — `notify_agent` follow-up dispatch (G6).

> Test cost guard applies throughout — never invoke the paid Claude CLI from
> tests; watched-job tests use trivial real processes, not backends.
