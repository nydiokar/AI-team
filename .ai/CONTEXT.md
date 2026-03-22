# AI-Team Gateway - Project Context

**Last Updated:** 2026-03-22  
**Branch:** `main`  
**Status:** Session-first gateway implemented; production hardening in progress

---

## What this project is

A Telegram-controlled remote gateway for local coding agents such as Claude Code and Codex.

It is not a general autonomous agent framework.

Target flow:
- open a session from Telegram
- route follow-up messages to the active session
- resume the native backend session on demand
- keep gateway state file-backed, inspectable, and bounded

Canonical intent lives in [.ai/context/production_vision.md](C:/Users/Cicada38/Projects/AI-team/.ai/context/production_vision.md).

---

## Current state

The core session architecture is in place and working in code:
- file-backed sessions
- Telegram session routing
- backend abstraction for Claude/Codex
- native resume support
- session summaries and session event logs
- standalone task fallback

On 2026-03-22, a production-hardening pass was completed across command UX, path safety, and session state handling.
On the same date, the active execution path was also aligned further with the new vision:
- native backend runtime stays in control
- local prompt-rewrite and local agent-selection logic are no longer used on the hot path
- the session state machine remains the primary intended path

### What was just changed

- Added a shared path resolver in [src/core/path_resolver.py](C:/Users/Cicada38/Projects/AI-team/src/core/path_resolver.py)
  - validates session paths
  - normalizes relative paths against configured workspace scope
  - suggests close directory matches for bad paths
  - lists child directories for operator guidance
- Updated [src/telegram/interface.py](C:/Users/Cicada38/Projects/AI-team/src/telegram/interface.py)
  - help/start text now reflects the real session-first product
  - `/session_new` now validates and resolves paths before creating a session
  - `/session_dirs` added
  - `/session_cancel` added
  - `/run` added
  - `/say` added
  - session ownership checks added to session operations
  - successful session creation now shows top directories in the chosen repo
  - git command handlers no longer assume task results are dicts
- Updated [src/orchestrator.py](C:/Users/Cicada38/Projects/AI-team/src/orchestrator.py)
  - session task creation can take an explicit `cwd`
  - session states now move through `BUSY`, `AWAITING_INPUT`, `CANCELLED`, `ERROR`
  - status payload now exposes Telegram and workspace scope info
  - removed LLAMA prompt rewriting from task execution so raw user intent reaches the backend runtime
  - reduced one-off task wrapping to a minimal shell around the raw instruction
- Updated [src/bridges/claude_bridge.py](C:/Users/Cicada38/Projects/AI-team/src/bridges/claude_bridge.py)
  - execution cwd resolution now uses the shared path resolver
- Updated [src/validation/engine.py](C:/Users/Cicada38/Projects/AI-team/src/validation/engine.py)
  - removed AgentManager-based thresholds from active validation flow
- Updated [main.py](C:/Users/Cicada38/Projects/AI-team/main.py)
  - fixed the old Telegram display bug in `status`
  - stopped surfacing `agents_enabled` as if it were still a product-level switch
- Added focused tests in [tests/test_path_resolver.py](C:/Users/Cicada38/Projects/AI-team/tests/test_path_resolver.py)

### What exists and works

| Component | Location | Notes |
|:----------|:---------|:------|
| Task orchestrator | `src/orchestrator.py` | Queue, workers, retries, cancellation, event log, session updates |
| Telegram interface | `src/telegram/interface.py` | Session commands, one-off task commands, git commands |
| Session store | `src/core/session_store.py` | File-backed CRUD and active Telegram binding |
| Path resolver | `src/core/path_resolver.py` | Safe path resolution and suggestions |
| Claude backend | `src/backends/claude_code.py` | Native `--resume` support |
| Codex backend | `src/backends/codex.py` | Native session resume support |
| Claude bridge | `src/bridges/claude_bridge.py` | Stateless execution for one-off tasks |
| Event log | `logs/events.ndjson` | System-wide NDJSON events |
| Session event log | `logs/session_events/<session_id>.log` | Per-session turn log |
| Session summaries | `state/summaries/<session_id>.md` | Compact readable session summary |
| Session state | `state/sessions/<session_id>.json` | Full session record |
| Telegram bindings | `state/telegram/active_bindings.json` | chat_id -> session_id |

---

## Command surface now intended

### Session commands

- `/session_new <backend> <path>`
- `/session_list`
- `/session_use <session_id>`
- `/session_status [session_id]`
- `/session_dirs [path]`
- `/session_cancel [session_id]`
- `/session_close [session_id]`

### Execution commands

- plain text -> continue active session if one exists
- `/say <instruction>` -> session-only execution
- `/run <instruction>` -> active session if present, otherwise one-off task
- `/task <instruction>` -> one-off task only
- `/progress <task_id>`
- `/cancel <task_id>`
- `/status`

### Git commands

- `/git_status`
- `/commit <task_id> [--no-branch] [--push]`
- `/commit_all <task_id> [--no-branch] [--push]`

---

## Production gaps that still remain

These are the material next steps, not doc cleanup.

### 1. Live end-to-end validation

Still required:
1. `/session_new claude <repo_path>`
2. send first message
3. verify `backend_session_id` is captured in `state/sessions/<id>.json`
4. send second message
5. verify backend resumes the existing conversation

This is still the most important production gate.

### 2. Workspace scope decision

`python main.py doctor` currently shows:
- `Base CWD: None`
- `Allowed root: None`

That means the new path validation UX is in place, but the workspace boundary is not configured yet.

Open decision:
- If `allowed_root` is set too narrowly to one target repo, Claude may be unable to edit this gateway repo or other intended workspaces.
- If it is left unset, path safety becomes much weaker.

Recommended direction:
- set `CLAUDE_BASE_CWD` and `CLAUDE_ALLOWED_ROOT` to the parent workspace that is intentionally allowed
- example: the projects directory, not a single repo, if the gateway is meant to operate across multiple repos there
- include the gateway repo itself inside that allowed root if the bot should be able to work on itself

In short: yes, these should be shown by `doctor`; no, they should not be set so narrowly that the gateway cannot edit intended repos, including itself when desired.

### 3. Full test suite reconciliation

There are still stale tests from the older architecture.

Known example:
- [tests/test_permissions.py](C:/Users/Cicada38/Projects/AI-team/tests/test_permissions.py) still expects per-task tool routing that no longer exists

Production requires:
- remove or rewrite stale tests
- add focused tests for session commands and session ownership
- add tests for `/session_new` path correction and suggestion behavior

### 4. Remaining cleanup

- Decide whether to remove `create_task_from_expanded` in [src/orchestrator.py](C:/Users/Cicada38/Projects/AI-team/src/orchestrator.py)
- Decide how much dormant local-agent scaffolding to publish
  - keep `src/bridges/llama_mediator.py` as future local operational layer
  - keep `src/core/agent_manager.py` only as clearly marked dormant code
  - keep `prompts/agents/*` only if they are explicitly described as dormant / future-facing
- Review Telegram output strings for consistency and remove remaining old task-runner wording
- Validate git command UX against real completed tasks and real repos

### 5. Publish cleanup inventory

#### Keep as active product surface

- `src/telegram/interface.py`
- `src/orchestrator.py`
- `src/backends/claude_code.py`
- `src/backends/codex.py`
- `src/core/session_store.py`
- `src/core/path_resolver.py`
- `src/bridges/claude_bridge.py`
- `main.py`
- `.ai/CONTEXT.md`
- `.ai/context/production_vision.md`

#### Keep, but mark clearly as dormant / future layer

- `src/bridges/llama_mediator.py`
- `src/core/agent_manager.py`
- `prompts/general_prompt_coding.md`
- `prompts/agents/*`

These should not be described as active product behavior in public docs.

#### Already removed because they described the wrong product

- `tests/test_agent_disable.py`
- `tests/test_agent_system.py`
- `tests/test_unified_prompts.py`
- `tests/test_manual_agent.task.md`
- `tests/full_prompt_test.py`
- `tests/debug_test.py`

#### Likely archive or replace before publish

The following docs are likely to confuse users unless rewritten:
- `docs/README.md`
- `docs/QUICK_START.md`
- `docs/ROADMAP.md`
- `docs/PROMPT_COMPARISON.md`
- `docs/structure/*`
- `docs/where we left off/*`
- `docs/archive/*`
- `docs/features/*`
- `docs/IMPLEMENTATION_ROADMAP.md`

Reason:
- they describe the old orchestrator / agent-template / roadmap history rather than the current session-first gateway

---

## Recommended immediate next move

1. Decide and set `CLAUDE_BASE_CWD` and `CLAUDE_ALLOWED_ROOT`
2. Run the live Telegram session-resume test
3. Clean out stale tests and replace them with session-first coverage
4. Then update docs outside `.ai/`

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | Main orchestrator, task/session execution flow |
| `src/telegram/interface.py` | Telegram command surface |
| `src/core/path_resolver.py` | Shared path validation and suggestions |
| `src/core/session_store.py` | File-backed session store |
| `src/core/interfaces.py` | Session/task/backend dataclasses and enums |
| `src/backends/claude_code.py` | Claude native session create/resume |
| `src/backends/codex.py` | Codex native session create/resume |
| `src/bridges/claude_bridge.py` | Stateless one-off execution |
| `main.py` | CLI entrypoints and status/doctor display |
| `.ai/context/production_vision.md` | Canonical product intent |

---

## Architecture rules

- Session continuity uses native backend resume, not terminal persistence
- State stays file-backed
- LLAMA remains narrow and optional
- Session ownership and path scope must stay explicit
- No uncontrolled autonomous behavior
