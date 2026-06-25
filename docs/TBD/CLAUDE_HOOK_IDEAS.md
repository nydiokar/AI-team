# Claude Hook Ideas

Claude Code hooks are worth tracking as a possible leverage point for the Telegram gateway, especially where we want deterministic lifecycle behavior instead of relying on prompt compliance.

## Why This Matters

Hooks could let us attach operational logic directly to Claude's lifecycle events and use that to improve:
- auditability
- approval flow
- context loading
- session persistence behavior
- "keep going" nudges when execution stops too early

This is Claude-specific infrastructure, not a cross-backend abstraction yet. Treat it as an integration idea to evaluate, not as a committed product path.

## Candidate Uses

### 1. `SessionStart`: load context automatically

Potential use:
- inject repo-specific context every time a Claude session starts
- load operator notes, active session summary, or gateway instructions without depending on the model to remember to fetch them

Why it is interesting:
- more deterministic than hoping the agent reads the right files on its own
- could reduce first-turn setup friction for resumed sessions

### 2. `PreToolUse`: log every bash command

Potential use:
- record every shell command Claude is about to run
- forward those events into our existing session/event logging pipeline

Why it is interesting:
- useful for compliance and audit trails
- gives us better visibility into what the backend is doing during a session
- could support future policy checks or operator review

### 3. `PermissionRequest`: route approvals to chat

Potential use:
- intercept permission prompts and forward them to Telegram or WhatsApp for approve/deny actions
- unblock stuck sessions when Claude is waiting on a human decision

Why it is interesting:
- maps well to the gateway's remote-control model
- could let the operator approve sensitive actions without needing direct terminal access
- especially useful for long-running remote sessions

Note:
- Telegram is the natural first target in this repo, even if WhatsApp is also an interesting pattern.

### 4. `Stop`: nudge Claude to continue

Potential use:
- detect when Claude stops before the task is actually complete
- trigger a follow-up nudge such as "continue until the task is finished" or route a reminder through the gateway

Why it is interesting:
- may help reduce premature stopping on multi-step tasks
- could improve unattended execution quality

Risk:
- this needs tight guardrails or it can create noisy loops, repeated work, or hidden autonomy drift

## Fit With This Repo

The strongest alignment with the current product is:
- `PermissionRequest` -> remote human approval
- `PreToolUse` -> compliance/audit logging
- `SessionStart` -> deterministic session context bootstrap

`Stop` is the most speculative. It may be useful, but it has the highest risk of turning into brittle auto-reprompt behavior.

## Recommendation

If we evaluate Claude hooks in this repo, the order should be:
1. `PreToolUse` logging
2. `PermissionRequest` chat-based approvals
3. `SessionStart` context bootstrap
4. `Stop` continuation nudges

That order matches the current product priorities: safety, inspectability, and remote operability before autonomy enhancements.
