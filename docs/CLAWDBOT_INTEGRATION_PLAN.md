# Clawdbot Integration Plan

## The Vision

**Vibe code from Telegram while waiting in line at the airport or riding the bus.**

Let the coding agent go loose, be maximum useful, but auditable automatically.

---

## What We Actually Built (Unique Value)

### The Philosophy Difference

| Pi-mono Extensions | AI-team Approach |
|-------------------|------------------|
| **Blacklist**: Block bad things | **Whitelist**: Allow only what's needed |
| `if (command.includes("rm -rf")) block()` | `if (!allowedTools.includes(tool)) block()` |
| Trust the output | Validate the output |
| Logs for debugging | Artifacts for compliance |
| One-size-fits-all rules | Task-type-aware policies |

### What Exists vs. What We Add

**Pi-mono example extensions have:**
- `permission-gate.ts` - Blocks `rm -rf`, `sudo`, `chmod 777`
- `protected-paths.ts` - Blocks writes to `.env`, `.git/`, `node_modules/`
- `tool-override.ts` - Path blocking + access logging
- `claude-rules.ts` - Loads `.claude/rules/` into prompt

**They DON'T have (our unique value):**

| Gap | What AI-team Provides |
|-----|----------------------|
| **Task-type-aware policies** | CODE_REVIEW = read-only, FIX = full access, DOCUMENTATION = write but no bash |
| **Semantic tool restriction** | Not "block dangerous" but "only allow what this task type needs" |
| **Output validation** | Similarity checks (did it do what was asked?), entropy checks (is output degenerate?) |
| **Compliance artifacts** | Structured JSON results (schema v1.0), NDJSON event stream, audit trail |
| **Agent-specific thresholds** | Bug-fix needs 0.8 similarity, exploratory analysis needs 0.6 |

### The Governance Layer

We didn't build "another coding agent" - we built a **governance layer** for coding agents.

```
User Intent → Task Classification → Policy → Execution → Validation → Artifacts
                    ↑                  ↑           ↑            ↑
              (semantic)         (whitelist)  (quality)    (compliance)
```

The market has:
- Lots of coding agents (Claude Code, Codex, Pi, Aider, Cursor...)
- Lots of gateways (Clawdbot, n8n, custom...)
- **Almost no governance layers for letting agents run loose safely**

### Why This Matters

**For personal use**: Pi-mono's blacklist extensions might be enough.

**For "let it loose" autonomous operation**:
- You NEED whitelist policies (task type → minimum required tools)
- You NEED output validation (catch hallucinations, drift)
- You WANT audit trail (what happened while you were on the bus?)

### What We Preserve

| Component | Why Keep It |
|-----------|-------------|
| `src/bridges/claude_bridge.py` | Policy → `--allowedTools` enforcement logic |
| `src/core/agent_manager.py` | Task type → permissions mapping (the semantic layer) |
| `src/validation/engine.py` | Similarity, entropy, structure checks (no one else has this) |
| Artifact schema + persistence | Compliance audit trail |
| `prompts/agents/*.md` | Agent-specific instructions + thresholds |

### What We Can Discard (Clawdbot/Pi-mono provide better)

| Component | Why Discard |
|-----------|-------------|
| `telegram/interface.py` | Clawdbot has 12 channels |
| `file_watcher.py` | Clawdbot triggers from messages |
| Basic orchestrator | Clawdbot gateway handles this |
| Session management | Pi-mono + Clawdbot process tool |

---

## Key Insight

**Clawdbot is omnipotent but unguarded. Our unique value = guardrails for code execution.**

Clawdbot can reach every tool (12 messaging channels, browser, devices, etc.) but has no:
- Task-type-aware permission policies
- Output validation (similarity, entropy checks)
- Compliance artifact trail
- Semantic contracts for capabilities

Every clawdbot capability could benefit from "contracts" (how to treat email, calendar, etc.), but **code execution is the most straightforward to guard** - clear inputs, clear outputs, clear permissions.

---

## Architecture Understanding

```
You (Telegram/WhatsApp/Discord/etc)
         │
         ▼
┌─────────────────────────┐
│       Clawdbot          │
│    (Gateway Agent)      │
│                         │
│  Has access to tools:   │
│  - execute_code_task ◄──┼─── Our guardrails wrapped as a tool
│  - browser_control      │
│  - calendar             │
│  - etc.                 │
└───────────┬─────────────┘
            │
            │ "fix the auth bug in src/login.py"
            │ Clawdbot decides: "this needs execute_code_task"
            ▼
┌─────────────────────────────────────────────┐
│         execute_code_task (our tool)        │
│                                             │
│  1. Parse intent → task_type = "FIX"        │
│  2. Policy: FIX → [Read, Edit, Bash]        │
│  3. Spawn Claude Code with restrictions:    │
│     claude --allowedTools Read,Edit,Bash    │
│  4. Validate output                         │
│  5. Save artifacts                          │
│  6. Return result                           │
└───────────┬─────────────────────────────────┘
            │
            ▼
┌─────────────────────────┐
│      Claude Code        │
│  (runs with HARD        │
│   tool constraints)     │
└─────────────────────────┘
```

---

## What's Unique in AI-team (KEEP)

| Component | Why It Matters |
|-----------|----------------|
| **Policy Engine** | Task type → permissions mapping. No agent has this semantically. |
| **Bridge/Adapter** | Translates policy → `--allowedTools` flags. THE enforcement mechanism. |
| **Validation Engine** | Similarity, entropy, structure checks. Quality gate above any agent. |
| **Artifact Persistence** | JSON results + NDJSON events = compliance audit trail. |

**The bridge is NOT redundant** - it's what makes policy actually enforced at runtime.

---

## What Clawdbot Provides (USE)

- 12 messaging channel integrations (Telegram, WhatsApp, Slack, Discord, Signal, etc.)
- Device nodes (macOS, iOS, Android)
- Browser automation
- Voice with ElevenLabs
- Gateway/session management
- Mature WebSocket control plane

---

## Next Steps

### Phase 1: Investigate Clawdbot
- [ ] Clone clawdbot repo
- [ ] Understand how tools are registered (look for tool definitions)
- [ ] Understand tool interface/contract (parameters, return format)
- [ ] Understand how tools access local system (spawning processes)

### Phase 2: Extract Portable Core from AI-team
- [ ] Extract `PolicyEngine` class (task_type → permissions mapping)
- [ ] Extract `ClaudeAdapter` (spawns Claude Code with `--allowedTools`)
- [ ] Extract `ValidationEngine` (similarity, entropy, structure checks)
- [ ] Extract `ArtifactManager` (JSON results, NDJSON events)
- [ ] Make it a standalone package/module

### Phase 3: Create Clawdbot Tool
- [ ] Register `execute_code_task` tool in clawdbot
- [ ] Wire up: policy → adapter → validation → artifacts
- [ ] Test via Telegram: "fix the bug in X" → guarded Claude Code execution

### Phase 4: Future - Contracts for Other Capabilities
- [ ] Email capability contract (clarifications, confirmation before send)
- [ ] Calendar capability contract
- [ ] Browser capability contract
- [ ] Generic "capability contract" framework?

---

## The Core Code to Port

### Policy Engine (from AI-team)
```python
def get_allowed_tools_for_task(task_type: TaskType) -> list[str]:
    """Task-type-aware tool-gating policy - THIS IS THE UNIQUE VALUE"""
    match task_type:
        case TaskType.CODE_REVIEW | TaskType.SUMMARIZE:
            return ["Read", "LS", "Grep", "Glob"]  # Read-only
        case TaskType.DOCUMENTATION:
            return ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob"]  # Write, no Bash
        case TaskType.FIX | TaskType.BUG_FIX | TaskType.ANALYZE:
            return ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"]  # Full
```

### Tool Definition (for clawdbot)
```typescript
export const executeCodeTask = {
  name: "execute_code_task",
  description: "Execute code tasks with guardrails (fix bugs, review code, etc.)",
  parameters: {
    task_type: { type: "string", enum: ["FIX", "CODE_REVIEW", "ANALYZE", "DOCUMENTATION"] },
    prompt: { type: "string" },
    target_files: { type: "array", items: { type: "string" } }
  },

  async execute({ task_type, prompt, target_files }) {
    // 1. Policy
    const allowedTools = getPolicyForTaskType(task_type);

    // 2. Spawn Claude Code with HARD constraints
    const result = await spawnClaudeCode({
      prompt, allowedTools, outputFormat: "json"
    });

    // 3. Validate
    const validation = validateOutput(task_type, result);

    // 4. Artifacts
    await saveArtifacts(task_type, result, validation);

    return { success: validation.passed, summary: result.summary };
  }
};
```

---

## Key Quote

> "Put the guardrail around it and let it loose"

This is the philosophy: clawdbot provides reach (channels, devices), we provide safety (policy, validation, audit). Together = omnipotent but guarded.

---

---

## Session Continuity (CRITICAL GAP)

Current implementation is fire-and-forget. Each message spawns new Claude Code session with no context.

### What's Needed

1. **Capture session_id** from Claude Code JSON output
2. **Store mapping**: `clawdbot_conversation_id → claude_session_id`
3. **Detect questions**: When Claude Code needs clarification
4. **Route back**: Send question to user via clawdbot
5. **Continue session**: Use `--resume <session_id>` with user's answer

### Claude Code Flags

| Flag | Purpose |
|------|---------|
| `--resume <session_id>` | Resume specific session |
| `--continue` | Resume most recent session |
| `--output-format json` | JSON includes `session_id` |

### Flow

```
User: "fix auth bug"
  → execute_code_task() → Claude Code (session ABC)
  → Claude asks: "JWT or sessions?"
  → Return to user via clawdbot
User: "JWT"
  → continue_task(session="ABC", answer="JWT")
  → Claude Code resumes with full context
```

### Implementation Changes

```python
# 1. Add to TaskResult
@dataclass
class TaskResult:
    # ... existing ...
    session_id: Optional[str] = None
    status: str = "complete"  # or "needs_input"
    question: Optional[str] = None

# 2. Session store
session_store: Dict[str, str] = {}  # conversation_id → session_id

# 3. Capture session from output
if isinstance(parsed_output, dict):
    session_id = parsed_output.get('session_id')

# 4. Continue method
async def continue_task(self, session_id: str, user_answer: str):
    command = ["claude", "--resume", session_id, "-p", user_answer]
```

### Two Solutions for Two Problems

| Problem | Solution | What It Does |
|---------|----------|--------------|
| **Mid-task clarification** | `--resume <session_id>` | Continues the EXACT conversation |
| **Long-term project memory** | claude-mem / MCP | Recalls WHAT happened in past sessions |

**Session IDs** = Active conversation state (pending edits, open files, mid-task context)
**claude-mem** = Passive memory recall (project patterns, past decisions, conventions)

### claude-mem (MCP Memory Plugin)

Repository: https://github.com/thedotmack/claude-mem

**What it does:**
- Captures tool usage during sessions via lifecycle hooks
- Compresses observations with AI
- Stores in SQLite + Chroma vector DB
- Injects relevant context at session start
- Provides 4 MCP tools for memory search

**Architecture:**
- 5 Lifecycle Hooks: SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd
- Worker Service: HTTP API on port 37777 with web UI
- Chroma Vector Database: Hybrid semantic + keyword search
- 3-layer workflow: search → timeline → get_observations (~10x token savings)

**Installation:**
```bash
/plugin marketplace add thedotmack/claude-mem
/plugin install claude-mem
```

**Why it matters for us:**
```
Session 1 (last week): "Fixed auth bug using JWT"
Session 2 (today): "Add refresh tokens to auth"

With claude-mem:
→ New session automatically knows "this project uses JWT for auth"
→ Doesn't need to rediscover the architecture
→ Remembers patterns, decisions, conventions
```

### Why We Need BOTH

```
User: "fix the auth bug"
  → Claude Code starts session ABC123
  → Claude asks: "JWT or session-based?"

  ❌ claude-mem alone: Would start NEW session, lose pending state
  ✅ --resume ABC123: Continues EXACT conversation with pending edits

User (next week): "add refresh token support"
  → NEW session starts

  ❌ Session IDs alone: No memory of past work
  ✅ claude-mem: Injects "this project uses JWT, auth is in src/auth/"
```

| Feature | Session IDs | claude-mem |
|---------|-------------|------------|
| Mid-task Q&A | ✅ Required | ❌ Can't do |
| "What did I do last week?" | ❌ Sessions expire | ✅ Yes |
| Project conventions | ❌ Not its job | ✅ Yes |
| Pending edits state | ✅ Preserved | ❌ Lost |
| Complexity | Low (flags) | Medium (MCP + DB) |

### Implementation Phases

**Phase 1: Session ID Management** (solves immediate problem)
- Capture `session_id` from Claude Code JSON output
- Store mapping: `clawdbot_conversation_id → claude_session_id`
- Use `--resume` for mid-task continuations
- Detect questions → route back to user → continue session

**Phase 2: claude-mem Integration** (long-term memory)
- Install claude-mem plugin
- Configure for project-level memory
- Benefits: Claude Code remembers project context across days/weeks
- Complements session IDs, doesn't replace them

---

---

## Pi-Mono Agent & Clawdbot Relationship (CRITICAL)

### The Stack

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLAWDBOT                                 │
│  Gateway + Multi-channel (Telegram, WhatsApp, Slack, etc.)      │
│  Session management, discovery, tool wiring                     │
└──────────────────────────┬──────────────────────────────────────┘
                           │ Uses pieces of
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                        PI-MONO                                  │
│  @mariozechner/pi-agent-core  - Agent loop, tool execution      │
│  @mariozechner/pi-ai          - Unified LLM API                 │
│  @mariozechner/pi-coding-agent - CLI coding agent               │
└─────────────────────────────────────────────────────────────────┘
                           │ Can spawn
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│              CODING AGENTS (Claude Code, Codex, etc.)           │
└─────────────────────────────────────────────────────────────────┘
```

**Key insight**: Clawdbot reuses pi-mono's models/tools but owns session management. There is NO pi-coding-agent runtime in clawdbot - it's embedded and modified.

### Pi-Mono's Philosophy on Security

From their docs:
> "No permission popups" - "Security theater."
> Run in a container or build your own security with **Extensions**.

**They explicitly expect users to add their own guardrails via Extensions!**

### Pi Extensions = OUR INTEGRATION POINT

Extensions are TypeScript modules that can:
1. **Intercept tool calls BEFORE execution**
2. **Block dangerous operations**
3. **Require user confirmation**
4. **Enforce custom policies**

```typescript
// Example: Policy enforcement extension
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

export default function guardedCodingAgent(pi: ExtensionAPI) {

  // Intercept ALL tool calls
  pi.on("tool_call", async (event, ctx) => {
    const taskType = ctx.state.get("task_type") || "UNKNOWN";
    const allowedTools = getPolicyForTaskType(taskType);

    // BLOCK if tool not allowed for this task type
    if (!allowedTools.includes(event.toolName)) {
      return {
        block: true,
        reason: `Tool '${event.toolName}' not allowed for task type '${taskType}'`
      };
    }

    // Additional checks for dangerous operations
    if (event.toolName === "bash" && isDangerous(event.input.command)) {
      const ok = await ctx.ui.confirm("Dangerous command", "Allow?");
      if (!ok) return { block: true, reason: "Blocked by user" };
    }
  });

  // Intercept results for validation
  pi.on("tool_result", async (event, ctx) => {
    // Validate output quality (similarity, entropy)
    const validation = validateOutput(event.result);
    if (!validation.passed) {
      ctx.ui.notify(`Validation failed: ${validation.issues}`, "warning");
    }
    // Save artifact
    saveArtifact(ctx.state.get("task_id"), event);
  });
}

function getPolicyForTaskType(taskType: string): string[] {
  const policies: Record<string, string[]> = {
    "CODE_REVIEW": ["read", "grep", "glob", "ls"],
    "FIX": ["read", "write", "edit", "bash", "grep", "glob", "ls"],
    "DOCUMENTATION": ["read", "write", "edit", "grep", "glob", "ls"],
  };
  return policies[taskType] || ["read", "grep", "glob", "ls"];
}
```

### Built-in Security Extensions in Pi-Mono

Pi-mono already has example extensions:
- **filter-output**: Redact sensitive data (API keys, tokens) from tool results
- **security**: Block dangerous bash commands, protect sensitive paths

**We can BUILD ON these patterns!**

### What This Means for Our Integration

**Option A: Clawdbot Skill (Markdown-based)**
- Skill instructions + Python scripts
- Less integrated, more portable
- Works with any clawdbot setup

**Option B: Pi Extension (TypeScript)**
- Deep integration with tool execution
- Can intercept BEFORE tool runs
- Proper policy enforcement
- Requires TypeScript, tighter coupling

**Option C: BOTH**
- Skill for high-level workflow (task classification, artifact management)
- Extension for low-level guardrails (tool blocking, validation)

### Recommended: Option C (Hybrid)

```
┌─────────────────────────────────────────────────────────────────┐
│  guarded-coding-agent SKILL                                     │
│  - Task classification (FIX, CODE_REVIEW, etc.)                 │
│  - Artifact persistence (JSON results, NDJSON events)           │
│  - High-level workflow instructions                             │
└──────────────────────────┬──────────────────────────────────────┘
                           │ Triggers
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  guardrails EXTENSION                                           │
│  - Tool call interception                                       │
│  - Policy enforcement (task type → allowed tools)               │
│  - Output validation (similarity, entropy)                      │
│  - Dangerous command blocking                                   │
└─────────────────────────────────────────────────────────────────┘
```

### Boundary Definition (FINAL)

| Layer | Who Provides | What It Does |
|-------|--------------|--------------|
| **Multi-channel access** | Clawdbot | Telegram, WhatsApp, Slack, etc. |
| **Agent runtime** | Pi-mono (via clawdbot) | Tool execution, LLM calls |
| **Session management** | Clawdbot | Background processes, stdin/stdout |
| **Tool interception** | **US (Extension)** | Policy enforcement, blocking |
| **Validation** | **US (Extension)** | Similarity, entropy checks |
| **Task classification** | **US (Skill)** | FIX, CODE_REVIEW, etc. |
| **Artifacts** | **US (Skill + Extension)** | JSON results, audit trail |
| **Claude Code spawning** | Clawdbot's coding-agent skill | `bash pty:true` |

### Sources

- [Pi-mono GitHub](https://github.com/badlogic/pi-mono)
- [Pi Extensions Docs](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md)
- [Clawdbot Agent Concepts](https://docs.clawd.bot/concepts/agent)
- [awesome-pi-agent](https://github.com/qualisero/awesome-pi-agent)

---

## Clawdbot Skill System (RESEARCH COMPLETE)

### How Clawdbot Skills Work

Skills are **markdown-driven instruction packages** - NOT code plugins. They guide the AI agent's behavior.

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/     - Executable code (Python/Bash)
    ├── references/  - Documentation loaded as needed
    └── assets/      - Templates, files for output
```

### SKILL.md Structure

```yaml
---
name: guarded-coding-agent
description: |
  Execute code tasks with policy-based guardrails. Use when user requests
  code changes, bug fixes, code reviews, or documentation. Enforces
  task-type-aware tool restrictions and validates output quality.
  Triggers: "fix bug", "review code", "add feature", "document this"
metadata:
  clawdbot:
    requires:
      bins: [claude, python3]
    primaryEnv: ANTHROPIC_API_KEY
---

[Markdown instructions for the agent...]
```

### Key Insight: coding-agent Skill Already Exists!

Clawdbot has a built-in `coding-agent` skill that spawns Claude Code:

```bash
# How it works - uses bash with pty
bash pty:true workdir:~/project command:"claude 'fix the bug'"

# Background mode with session management
bash pty:true background:true command:"claude 'fix the bug'"
process action:log sessionId:XXX
process action:write sessionId:XXX input:"user clarification"
```

**BUT it has NO guardrails** - spawns Claude Code with full permissions.

### Our Integration Approach

**Option A: Create `guarded-coding-agent` skill** that wraps our policy logic:

```
guarded-coding-agent/
├── SKILL.md
│   └── Instructions to:
│       1. Determine task type from user request
│       2. Run policy script to get allowed tools
│       3. Spawn Claude Code with --allowedTools restriction
│       4. Validate output with validation script
│       5. Save artifacts
├── scripts/
│   ├── policy_engine.py      # Task type → allowed tools
│   ├── spawn_claude.py       # Wrapper with guardrails
│   └── validate_output.py    # Similarity, entropy checks
└── references/
    └── task_types.md         # Documentation of policies
```

**Option B: Fork/extend `coding-agent`** with guardrails added.

### Skill Frontmatter for Gating

```yaml
metadata:
  clawdbot:
    requires:
      bins: [claude, python3]        # Required binaries
      env: [ANTHROPIC_API_KEY]       # Required env vars
    os: [darwin, linux, win32]       # Platform filter
    primaryEnv: ANTHROPIC_API_KEY    # Auto-inject API key
```

### Skill Locations & Precedence

1. `<workspace>/skills` - Highest priority (our custom skills)
2. `~/.clawdbot/skills` - User-level
3. Bundled skills - Lowest priority
4. Extra dirs via `skills.load.extraDirs` config

### Process Management (for session continuity)

Clawdbot's `process` tool handles background sessions:

| Action | Purpose |
|--------|---------|
| `process action:list` | List running sessions |
| `process action:poll sessionId:X` | Check if active |
| `process action:log sessionId:X` | Get output |
| `process action:write sessionId:X input:"text"` | Send input (for clarifications!) |
| `process action:kill sessionId:X` | Terminate |

**This solves session continuity!** We can:
1. Spawn Claude Code in background: `bash pty:true background:true`
2. Monitor with `process action:poll`
3. Send clarifications with `process action:write`
4. Get results with `process action:log`

### Implementation Plan

**Phase 1: Create `guarded-coding-agent` skill**

```python
# scripts/spawn_claude.py
import subprocess
import json

def get_policy(task_type: str) -> list:
    """Task type → allowed tools"""
    policies = {
        "CODE_REVIEW": ["Read", "Grep", "Glob", "LS"],
        "FIX": ["Read", "Edit", "Bash", "Grep", "Glob", "LS"],
        "DOCUMENTATION": ["Read", "Edit", "Write", "Grep", "Glob", "LS"],
    }
    return policies.get(task_type, ["Read", "Grep", "Glob", "LS"])

def spawn_claude(task_type: str, prompt: str, workdir: str):
    allowed_tools = get_policy(task_type)
    cmd = [
        "claude",
        "--allowedTools", ",".join(allowed_tools),
        "--output-format", "json",
        "-p", prompt
    ]
    # Execute and return result with session_id
```

**Phase 2: SKILL.md instructions**

```markdown
## Workflow

1. **Classify task type** from user request:
   - "fix", "bug" → FIX
   - "review", "check" → CODE_REVIEW
   - "document", "add docs" → DOCUMENTATION
   - "analyze", "investigate" → ANALYZE

2. **Get policy** by running: `python scripts/spawn_claude.py --task-type <type>`

3. **Execute** with restrictions shown in output

4. **If Claude asks questions**: Use `process action:write` to send user's answer

5. **Validate output** by running: `python scripts/validate_output.py <output>`

6. **Save artifacts** to results/ directory
```

### What We DON'T Need to Build

| Component | Why Not Needed |
|-----------|----------------|
| Session management | Clawdbot's `process` tool handles it |
| Multi-channel support | Clawdbot provides (Telegram, WhatsApp, etc.) |
| File watcher | Clawdbot triggers skills from messages |
| Queue management | Clawdbot handles concurrency |

### What We DO Need to Port

| Component | How to Port |
|-----------|-------------|
| Policy engine | `scripts/policy_engine.py` |
| Validation engine | `scripts/validate_output.py` |
| Artifact persistence | `scripts/save_artifacts.py` |
| Agent instructions | `references/agent_instructions.md` |

---

## Resources

### External Projects
- Clawdbot repo: https://github.com/clawdbot/clawdbot
- claude-mem: https://github.com/thedotmack/claude-mem (persistent memory plugin)
- mcp-memory-service: https://github.com/doobidoo/mcp-memory-service (alternative MCP memory)
- Claude Code Memory Docs: https://code.claude.com/docs/en/memory
- OpenMemory MCP: https://mem0.ai/blog/introducing-openmemory-mcp

### AI-team Unique Components (to port)
- `src/bridges/claude_bridge.py` - enforcement adapter (policy → --allowedTools)
- `src/core/agent_manager.py` - policy definitions (task type → permissions)
- `src/validation/engine.py` - output validation (similarity, entropy, structure)
- `config/settings.py` - configuration
- `prompts/agents/*.md` - agent instructions with implicit policies

### Claude Code Session Flags
| Flag | Purpose |
|------|---------|
| `--resume <session_id>` | Resume specific session |
| `--continue` or `-c` | Resume most recent session |
| `--resume` (no arg) | Interactive session picker |
| `--output-format json` | JSON output includes `session_id` |

### CLAUDE.md Memory Hierarchy (built-in)
| Level | Location | Purpose |
|-------|----------|---------|
| Managed policy | `/Library/Application Support/ClaudeCode/CLAUDE.md` | Org-wide |
| Project memory | `./CLAUDE.md` or `./.claude/CLAUDE.md` | Team-shared |
| Project rules | `./.claude/rules/*.md` | Modular rules |
| User memory | `~/.claude/CLAUDE.md` | Personal global |
| Local project | `./CLAUDE.local.md` | Personal per-project |
