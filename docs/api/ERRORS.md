# Control API — Error Catalog

_Generated 2026-10-13 by `scripts/api_docs/generate_api_docs.py` from `_REASON_STATUS` in `src/control/control_api.py`. Do not edit by hand._

Failed requests return a stable envelope:

```json
{ "ok": false, "reason": "<machine_code>", "detail": "<human prose>" }
```

The server owns the `reason` code; the client owns how it is worded for users. Map the code, not the prose.

## Reason codes

| HTTP | `reason` | Meaning |
| ---- | -------- | ------- |
| 400 | `dangerous_extension` | The upload was rejected because its file extension is disallowed. |
| 400 | `invalid_decision` | The decision value is not one of the accepted verdicts. |
| 400 | `invalid_repo_path` | The repository path is missing or outside the allowed roots. |
| 400 | `missing_action` | A required `action` field was absent from the request. |
| 400 | `no_repo_path` | The upload requires a repository path and none was provided. |
| 400 | `unknown_backend` | The requested backend id is not recognized. |
| 400 | `unknown_effort` | The reasoning-effort value is not one of the accepted levels. |
| 400 | `unknown_model` | The requested model id is not valid for the selected backend. |
| 404 | `not_found` | The target resource (e.g. an approval) does not exist. |
| 404 | `session_not_found` | No session exists with that id. |
| 409 | `already_resolved` | The approval/decision has already been resolved. |
| 409 | `harness_level3_needs_approval` | This is a Level-3 task and needs operator approval before it can run (HARNESS_LEVEL3_GUARD is armed). Retrying will not help. |
| 409 | `not_closed` | The operation requires a different session state (e.g. the session is not closed). |
| 410 | `backend_retired` | The backend has been permanently removed (e.g. the OpenCode CLI). Use `opencode-server` instead. |
| 413 | `file_too_large` | The uploaded file (or body) exceeds the configured size cap. |
| 503 | `turn_queue_unavailable` | A managed close could not reach the turn ledger; the request fails closed rather than silently. |

## Transport / HTTP-layer errors

These are enforced by the framework or middleware rather than a `CommandResult.reason`:

| HTTP | `reason` | Meaning |
| ---- | -------- | ------- |
| 401 | `unauthorized` | Missing or invalid bearer token. Send `Authorization: Bearer <DASHBOARD_TOKEN>`. |
| 413 | `payload_too_large` | The raw request body exceeded the streamed pre-parse cap and was rejected before parsing (`src/control/body_cap.py`). |
| 422 | `validation_error` | The request body failed Pydantic validation. The response `detail` lists the offending fields. |
| 500 | `server_misconfigured` | `DASHBOARD_TOKEN` is not configured on the gateway. |

