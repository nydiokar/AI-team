# Missing Data Note — Session `064dcf70-e1de-4738-9a25-599cc58e5f06`

## Data we could see

- Full Claude Code transcript (499 records, 1.2 MB) from `~/.claude/projects/`
- Every user prompt (5 distinct text messages + tool result callbacks)
- Every assistant response with per-NDJSON-line `usage` fields (input_tokens, output_tokens, cache metrics)
- All error metadata (type, status code)
- Git commit history on `feat/webui-ui0`
- Worker's `claude_code.py` subprocess management code

## Data NOT available from this machine

| Missing piece | Where it lives | How to get it |
|---|---|---|
| **Gateway-side logs** | The separate gateway computer | SSH into the gateway machine, check its application logs for the session UUID |
| **Actual model used** | Not recorded in transcript | Check gateway logs, or the `config/models.py` model resolution at `~/.claude/config/models.toml` |
| **Precise API cost / billing** | Anthropic API dashboard | Use the `requestId` values (`req_011CcN4WRpwPSoDJjAhjfiY5`, etc.) from assistant records to look up the exact cost in the Anthropic Console |
| **Debug log** | `~/.claude/debug/claude-{pid}-{session}.log` | Does not exist — either `CLAUDE_DEBUG` was not set, or the file was cleaned up |
| **Worker log entry** | `logs/` directory | The worker doesn't log individual SDK-proxied sessions to separate files; session-level logging would require enabling it in the gateway's orchestrator |

## Recommendations for future investigations

1. **Enable debug logging** on the gateway server by setting `CLAUDE_DEBUG=1` in the gateway's environment before launch.
2. **Log session_id in worker stdout** — add a log line in `claude_code.py:_run` when a new subprocess is spawned with the session ID, so the worker logs contain a searchable record.
3. **Gateway logs** — either provide SSH access to the gateway machine or forward its logs to a shared location.
