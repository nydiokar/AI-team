# AI Task Orchestrator

A production-ready system that coordinates AI-powered code tasks through file-based workflows, integrating Claude Code CLI with optional LLAMA/Ollama for intelligent task routing.

## 🏗️ Architecture

```
Task File → LLAMA Parser → Claude Code → Results → Summary → Notification
     ↓           ↓              ↓           ↓          ↓          ↓
  .task.md   Structured    Code Changes   Output   User Report  Telegram
             Request       + Analysis              (Optional)
```

## ✅ Current Status (Ready for Production)

**Phase 1 Complete:**
- ✅ File-based task system with `.task.md` format
- ✅ Intelligent component detection and fallbacks
- ✅ Claude Code CLI integration with automation
- ✅ LLAMA/Ollama integration with fallback parsing
- ✅ Async file watching and task processing
- ✅ Comprehensive logging and error handling
- ✅ Production-ready orchestrator with worker pools

## 🚀 Quick Start

### 1. Setup Environment (Windows PowerShell)

**💡 Performance Tip:** The basic installation is now much faster. Heavy dependencies (LLAMA, Telegram) are optional and only installed when needed.

```powershell
# Create and activate venv (recommended)
python -m venv .venv
. .venv\Scripts\Activate.ps1

# Install project in editable mode with dev and test extras
pip install -e ".[dev,test]"

# Optional extras (install only what you need)
# For Telegram integration
pip install -e ".[telegram]"
# For LLAMA (Ollama) mediation and sentence-transformers (optional)
pip install -e ".[llama]"

# Or install everything at once (slower)
pip install -e ".[dev,test,llama,telegram]"

# Copy environment template if present
if (Test-Path .env.example) { Copy-Item .env.example .env -Force }
```

### 2. Test Components

```bash
# Check system status
python main.py status

# Create a sample task
python main.py create-sample

# View help
python main.py help
```

### 3. Run Orchestrator

```powershell
# Optional non-interactive flag for CI/unattended runs
$env:CLAUDE_SKIP_PERMISSIONS = "true"

# Start the system
python main.py

# The system will:
# - Watch tasks/ directory for .task.md files
# - Process tasks automatically
# - Log all activity
```

## 📁 Directory Structure

```
orchestrator/
├── tasks/           # Drop .task.md files here (watched)
├── results/         # Task execution results
├── summaries/       # LLAMA-generated summaries
├── logs/           # System logs
├── config/         # Configuration files
├── src/            # Source code
│   ├── core/       # Core interfaces and parsers
│   ├── bridges/    # Claude & LLAMA integrations
│   └── validation/ # Validation engine
├── main.py         # Main CLI entry point
└── README.md       # This file
```

## 📝 Task File Format

Create `.task.md` files in the `tasks/` directory:

```yaml
---
id: task_001
type: fix|code_review|analyze|summarize
priority: high|medium|low
created: 2025-08-03T10:30:00Z
---

# Task Title

**Target Files:**
- /path/to/file1.py
- /path/to/file2.js

**Prompt:**
Detailed description of what you want Claude to do.

**Success Criteria:**
- [ ] Specific outcome 1
- [ ] Specific outcome 2

**Context:**
Additional background information.
```

## 🔧 Configuration

### Environment Variables (.env)

```bash
# Telegram Integration (Optional)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ALLOWED_USERS=123456789,987654321
TELEGRAM_CHAT_ID=123456789

# Claude Code Settings
CLAUDE_SKIP_PERMISSIONS=false
# Optional: base working directory where Claude starts (recommended)
# Example (Windows): C:\\Users\\User\\Projects
# Example (POSIX): /home/you/Projects
CLAUDE_BASE_CWD=
# Optional: safety allowlist root (defaults to CLAUDE_BASE_CWD if set)
CLAUDE_ALLOWED_ROOT=
# Execution behavior caps
CLAUDE_TIMEOUT_SEC=300
CLAUDE_MAX_TURNS=0   # 0 means CLI default/unlimited

# System Settings
LOG_LEVEL=INFO
MAX_CONCURRENT_TASKS=3
```

- `python main.py doctor` prints the effective configuration, checks Claude CLI availability, shows relevant env overrides, and verifies key directories.
- `python main.py tail-events [--task TASK_ID] [--lines N]` prints recent events from `logs/events.ndjson` without blocking the orchestrator.
- Telegram: `/progress <task_id>` shows the last events for a task (optional, when Telegram is configured).
- At runtime, the system can `reload_from_env()` to pick up changes to `CLAUDE_*` values without restart.

### Telegram Bot Integration

The system includes an optional Telegram bot interface for remote task management:

**Setup:**
1. Create a bot with [@BotFather](https://t.me/botfather) on Telegram
2. Get your bot token and add it to `.env`:
   ```bash
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_ALLOWED_USERS=your_user_id  # Optional: restrict access
   ```
3. Install the dependency: `pip install python-telegram-bot>=20.0`

**Commands:**
- `/start` - Welcome message and help
- `/help` - Detailed help and examples
- `/task <description>` - Create a new AI task
- `/status` - Show system status and task queue
- `/progress <task_id>` - Show recent events for a task
- `/cancel <task_id>` - Request task cancellation

**Natural Language:**
You can also just send messages like:
- "Create a new pijama directory and set up a Python project there"
- "Review the authentication code in /auth-system"
- "Fix the database connection timeout in /backend"

**Working Directory Support:**
- Use "in /project-name" for relative paths under your Projects folder
- Use "in C:\\path\\to\\project" for absolute Windows paths
- If no path specified, Claude starts in your configured base directory

**Notifications:**
The bot automatically notifies you when tasks complete or fail, with summaries and next steps.

### Component Detection

The system automatically detects available components:

- **Claude Code CLI**: Checks for `claude --version`
- **LLAMA/Ollama**: Attempts `ollama list`
- **Fallback Mode**: Uses built-in parsers if components unavailable

### Prompt Policy

For reliability, we use a pass-through prompt policy with minimal framing:
- Pass the task's prompt verbatim
- Add `Target Files:` and `Context:` when provided
- Let Claude Code decide how to reason and which allowed tools to use
- Guardrails are provided by validation and structured results/NDJSON, not heavy prompt instructions

## 🖥️ Production Setup

### On Your Workstation (Full Setup)

```bash
# 1. Install Claude Code CLI
# Follow: https://docs.anthropic.com/en/docs/claude-code

# 2. Install Ollama
# Follow: https://ollama.ai/download

# 3. Pull a model
ollama pull llama3.1:8b

# 4. Start orchestrator
python main.py
```

### Current Environment

```bash
# Uses LLAMA parsing (or fallback) and real Claude Code CLI execution
python main.py
```

## 📊 Monitoring

### Logs
- **Console**: Real-time status updates
- **File**: `logs/orchestrator.log` (rotated automatically, ~1MB x 3)
- **Events (NDJSON)**: `logs/events.ndjson` — one line per event (`task_received`, `parsed`, `claude_started`, `claude_finished`, `summarized`, `validated`, `artifacts_written`, `task_archived`, `retry`).

### Status Commands
```bash
python main.py status          # Component status
python main.py create-sample   # Test task creation
python main.py clean tasks                 # Archive loose tasks to tasks/processed
python main.py clean artifacts --days 30   # Prune old results/summaries
python main.py validate-artifacts          # Validate results/*.json against schema
python main.py tail-events [--task TASK_ID] [--lines N]   # Show recent NDJSON events
python main.py doctor                      # Diagnostics + effective env/config
```

## 🔄 Workflow Examples

### 1. Code Review Task
```bash
# Create task file: tasks/review_auth.task.md
# System automatically:
# 1. Detects new file
# 2. Parses with LLAMA (or fallback)
# 3. Creates optimized Claude prompt
# 4. Executes with Claude Code CLI
# 5. Summarizes results
# 6. Logs completion
```

### 2. Bug Fix Task
```bash
# Drop: tasks/fix_db_connection.task.md
# Results appear in: results/fix_db_connection_result.json
# Summary in: summaries/fix_db_connection_summary.txt
```

## 🔮 Next Phase Features (Ready to Implement)

When you're on your workstation with full components:

1. **Telegram Integration**
   - `/task` command creates tasks
   - Real-time notifications
   - Status queries

2. **Advanced Validation**
   - LLAMA hallucination detection
   - Code quality validation
   - Success criteria verification

3. **Enhanced Task Types**
   - Multi-step workflows
   - Dependency management
   - Scheduled tasks

## 🐛 Troubleshooting

### Common Issues

1. **Unicode Errors**: System handles encoding automatically
2. **Permission Issues**: Configure Claude CLI permissions
3. **Component Not Found**: System falls back gracefully

### Debug Mode
```bash
# Enable debug logging
LOG_LEVEL=DEBUG python main.py
```

## 🎯 Success Criteria Met

- ✅ **File-based task creation works**
- ✅ **LLAMA can parse and route tasks** (with fallback)
- ✅ **Claude Code integration automated**
- ✅ **Basic system monitoring**
- ✅ **Robust error handling**

The system is **production-ready** and will seamlessly upgrade when you add Claude Code CLI and LLAMA/Ollama on your workstation!