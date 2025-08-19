---
id: read_only_smoke
type: summarize
priority: low
created: 2025-08-18T00:00:00Z
---

# Read-only Smoke Test

**Target Files:**
- orchestrator/src/orchestrator.py
- orchestrator/src/bridges/claude_bridge.py

**Prompt:**
Summarize the purpose of the orchestrator and the Claude bridge without making any code changes. Only read files; do not write or execute commands.

**Success Criteria:**
- [ ] The task completes without modifying any files
- [ ] Only read-related tools are used
- [ ] A brief summary is produced

**Context:**
This task is used as a non-destructive health check to verify read-only permissions.


