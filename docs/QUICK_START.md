# Quick Start - Test the AI Task Orchestrator

## 1. Start the System
```bash
cd orchestrator
# Optional: allow non-interactive permissions for CI
# On Windows PowerShell:
#   $env:CLAUDE_SKIP_PERMISSIONS="true"  # only for unattended runs
python main.py
```

**Expected Output:**
```
AI Task Orchestrator
===================

Component Status:
  Claude Code CLI: [OK] Available
  LLAMA/Ollama: [OK] Available (llama3.2:latest)
  File Watcher: [OK] Running

Orchestrator is running. Press Ctrl+C to stop.
Watching for task files in: C:\Users\...\orchestrator\tasks
```

## 2. Watch the Magic Happen

The system will automatically:
1. 🔍 **Detect** the test task file
2. 🦙 **LLAMA parses** the task intelligently (or fallback parser)
3. 🤖 **Claude Code** executes the task (least-privilege tools)
4. 🦙 **LLAMA summarizes** the results
5. ✅ **Validation** checks (lightweight sanity/structure)
6. 📁 **Saves** results and summaries, archives the task

## 3. Check Results (after ~30-60 seconds)

**Files Created:**
- `results/{task_id}.json`
- `summaries/{task_id}_summary.txt`

**Events (NDJSON):**
- `logs/events.ndjson` appends one line per event (e.g., `task_received`, `parsed`, `claude_started`, `summarized`, `validated`, `artifacts_written`, `task_archived`).

## 4. Verify Success

Read `VERIFICATION_CONTEXT.md` for detailed verification steps.

**Quick Check**: If both result files exist, the summary is non-empty, and `logs/events.ndjson` contains recent events, the system is working.

---

**This is your complete AI-powered coding automation system!** 🚀