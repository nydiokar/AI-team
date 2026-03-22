# Roadmap

This roadmap is for the current product: a session-first Telegram gateway for local coding agents.

## Current State

Implemented:
- session creation and routing from Telegram
- native resume through Claude Code and Codex backends
- file-backed session state and summaries
- per-session event logs
- path validation and path suggestions
- one-off task fallback
- basic git helper commands

## Before Calling It Production

### 1. Live session-resume validation

Required:
- create a real Telegram session
- verify backend session id capture
- verify second turn resumes the same backend session

### 2. Workspace boundary decision

Required:
- set `CLAUDE_BASE_CWD`
- set `CLAUDE_ALLOWED_ROOT`
- confirm the chosen root includes every repo the gateway should be able to edit

### 3. Test-suite reconciliation

Required:
- continue removing stale legacy tests
- keep focused tests around session routing, path validation, and backend behavior
- make sure the remaining suite reflects the session-first product rather than the historical orchestrator/agent-template design

### 4. Publish cleanup

Required:
- keep docs small and canonical
- archive or remove historical docs that describe the wrong product
- leave dormant LLAMA/local-agent code clearly marked as future-facing rather than active

## Later

Possible future work, only if it still fits the product:
- stronger real-repo E2E coverage
- richer session inspection
- safer git/approval flows
- optional local operational layer using LLAMA or local agents

The key constraint remains the same:
- backend-native runtime stays primary
- the gateway should not drift into a broad autonomous orchestration framework
