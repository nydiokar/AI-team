# Telegram ↔ Web UI Parity Map

> **Branch**: `feat/webui-ui0`  
> **Date**: 2026-06-25  
> **Goal**: The Web UI should replicate every user-facing Telegram capability.  
> Read before building anything. Update this table as work lands.

---

## 1 · Full Telegram surface inventory

The table below enumerates every command and inline-button flow in
`src/telegram/interface.py`.  Backend column = the gateway service / API method
the action calls.

| # | Telegram entry point | What it does | Backend method / endpoint | UX pattern |
|---|---|---|---|---|
| 1 | `/start` | Welcome + quick-start text | — (static) | Text reply |
| 2 | `/help` | Full command reference | — (static) | Text reply |
| 3 | Plain text (no command) | Submit instruction to active session (or one-off) | `orchestrator.submit_instruction` | Free-text, debounced 3 s |
| 4 | `/task <instruction>` | One-off task without a session | `orchestrator.submit_instruction` | Free-text args |
| 5 | `/session_new` (no args) | Guided new-session wizard: backend → (node picker if mesh) → repo picker | `session_service.create_session` | Multi-step inline-button picker |
| 6 | `/session_new <backend> <path>` | Direct new session, path resolved via PathResolver | `session_service.create_session` | Args |
| 7 | Backend picker buttons (`session_new_backend:*`) | Step 1 of wizard | — | Inline button → next step |
| 8 | Node picker buttons (`session_new_node:*`) | Step 2 of wizard (only when remote workers online) | DB `list_nodes` | Inline button → next step |
| 9 | Repo picker buttons (`session_new_repo:*`) | Step 3 of wizard — auto-discovers repos from `WORKER_PROJECTS_ROOT` | `_local_repo_choices` / `_repo_choices_for_node` | Inline button → create |
| 10 | Back buttons in wizard | Navigate backwards through steps | — | Inline button |
| 11 | Cancel button in wizard | Abort new-session flow | — | Inline button |
| 12 | `/session_list` | Open sessions list + switch picker + closed-count hint | `session_store.list_all` | Text + inline buttons |
| 13 | Session switch picker (`session_use:*`) | Tap to switch active session | `session_store.bind` | Inline button callback |
| 14 | `/session_closed` | Browse closed sessions with restore buttons | `session_store.list_all` | Text + inline buttons |
| 15 | Restore picker (`session_restore:*`) | Restore a closed session from the list | `session_service.restore_session` | Inline button callback |
| 16 | `/session_use [id]` | Switch active session (text arg or picker) | `session_store.bind` | Args or inline picker |
| 17 | `/session_status [id]` | Rich detail card for a session (includes dirs via NodeInspector) | `_inspect("list_dirs")` | Text card |
| 18 | `/session_cancel [id]` | Stop the running task in a session | `orchestrator.cancel_task` + `mark_cancelled` | Text |
| 19 | `/session_close [id]` | Close (archive) a session | `session_service.close_session` | Text |
| 20 | `/session_restore [id]` | Restore a closed session + make active | `session_service.restore_session` | Text or picker |
| 21 | `/compact [id]` | Collapse Claude context window | `orchestrator.compact_session` | Text confirmation |
| 22 | `/model` (no args) | Show current model + inline picker | `session_service.set_model` | Inline button picker |
| 23 | `/model <name>` | Set model directly by name | `session_service.set_model` | Args |
| 24 | Model set buttons (`model_set:*`) | Select a model from picker | `session_service.set_model` | Inline button callback |
| 25 | `/session_dirs [path]` | Browse directories in session repo (routed to owning node) | `_inspect("list_dirs")` | Text list |
| 26 | `/git_status [id]` | Git status of session repo (routed to owning node) | `_inspect("git_status")` | Text report |
| 27 | `/commit [id] [flags]` | Safe commit of session changes | `_inspect("commit")` | Text result |
| 28 | `/commit_all [id] [flags]` | Commit all staged changes | `_inspect("commit_all")` | Text result |
| 29 | `/status` | Gateway health dashboard + active session card | `orchestrator.get_status` + `session_store` | Text card |
| 30 | `/nodes` | Mesh worker list: online full, offline compact, ancient as count | DB `list_nodes` + live_state | Text list |
| 31 | `/node <id>` | Single node detail: backends, repos, load, heartbeat | DB `get_node` | Text card |
| 32 | `/jobs [limit]` | Watched jobs: running and recently finished | DB `list_jobs` | Text list |
| 33 | File / photo upload (document handler) | Upload a file to the session repo (local) or staging (remote), then optionally submit instruction from caption | `orchestrator.submit_instruction` + filesystem write | File message + optional caption |

---

## 2 · Web UI parity gap table

Key: ✅ done · ⚠️ partial · ❌ missing · N/A not applicable to Web

| Feature | Web UI status | Backend endpoint | Notes |
|---|---|---|---|
| Welcome / orientation text | ✅ Token gate + empty states suffice | — | N/A (no "start" concept on web) |
| Help reference | ❌ Missing | — | Could be a `/help` route or an info drawer |
| **Natural language input** to active session | ✅ `Composer` → `POST /api/instructions` | exists | Core loop done |
| One-off task (no session) | ⚠️ Composer sends with no sessionId but UX doesn't explain | `POST /api/instructions` | Add tooltip / empty-state note |
| **New session** — backend picker | ✅ `NewSessionSheet` has backend buttons | `POST /api/sessions` | Done |
| **New session** — repo picker (auto-discover) | ❌ Missing — sheet has a free-text input | needs `GET /api/projects` (new) | **Priority 1** — Telegram wizard discovers repos from filesystem; web shows a raw path field |
| **New session** — node picker (mesh) | ❌ Missing | `GET /api/nodes` exists | Show node selector when >0 remote workers |
| **New session** — model selector (in wizard) | ❌ Missing | `POST /api/sessions/{id}/model` exists | Optional at create time; Telegram sets it post-create via `/model` |
| Session list | ✅ `SessionsScreen` with open/closed/attention groups | `GET /api/sessions` | Done |
| Switch active session | ✅ Tapping a `SessionRow` navigates to `SessionDetailScreen` | — | The web concept of "active" is the currently viewed session |
| Browse closed sessions + restore | ✅ Closed group in `SessionsScreen` + Restore button in `SessionDetailScreen` | `POST /api/sessions/{id}/restore` | Done |
| Session detail / status card | ⚠️ Header shows status chip + backend/target; missing dirs listing, model, machine, last task | `POST /api/sessions/{id}/inspect` | Add an expandable info panel |
| **Stop task** (cancel in-flight) | ✅ Stop button in `SessionDetailScreen` menu | `POST /api/sessions/{id}/stop` | Done |
| Close session | ✅ Close in session detail menu | `POST /api/sessions/{id}/close` | Done |
| Restore session | ✅ Restore in session detail menu | `POST /api/sessions/{id}/restore` | Done |
| **Compact context** | ❌ Missing | `POST /api/sessions/{id}/compact` exists | Add to session detail action menu |
| **Model picker** | ❌ Missing | `POST /api/sessions/{id}/model` exists | Add to session detail action menu; needs `GET /api/models?backend=<b>` (new, or inline list) |
| **Session dirs browser** | ❌ Missing | `POST /api/sessions/{id}/inspect` with `op=list_dirs` exists | Could live inside session detail as a file-tree pane |
| **Git status** | ❌ Missing | `POST /api/git/status` exists (gateway-level); per-session via inspect | Add to session detail action menu |
| **Git commit** | ❌ Missing | `POST /api/git/commit` exists | Add to session detail action menu |
| **Git commit_all** | ❌ Missing | `POST /api/git/commit_all` exists | Add to session detail action menu |
| Gateway health / status | ⚠️ `SystemScreen` shows nodes + activity log but no summary chip | `GET /api/nodes` | Add a "healthy/degraded" headline chip |
| **Nodes list** | ✅ `SystemScreen` shows node cards with heartbeat | `GET /api/nodes` | Done (no offline history / ancient count) |
| **Node detail** | ❌ Missing | `GET /api/nodes` (row has backends, repos, load) | Add a node detail sheet/modal |
| **Jobs list** | ❌ Missing | `GET /api/jobs` exists | Add a Jobs tab or panel in SystemScreen |
| **File upload** | ❌ Missing | needs `POST /api/sessions/{id}/upload` (new backend endpoint) | **Priority 2** — Telegram delivers files to session repo; web needs a file picker + upload |
| Task list / approval queue | ✅ `TasksScreen` + approvals in SessionDetail | `GET /api/tasks`, `GET /api/approvals` | Done |

---

## 3 · New backend endpoints required

Only two gaps need new HTTP surface — everything else already exists:

| Endpoint | Purpose | Telegram equivalent |
|---|---|---|
| `GET /api/projects?node_id=<id>` | List discoverable repos for a node — the same `_repo_choices_for_node` logic Telegram calls during the wizard. Returns `[{name, path}]`. Local node reads filesystem; remote reads DB row `repos` JSON. | `_local_repo_choices` / `_repo_choices_for_node` |
| `POST /api/sessions/{id}/upload` | Accept a multipart file upload, write it to `session.repo_path/uploads/` (local) or stage under `state/uploads/` (remote), then optionally auto-submit an instruction (the caption equivalent). | `_handle_document` |
| `GET /api/models?backend=<b>` | Return the model list for a backend (`config.models.options`). Needed for the model picker — avoids hardcoding model names in the frontend. | `_build_model_set_markup` reads `config.models.options` |

---

## 4 · Prioritized port plan

### Priority 1 — Repo picker in NewSessionSheet (high friction gap)

The single biggest UX cliff: Telegram users never type absolute paths; they pick
from a list. Web users currently must know and type the exact path.

**Plan:**
1. Add `GET /api/projects?node_id=__local__` to `control_api.py` — thin wrap over `_local_repo_choices` / `_repo_choices_for_node`.
2. In `NewSessionSheet.tsx`: replace the free-text `<input>` with an async repo picker that fetches from the new endpoint. Keep a free-text fallback (manual path) below the list for advanced use.
3. When mesh workers exist, add a node selector step before the repo picker (same 3-step flow as Telegram).

**Scope**: ~80 lines backend + ~120 lines frontend. No deferred-track work.

### Priority 2 — File upload

Users can't share screenshots, design files, or code snippets with the agent via web today.

**Plan:**
1. Add `POST /api/sessions/{id}/upload` (multipart) — mirrors `_handle_document` logic.
2. In `Composer.tsx`: add a paperclip button that opens a native file picker. On select, POST to upload endpoint, then optionally submit an instruction with the file reference.

**Scope**: ~60 lines backend + ~80 lines frontend.

### Priority 3 — Model picker in session detail

Switching models is a common tuning operation. Currently web-only users cannot do it.

**Plan:**
1. Add `GET /api/models?backend=<b>` to `control_api.py`.
2. In `SessionDetailScreen.tsx` action menu: add "Change model" → sheet with model list → `POST /api/sessions/{id}/model`.

**Scope**: ~30 lines backend + ~80 lines frontend.

### Priority 4 — Compact context

One menu item in the session action menu.

**Plan:** Add "Compact context" to the existing `MoreVertical` menu in `SessionDetailScreen.tsx`. Calls `POST /api/sessions/{id}/compact` (already exists). Show a toast on success/error.

**Scope**: ~15 lines frontend, 0 backend.

### Priority 5 — Git actions (status / commit / commit_all)

Useful for the phone-as-a-commit-remote use-case.

**Plan:** Add a "Git" sub-menu or sheet in session detail that shows:
- Git status (branch, clean/dirty, file counts)
- Commit button (safe commit) + Commit All button
- Each calls existing endpoints via `POST /api/sessions/{id}/inspect`.

**Scope**: ~100 lines frontend, 0 backend (endpoints exist).

### Priority 6 — Session info panel (dirs, model, machine)

Telegram's `/session_status` shows a rich card with dirs, model, machine, last task.

**Plan:** Add an expandable "Info" section in `SessionDetailScreen` that calls `POST /api/sessions/{id}/inspect` with `op=list_dirs`, and renders current model + target node.

**Scope**: ~50 lines frontend, 0 backend.

### Priority 7 — Node detail sheet

When tapping a node in SystemScreen, show a detail sheet (backends, repos, load, last heartbeat). Data is already in the `GET /api/nodes` response.

**Scope**: ~60 lines frontend, 0 backend.

### Priority 8 — Jobs panel

Add a collapsible "Jobs" section to `SystemScreen` using `GET /api/jobs`.

**Scope**: ~60 lines frontend, 0 backend.

### Priority 9 — Help / onboarding

An info drawer or `/help` route explaining the app.

**Scope**: ~40 lines frontend, 0 backend.

---

## 5 · What is intentionally out of scope

- Approvals / workflow automation → `docs/DEFERRED.md`
- Rate limiting (web auth token is already a sufficient gate)
- Telegram-specific formatting (hashtag search links, message threading)
- Notification push (separate spec phase)
