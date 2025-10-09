## Operations & Artifacts

### Artifacts
- Results JSON: `results/<task_id>.json`
  - Contains: schema_version, success, errors, stdout/stderr triage, validation, orchestrator/bridge/llama status
- Summary text: `summaries/<task_id>_summary.txt`
- Processed tasks: `tasks/processed/*.completed.task.md` / `*.failed.task.md`
- Events: `logs/events.ndjson`

### Events
- Emitted for: task_received, parsed, claude_started, summarized, validated, claude_finished, artifacts_written, task_archived
- Format: newline-delimited JSON with timestamps

### Configuration
- Location: `config/settings.py`
- Highlights:
  - Claude: base command flags, base CWD, allowed root, skip permissions, timeout, max-turns
  - LLAMA: model name, host/port, caps (`max_prompt_chars`, `max_parse_chars`)
  - System: logs/summaries/results/tasks directories, worker count
  - Validation: thresholds (similarity/entropy)

### Operational Tips
- Ensure Claude CLI is installed and on PATH; `claude --version` should succeed
- Base working directory must exist and be within allowed root
- For flaky CLI behavior, stdin-piped prompts are more reliable than argv
- Check `logs/events.ndjson` for live troubleshooting; JSON artifacts include triage


