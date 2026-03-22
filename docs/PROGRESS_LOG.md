# Progress Log

## 2026-03-22

### Completed

- Re-centered the repo around the actual product: a Telegram session-first coding gateway
- Added shared path validation and path suggestions for session creation
- Added Telegram commands for session directory listing, session cancellation, `/run`, and `/say`
- Tightened session ownership checks and session state transitions
- Removed prompt rewriting from the active execution path so Claude Code / Codex stay in control of their own runtime
- Stopped surfacing the old local agent-layer as if it were active product behavior
- Added focused tests for path resolution and Telegram session flow
- Removed several stale tests and docs that described the older agent-template/orchestrator product

### Current Gate

- Run a live end-to-end Telegram session resume test against Claude Code

### Notes

- LLAMA mediator is still present, but now explicitly treated as a dormant future layer rather than the active product path
- The docs set was reduced to a small canonical publish-facing surface
