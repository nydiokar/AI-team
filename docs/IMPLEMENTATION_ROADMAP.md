# Implementation Roadmap: AI-Team + Time-Lab Integration

**Created**: 2025-10-17
**Status**: Active Planning
**Purpose**: Practical, step-by-step integration plan that respects existing AI-team architecture

---

## 🎯 Context: What We're Actually Integrating

### AI-Team Current State (Windows-Proven)

**What it does now:**
- Watches `AI-team/tasks/` for `.task.md` files
- Executes via Claude Code CLI (headless)
- Uses LLAMA for parsing/summarization (optional, graceful fallback)
- Writes artifacts to:
  - `AI-team/results/{task_id}.json` (full result)
  - `AI-team/summaries/{task_id}_summary.txt` (human-readable)
  - `AI-team/logs/events.ndjson` (execution events)
  - `AI-team/tasks/processed/{task_id}.completed.task.md` (archived task)

**Key configuration:**
```python
# config/settings.py:98 (HARDCODED - PROBLEM!)
self.claude.base_cwd = r"C:\Users\Cicada38\Projects"

# Can be overridden via .env (lines 192-201):
CLAUDE_BASE_CWD=/home/gorila/projects
CLAUDE_ALLOWED_ROOT=/home/gorila/projects
```

**File watching:**
```python
# orchestrator.py:47
self.file_watcher = AsyncFileWatcher(config.system.tasks_dir)
# tasks_dir = "tasks" (relative to AI-team/)
```

**Artifact writing:**
```python
# orchestrator.py:745-864
def _write_artifacts(self, task_id: str, result: TaskResult):
    results_dir = Path(config.system.results_dir)  # "results"
    summaries_dir = Path(config.system.summaries_dir)  # "summaries"
    # Writes to AI-team/results/, AI-team/summaries/
```

---

## ⚠️ Key Realizations

### 1. **AI-Team Already Has an Index**
```python
# orchestrator.py:288-302
self._artifact_index_path = Path(config.system.results_dir) / "index.json"
# AI-team/results/index.json maps task_id -> artifact path
```

**Implication**: We don't need to duplicate this. Time-lab can use it or receive a simpler format.

### 2. **.ai-team Folder Per-Project is Optional**
Current behavior:
- AI-team watches `AI-team/tasks/` (single directory)
- Task files have `cwd:` frontmatter field (lines 1049, 1138)
- Claude executes in that cwd context

**Two approaches:**
- **A. Per-project .ai-team/** - Each project has `.ai-team/tasks/`
- **B. Centralized AI-team/tasks/** - All tasks in one place, use `cwd:` field

**Your question:** Which is better?

**Answer**: **Start with B (centralized), evolve to A if needed.**

**Why:**
- AI-team is already set up for centralized watching
- `cwd:` field already works (line 1049: frontmatter includes `cwd`)
- Less initial churn in projects
- Can add per-project watching later (modify FileWatcher to watch multiple dirs)

### 3. **Artifact Duplication is Intentional**
AI-team artifacts are **operational** (debugging, monitoring):
- Full stdout/stderr (can be MB of logs)
- Raw execution details
- Retry attempts
- Component status

Time-lab artifacts should be **archival** (insights, learning):
- Curated summary
- Key metadata
- Cross-project queryable

**Not redundant - different purposes, different retention.**

### 4. **Nix Integration Level**
AI-team uses Python + system deps (Ollama, Claude CLI).

**What needs Nix:**
- ✅ Python 3.11 + packages (watchdog, pydantic, etc.)
- ✅ Ollama (optional)
- ✅ Claude CLI (must be in PATH)
- ❌ Don't nixify task files (they're just markdown)
- ❌ Don't nixify artifacts (they're data)

**Nix scope**: Development environment only, not the tool itself.

---

## 🛠️ Integration Strategy: Minimal Changes to AI-Team

### Core Principle
**Don't twist AI-team to fit time-lab. Extend it cleanly.**

### What Changes in AI-Team
1. **Remove Windows hardcoded path** (line 98)
2. **Add time-lab artifact writer** (new module)
3. **Make watch directory configurable** (optional enhancement)

### What Changes in Time-Lab
1. **Accept artifacts via simple protocol**
2. **Provide schemas for validation**
3. **Build query tools**

### What Doesn't Change
- AI-team's core workflow (watch → parse → execute → validate)
- AI-team's existing artifact structure
- AI-team's Claude/LLAMA integration
- Project structure (no mandatory `.ai-team/` folders yet)

---

## 📋 Phase-by-Phase Implementation

### Phase 0: Fix AI-Team for Linux (Week 1)

**Goal**: Make AI-team work on Linux without any time-lab integration

**Tasks:**

1. **Remove hardcoded Windows path**
   ```python
   # config/settings.py:98
   # BEFORE:
   self.claude.base_cwd = r"C:\Users\Cicada38\Projects"

   # AFTER:
   # Remove this line entirely - it's overridden by _apply_env_overrides() anyway
   # Lines 192-201 already handle env vars correctly
   ```

2. **Create AI-team/.env for Linux**
   ```bash
   cd ~/AI-team
   cp .env.example .env

   # Edit .env:
   CLAUDE_BASE_CWD=/home/gorila/projects
   CLAUDE_ALLOWED_ROOT=/home/gorila/projects
   CLAUDE_SKIP_PERMISSIONS=false
   CLAUDE_MAX_TURNS=0

   # Optional (Ollama):
   LLAMA_HOST=localhost
   LLAMA_PORT=11434
   LLAMA_MODEL=llama3.2:latest

   # System:
   SYSTEM_LOG_LEVEL=INFO
   SYSTEM_MAX_CONCURRENT_TASKS=3
   SYSTEM_TASK_TIMEOUT=1800
   ```

3. **Test AI-team on Linux**
   ```bash
   cd ~/AI-team

   # Install dependencies
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[llama,dev]"  # With Ollama support

   # Or just core:
   pip install -e .

   # Test configuration
   python -c "from config import config; print(config.claude.base_cwd)"
   # Should print: /home/gorila/projects

   # Create test task
   mkdir -p tasks
   cat > tasks/test_linux.task.md << 'EOF'
   ---
   id: test_linux_001
   type: analyze
   priority: medium
   created: 2025-10-17T15:00:00Z
   cwd: /home/gorila/projects/test-project
   ---

   # Test Linux Integration

   **Target Files:**
   - README.md

   **Prompt:**
   List the files in this directory and create a simple README if it doesn't exist.

   **Success Criteria:**
   - [ ] Directory listing works
   - [ ] README.md exists

   **Context:**
   Testing AI-team on Linux before time-lab integration.
   EOF

   # Create test project
   mkdir -p /home/gorila/projects/test-project

   # Run orchestrator
   python src/orchestrator.py
   # Wait for it to process the task
   # Check results/test_linux_001.json
   ```

4. **Verify outputs**
   ```bash
   # Check artifact was created
   ls -la results/test_linux_001.json
   cat summaries/test_linux_001_summary.txt

   # Check event log
   tail logs/events.ndjson

   # Check processed task
   ls -la tasks/processed/test_linux_001.completed.task.md
   ```

**Success Criteria:**
- ✅ AI-team starts without errors
- ✅ Watches `tasks/` directory
- ✅ Processes test task
- ✅ Executes Claude in correct cwd
- ✅ Creates artifacts in `results/`, `summaries/`, `logs/`
- ✅ No Windows path errors

**Deliverable**: Working AI-team on Linux

---

### Phase 1: Design Time-Lab Protocol (Week 1-2)

**Goal**: Define how AI-team talks to time-lab (data contract)

**Key Decisions:**

#### Decision 1: Where do projects go?

**Option A: Per-project .ai-team folders**
```
~/projects/
├── auth-module/
│   ├── .ai-team/
│   │   └── tasks/        # Drop tasks here
│   └── src/
```

**Option B: Centralized tasks with cwd field**
```
~/AI-team/tasks/
├── auth_fix_login.task.md   # cwd: /home/gorila/projects/auth-module
└── crypto_gas_fees.task.md  # cwd: /home/gorila/projects/crypto-analyzer
```

**Recommendation: Start with B, add A later if needed**

**Why:**
- AI-team already works this way
- Less changes to make it work
- Can watch multiple dirs later (FileWatcher enhancement)
- Projects stay clean (no AI config pollution)

**Trade-off:**
- Tasks aren't co-located with project
- But: artifacts are in time-lab anyway, not in project

#### Decision 2: What does time-lab receive?

**NOT full AI-team artifacts** (too large, operational details)

**Instead: Curated summary**

```json
{
  "schema_version": "time-lab-v1",
  "task_id": "test_linux_001",
  "timestamp": "2025-10-17T15:23:45Z",
  "project": "test-project",  // Extracted from cwd or explicit field
  "task_type": "analyze",
  "summary": "Listed directory files and created README",
  "duration_seconds": 12.3,
  "success": true,
  "files_changed": ["README.md"],
  "git_context": {
    "sha_before": "abc123",
    "sha_after": "def456",
    "branch": "main"
  },
  "ai_team_artifact": "~/AI-team/results/test_linux_001.json"  // Link back
}
```

**This is MUCH smaller than AI-team's full artifact.**

#### Decision 3: How does time-lab receive it?

**Three options:**

**A. File-system write (simplest)**
```python
# AI-team writes directly to time-lab
time_lab_path = Path(os.getenv("TIME_LAB_PATH", "~/time-lab"))
date = datetime.now().strftime("%Y/%m/%d")
project = extract_project_from_cwd(task.metadata.get("cwd"))
artifact_dir = time_lab_path / date / project / task_id
artifact_dir.mkdir(parents=True, exist_ok=True)
(artifact_dir / "artifact.json").write_text(json.dumps(curated_artifact))
```

**B. Via time-lab script (cleaner)**
```bash
# AI-team calls time-lab's record script
just -f ~/time-lab/Justfile record \
  --artifact ~/AI-team/results/test_linux_001.json \
  --project test-project \
  --date 2025/10/17
```

**C. Queue-based (complex, overkill)**
```python
# AI-team writes to queue, time-lab reads
# Unnecessary for file-based system
```

**Recommendation: A (filesystem write) for now, B (script) later**

**Why:**
- Simplest to implement
- No subprocess overhead
- Debuggable (just JSON files)
- Can refactor to B without changing semantics

**Trade-off:**
- Tight coupling (AI-team knows time-lab path)
- But: acceptable for personal tool

#### Decision 4: How to extract project name?

**From task's `cwd` field:**
```python
# If cwd = "/home/gorila/projects/auth-module"
# Extract: "auth-module"

def extract_project_name(cwd: Optional[str]) -> str:
    if not cwd:
        return "unknown"
    path = Path(cwd)
    # Assume projects are directly under ~/projects
    projects_base = Path.home() / "projects"
    try:
        relative = path.relative_to(projects_base)
        return str(relative.parts[0]) if relative.parts else "unknown"
    except ValueError:
        # Not under projects/, use last dir name
        return path.name
```

**Handles:**
- `/home/gorila/projects/auth-module` → `auth-module`
- `/home/gorila/projects/crypto/analyzer` → `crypto`
- `/some/other/path/project` → `project`

---

### Phase 2: Implement Time-Lab Adapter in AI-Team (Week 2)

**Goal**: AI-team writes curated artifacts to time-lab

**New File: `AI-team/src/adapters/time_lab_writer.py`**

```python
"""
Adapter for writing curated artifacts to time-lab.

Responsibilities:
- Extract project name from task cwd
- Create curated artifact (subset of full result)
- Write to time-lab date/project/task structure
- Handle errors gracefully (don't break orchestrator)
"""
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
import os

from src.core import Task, TaskResult

logger = logging.getLogger(__name__)


class TimeLabWriter:
    """Writes curated artifacts to time-lab storage."""

    def __init__(self, time_lab_path: Optional[str] = None):
        """
        Args:
            time_lab_path: Path to time-lab root. Defaults to TIME_LAB_PATH env var or ~/time-lab
        """
        if time_lab_path:
            self.base_path = Path(time_lab_path)
        else:
            env_path = os.getenv("TIME_LAB_PATH")
            if env_path:
                self.base_path = Path(env_path)
            else:
                self.base_path = Path.home() / "time-lab"

        self.enabled = os.getenv("TIME_LAB_ENABLED", "true").lower() == "true"
        logger.info(f"TimeLabWriter initialized: enabled={self.enabled}, path={self.base_path}")

    def write_artifact(
        self,
        task: Task,
        result: TaskResult,
        ai_team_artifact_path: str
    ) -> bool:
        """
        Write curated artifact to time-lab.

        Returns True if successful, False otherwise.
        Does not raise exceptions (graceful degradation).
        """
        if not self.enabled:
            logger.debug("TimeLabWriter disabled, skipping")
            return False

        try:
            # Extract project name from task cwd
            project = self._extract_project_name(task)

            # Get date (YYYY/MM/DD)
            date = datetime.now().strftime("%Y/%m/%d")

            # Create artifact directory: time-lab/YYYY/MM/DD/project/task_id/
            artifact_dir = self.base_path / date / project / task.id
            artifact_dir.mkdir(parents=True, exist_ok=True)

            # Create curated artifact
            curated = self._create_curated_artifact(task, result, ai_team_artifact_path)

            # Write artifact.json
            artifact_file = artifact_dir / "artifact.json"
            artifact_file.write_text(json.dumps(curated, indent=2, ensure_ascii=False), encoding="utf-8")

            # Write summary.txt (human-readable)
            summary_file = artifact_dir / "summary.txt"
            summary_file.write_text(curated["summary"], encoding="utf-8")

            logger.info(f"Wrote time-lab artifact: {artifact_dir}")
            return True

        except Exception as e:
            logger.warning(f"Failed to write time-lab artifact for {task.id}: {e}")
            return False

    def _extract_project_name(self, task: Task) -> str:
        """Extract project name from task cwd field."""
        cwd = None
        if hasattr(task, "metadata") and task.metadata:
            cwd = task.metadata.get("cwd")

        if not cwd:
            return "unknown"

        path = Path(cwd)

        # Try to extract relative to ~/projects
        projects_base = Path.home() / "projects"
        try:
            relative = path.relative_to(projects_base)
            return str(relative.parts[0]) if relative.parts else "unknown"
        except ValueError:
            # Not under projects/, use last directory name
            return path.name if path.name else "unknown"

    def _create_curated_artifact(
        self,
        task: Task,
        result: TaskResult,
        ai_team_artifact_path: str
    ) -> Dict[str, Any]:
        """Create curated artifact (subset of full AI-team result)."""

        # Extract summary (LLAMA-generated from result.output)
        summary = result.output.split("\n\n", 1)[0] if result.output else "No summary available"
        if len(summary) > 500:
            summary = summary[:497] + "..."

        # Extract git context if available
        git_context = {}
        if hasattr(result, "parsed_output") and isinstance(result.parsed_output, dict):
            git_data = result.parsed_output.get("git", {})
            if git_data:
                git_context = {
                    "sha_before": git_data.get("sha_before"),
                    "sha_after": git_data.get("sha_after"),
                    "branch": git_data.get("branch")
                }

        return {
            "schema_version": "time-lab-v1",
            "task_id": task.id,
            "timestamp": result.timestamp,
            "project": self._extract_project_name(task),
            "task_type": task.type.value if hasattr(task.type, "value") else str(task.type),
            "priority": task.priority.value if hasattr(task.priority, "value") else str(task.priority),
            "summary": summary,
            "duration_seconds": result.execution_time,
            "success": result.success,
            "files_changed": result.files_modified,
            "git_context": git_context,
            "ai_team_artifact": ai_team_artifact_path,  # Link back to full artifact
            "metadata": {
                "cwd": task.metadata.get("cwd") if hasattr(task, "metadata") else None,
                "error_class": getattr(result, "error_class", None),
                "retries": getattr(result, "retries", 0)
            }
        }
```

**Integration Point: `orchestrator.py:486` (after `_write_artifacts`)**

```python
# In orchestrator.py, add after line 491 (after artifacts_written event):

# Write to time-lab if enabled
if hasattr(self, "time_lab_writer"):
    try:
        artifact_path = str(Path(config.system.results_dir) / f"{task.id}.json")
        self.time_lab_writer.write_artifact(task, result, artifact_path)
    except Exception as e:
        logger.warning(f"Failed to write time-lab artifact: {e}")

# Also initialize in __init__ (after line 50):
# Initialize TimeLabWriter if enabled
try:
    from src.adapters.time_lab_writer import TimeLabWriter
    self.time_lab_writer = TimeLabWriter()
except Exception as e:
    logger.warning(f"TimeLabWriter not available: {e}")
    self.time_lab_writer = None
```

**New Environment Variables:**
```bash
# Add to AI-team/.env:
TIME_LAB_ENABLED=true
TIME_LAB_PATH=/home/gorila/time-lab
```

**Testing:**
```bash
cd ~/AI-team

# Create test task
cat > tasks/test_timelab.task.md << 'EOF'
---
id: test_timelab_001
type: fix
priority: medium
created: 2025-10-17T16:00:00Z
cwd: /home/gorila/projects/test-project
---

# Test Time-Lab Integration

**Target Files:**
- test.txt

**Prompt:**
Create a file called test.txt with "Hello from AI-team + time-lab integration"

**Success Criteria:**
- [ ] test.txt exists
- [ ] Artifact written to time-lab

**Context:**
Testing time-lab artifact writing.
EOF

# Run orchestrator
python src/orchestrator.py

# Check time-lab received it
ls -la ~/time-lab/2025/10/17/test-project/test_timelab_001/
cat ~/time-lab/2025/10/17/test-project/test_timelab_001/artifact.json
cat ~/time-lab/2025/10/17/test-project/test_timelab_001/summary.txt
```

**Success Criteria:**
- ✅ AI-team processes task normally
- ✅ Curated artifact written to time-lab
- ✅ Project name extracted correctly
- ✅ Date folder created
- ✅ Summary is readable
- ✅ Link back to AI-team artifact works

---

### Phase 3: Build Time-Lab Query Tools (Week 3)

**Goal**: Make time-lab artifacts queryable

**Tasks:**

1. **Add schema validation**
   ```bash
   # time-lab/schemas/time-lab-artifact.schema.json
   {
     "$schema": "http://json-schema.org/draft-07/schema#",
     "type": "object",
     "required": ["schema_version", "task_id", "timestamp", "project"],
     "properties": {
       "schema_version": { "const": "time-lab-v1" },
       "task_id": { "type": "string" },
       "timestamp": { "type": "string", "format": "date-time" },
       "project": { "type": "string" },
       "task_type": { "type": "string" },
       "summary": { "type": "string" },
       "duration_seconds": { "type": "number" },
       "success": { "type": "boolean" },
       "files_changed": { "type": "array", "items": { "type": "string" } },
       "ai_team_artifact": { "type": "string" }
     }
   }
   ```

2. **Add query commands to Justfile**
   ```makefile
   # time-lab/Justfile

   # Show all artifacts for a date
   show date:
       @echo "📅 Artifacts for {{date}}"
       @find {{date}} -name "artifact.json" 2>/dev/null || echo "No artifacts found"

   # Query by keyword across all artifacts
   query term:
       @echo "🔍 Searching for: {{term}}"
       @grep -r "{{term}}" --include="*.json" --include="*.txt" . 2>/dev/null || echo "No matches"

   # List all projects
   projects:
       @echo "📦 Projects with artifacts:"
       @find . -type d -mindepth 4 -maxdepth 4 | cut -d/ -f4 | sort -u

   # Show project activity
   project name:
       @echo "📊 Activity for project: {{name}}"
       @find . -type d -name "{{name}}" | head -20

   # Statistics
   stats:
       @echo "📈 Time-Lab Statistics"
       @echo "Total artifacts: $(find . -name 'artifact.json' | wc -l)"
       @echo "Total projects: $(find . -type d -mindepth 4 -maxdepth 4 | cut -d/ -f4 | sort -u | wc -l)"
       @echo "Date range: $(find . -type d -mindepth 1 -maxdepth 1 -name '[0-9]*' | sort | head -1) to $(find . -type d -mindepth 1 -maxdepth 1 -name '[0-9]*' | sort | tail -1)"
   ```

3. **Test queries**
   ```bash
   cd ~/time-lab

   # Show today's work
   just show 2025/10/17

   # Search for "login"
   just query "login"

   # List projects
   just projects

   # Show auth-module activity
   just project auth-module

   # Overall stats
   just stats
   ```

**Success Criteria:**
- ✅ Can query by date
- ✅ Can search across all artifacts
- ✅ Can list projects
- ✅ Can see project-specific activity
- ✅ Statistics are accurate

---

### Phase 4: Multi-Project Validation (Week 3-4)

**Goal**: Prove it works with real projects

**Tasks:**

1. **Create tasks for 3 projects**
   ```bash
   cd ~/AI-team/tasks

   # Task 1: auth-module
   cat > auth_add_reset.task.md << 'EOF'
   ---
   id: auth_add_reset
   type: fix
   priority: high
   created: 2025-10-17T17:00:00Z
   cwd: /home/gorila/projects/auth-module
   ---

   # Add Password Reset Endpoint

   **Target Files:**
   - src/auth.py
   - tests/test_auth.py

   **Prompt:**
   Add a password reset endpoint to the authentication module.
   Include email sending logic and token generation.

   **Success Criteria:**
   - [ ] POST /auth/reset endpoint exists
   - [ ] Token generation works
   - [ ] Tests pass

   **Context:**
   Users need ability to reset forgotten passwords.
   EOF

   # Task 2: crypto-analyzer
   cat > crypto_gas_analysis.task.md << 'EOF'
   ---
   id: crypto_gas_analysis
   type: analyze
   priority: medium
   created: 2025-10-17T17:05:00Z
   cwd: /home/gorila/projects/crypto-analyzer
   ---

   # Analyze Gas Fees Pattern

   **Target Files:**
   - analysis/gas_fees.py

   **Prompt:**
   Analyze gas fee patterns over the last 30 days.
   Identify peak times and cost-saving opportunities.

   **Success Criteria:**
   - [ ] Gas fee data extracted
   - [ ] Peak times identified
   - [ ] Report generated

   **Context:**
   Optimize transaction timing to reduce costs.
   EOF

   # Task 3: discord-llm
   cat > discord_help_command.task.md << 'EOF'
   ---
   id: discord_help_command
   type: fix
   priority: low
   created: 2025-10-17T17:10:00Z
   cwd: /home/gorila/projects/discord-llm
   ---

   # Add /help Command

   **Target Files:**
   - bot/commands.py

   **Prompt:**
   Add a /help command that lists all available bot commands
   with descriptions and usage examples.

   **Success Criteria:**
   - [ ] /help command responds
   - [ ] Lists all commands
   - [ ] Examples are clear

   **Context:**
   Users need to discover bot capabilities.
   EOF
   ```

2. **Run AI-team, let it process all 3**
   ```bash
   cd ~/AI-team
   python src/orchestrator.py
   # Watch logs/events.ndjson
   ```

3. **Verify artifacts in time-lab**
   ```bash
   cd ~/time-lab

   # Should see all 3 projects
   just projects
   # Output:
   # auth-module
   # crypto-analyzer
   # discord-llm

   # Show today's work
   just show 2025/10/17
   # Should see all 3 tasks

   # Project-specific view
   just project auth-module
   # Should show auth_add_reset task
   ```

4. **Cross-project queries**
   ```bash
   # Find all "fix" tasks
   just query "\"task_type\": \"fix\""

   # Find work related to "command"
   just query "command"
   ```

**Success Criteria:**
- ✅ AI-team handles 3 projects correctly
- ✅ Each artifact goes to correct project folder
- ✅ Dates are correct
- ✅ Cross-project queries work
- ✅ No coupling between projects
- ✅ AI-team logs show clean execution

---

### Phase 5: Nix Integration (Week 4)

**Goal**: Reproducible environment for AI-team

**Add AI-team flake:**
```nix
# AI-team/flake.nix
{
  description = "AI Task Orchestrator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python311;
        pythonPackages = python.pkgs;
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            python
            pythonPackages.watchdog
            pythonPackages.pydantic
            pythonPackages.python-dotenv
            pythonPackages.pyyaml
            # Optional: LLAMA support
            pythonPackages.ollama
            # Optional: Telegram support
            pythonPackages.python-telegram-bot
            # Dev tools
            pythonPackages.ruff
            pythonPackages.black
            pythonPackages.mypy
            pythonPackages.pytest
            pythonPackages.pytest-asyncio
          ];

          shellHook = ''
            echo "🤖 AI-Team Development Environment"
            echo "Python: $(python --version)"
            echo ""
            echo "Available commands:"
            echo "  python src/orchestrator.py  # Start orchestrator"
            echo "  pytest                      # Run tests"
            echo "  ruff check src/             # Lint code"
            echo ""
            # Optionally activate venv if it exists
            if [ -d .venv ]; then
              source .venv/bin/activate
            fi
          '';
        };
      }
    );
}
```

**Usage:**
```bash
cd ~/AI-team
nix develop  # Enter reproducible environment
python src/orchestrator.py
```

**Update time-lab flake to reference AI-team:**
```nix
# time-lab/flake.nix additions
{
  # ... existing config ...

  devShells.ai-team = pkgs.mkShell {
    inputsFrom = [ AI-team.devShells.${system}.default ];
    buildInputs = [
      # time-lab tools
      pkgs.jq
      pkgs.just
      pkgs.ripgrep
    ];
  };
}
```

---

## 🎯 Success Metrics

After Phase 4, you should have:

1. **Working Integration**
   - AI-team processes tasks from centralized `AI-team/tasks/`
   - Each task has `cwd:` field pointing to project
   - Artifacts written to both AI-team (operational) and time-lab (archival)
   - Cross-project queries work

2. **Minimal Changes**
   - AI-team: 1 file removed (hardcoded path), 1 file added (time-lab writer)
   - Time-lab: Justfile commands, schema validation
   - Projects: No changes (no `.ai-team/` required yet)

3. **Flexibility**
   - Can add per-project `.ai-team/` later if desired
   - Can enhance queries without touching AI-team
   - Can swap AI-team for another tool (protocol is simple)

4. **Practical Value**
   - You can query "what login work did I do last month?"
   - You can see all work across projects for a given day
   - You can link back to full AI-team artifacts for debugging

---

## 🔧 Deferred: Not Now, Maybe Later

### Per-Project .ai-team Folders
**When to add:**
- When you have 10+ projects
- When task volume is high
- When you want tasks co-located with code

**How to add:**
```python
# Modify AI-team/src/core/file_watcher.py
# Add support for watching multiple directories
# Watch ~/projects/*/.ai-team/tasks/
```

### GitHub Watching
**When to add:**
- After Phase 4 works smoothly
- When you want CI/PR automation
- When you're comfortable with the system

### Advanced Queries
**When to add:**
- After Phase 4
- When basic queries aren't enough
- Consider: SQLite index, full-text search, LLM-powered queries

### Web UI
**When to add:**
- Way later (month 3+)
- When CLI queries feel limiting
- Consider: Simple Flask app, read-only view

---

## 📝 Key Takeaways

1. **AI-team doesn't need major refactoring** - It's well-designed, we're just adding an adapter

2. **Start centralized, evolve if needed** - Don't add `.ai-team/` to every project yet

3. **Time-lab is dumb storage** - No business logic, just organize and query

4. **Artifacts serve different purposes** - AI-team = operational, time-lab = archival

5. **Nix is optional enhancement** - Don't block on it, add for reproducibility later

6. **Test incrementally** - Phase 0 → Phase 4 with validation at each step

---

## 🚀 Next Action

**Start Phase 0 today:**

```bash
cd ~/AI-team

# 1. Edit config/settings.py:98 - remove hardcoded path
# 2. Create .env with Linux paths
# 3. Test with one task
# 4. Verify it works

# Then we proceed to Phase 1-2 next week
```

**This roadmap is your implementation guide. Follow it step-by-step.**
