## Components Reference

### Orchestrator (`src/orchestrator.py`)
- Role: Coordinates the entire pipeline
- Key methods:
  - `start/stop`: lifecycle and worker management
  - `_handle_new_task_file`: validate → parse → enqueue
  - `process_task`: execute Claude, summarize, validate, persist
  - `load_compact_context`: small context for UIs/prompts
- Dependencies: `TaskParser`, `AsyncFileWatcher`, `ClaudeBridge`, `LlamaMediator`, `ValidationEngine`

### FileWatcher (`src/core/file_watcher.py`)
- Role: Detect `.task.md` files with debounce
- Notes: Marshals events into asyncio loop; non-recursive directory watch

### TaskParser (`src/core/task_parser.py`)
- Role: Parse YAML frontmatter and Markdown sections into `Task`
- Regex nuances: lookahead guards to avoid truncating blocks on section headers

### ClaudeBridge (`src/bridges/claude_bridge.py`)
- Role: Execute tasks via Claude Code CLI (headless)
- Behavior: stdin-piped prompt, structured JSON output, allowed tools, max-turns/timeout
- Triage: captures stdout/stderr, attempts JSON parsing, detects interactive prompts

### LlamaMediator (`src/bridges/llama_mediator.py`)
- Role: LLAMA parsing, prompt creation, and summarization (when available)
- Fallbacks: Robust rule-based parsing and prompt templates

### ValidationEngine (`src/validation/engine.py`)
- Role: Lightweight similarity/entropy/structure checks
- Optional deps: sentence-transformers; falls back to Jaccard heuristics

### TelegramInterface (`src/telegram/interface.py`)
- Role: Optional chat-based control and notifications
- Commands: `/task`, `/status`, `/cancel`, agent templates

## Dependencies Map
- Orchestrator → FileWatcher, TaskParser, ClaudeBridge, LlamaMediator, ValidationEngine, TelegramInterface
- ClaudeBridge → config, Claude CLI
- LlamaMediator → Ollama client (optional), config
- ValidationEngine → config (thresholds), optional ML libs


