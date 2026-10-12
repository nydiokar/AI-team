#!/usr/bin/env python3
"""Generate the Control API reference artifacts *from the running app code*.

Why this exists
---------------
The gateway's HTTP surface is defined in ``src/control`` as FastAPI routers. A
hand-written API reference drifts from that code the moment either side changes.
This script instead *derives* the reference from the live app object, so the
committed artifacts always mirror what the server actually serves:

  * ``docs/api/openapi.json`` -- the OpenAPI 3.1 schema emitted by the app,
    enriched with a curated metadata overlay (title/description, auth scheme,
    resource tags, servers) and the **uniform error contract** every endpoint
    shares (401 / 413 / 422 + the ``{ok, reason, detail}`` envelope).
  * ``docs/api/ERRORS.md`` -- the error catalog, generated directly from the
    ``_REASON_STATUS`` map in ``control_api.py`` so the documented ``reason``
    codes cannot drift from the ones the server emits.

The app builds with a stub orchestrator: ``build_control_api`` constructs no
state at import/build time (routes touch the orchestrator only at call time),
so no DB, network, or live gateway is required. Run from the repo root:

    .venv/bin/python scripts/api_docs/generate_api_docs.py

Honesty note (deliberate, visible gap): request bodies are fully modelled
(Pydantic), and the error contract is attached to every operation. Success
*response* bodies are mostly un-modelled today (handlers return raw
``JSONResponse``); rather than hand-invent schemas that would drift, those are
documented via the shared ``CommandResult`` envelope and left as an explicit
next pass. Owning the gap beats faking precision.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs" / "api"
OPENAPI_PATH = DOCS_DIR / "openapi.json"
ERRORS_PATH = DOCS_DIR / "ERRORS.md"

# Ensure `src` is importable when run from anywhere.
sys.path.insert(0, str(REPO_ROOT))

from src.control.control_api import build_control_api, _REASON_STATUS  # noqa: E402

API_VERSION = "1.0.0"

# --- Resource tags -----------------------------------------------------------
# Map the second path segment -> a human resource group, so the reference is
# navigable instead of a flat list of 96 operations.
TAG_FOR_SEGMENT: dict[str, str] = {
    "sessions": "Sessions",
    "instructions": "Sessions",
    "cases": "Cases",
    "manager": "Cases",
    "work": "Work",
    "flows": "Work",
    "turns": "Turns",
    "turn-requests": "Turns",
    "turn-queue": "Turns",
    "cost": "Cost & Metrics",
    "metrics": "Cost & Metrics",
    "quota-windows": "Cost & Metrics",
    "system-alerts": "Cost & Metrics",
    "flags": "Admin",
    "approvals": "Admin",
    "push": "Admin",
    "git": "Admin",
    "backends": "Admin",
    "models": "Admin",
    "nodes": "Monitoring",
    "mesh": "Monitoring",
    "artifacts": "Monitoring",
    "events": "Monitoring",
    "tasks": "Monitoring",
    "jobs": "Monitoring",
    "projects": "Monitoring",
    "agent": "Monitoring",
    "cache-heartbeats": "Monitoring",
}

TAG_ORDER_AND_DESC: list[tuple[str, str]] = [
    ("Health", "Unauthenticated liveness/status probe."),
    ("Sessions", "Create, inspect and drive agent sessions (lifecycle, instructions, transcript, usage, uploads)."),
    ("Turns", "The managed turn queue: admit, list, edit, withdraw and resolve turn requests."),
    ("Cases", "Manager case control: open/close cases, record reviews, decompose work, wait groups, specs and artifacts."),
    ("Work", "Read-only projections over work/case flows (graph, timeline)."),
    ("Monitoring", "Read-only observability: nodes, mesh health, events (SSE), artifacts, tasks and jobs."),
    ("Admin", "Runtime control: feature flags, approvals, browser push, git operations."),
    ("Cost & Metrics", "Cost explorer, usage/metrics, quota windows and alerts."),
]

# Request bodies capped by the streamed pre-parse guard (src/control/body_cap.py).
# These paths can return 413 before the body is even parsed.
BODY_CAPPED = [
    re.compile(r"^/api/turn-requests/[^/]+$"),
    re.compile(r"^/api/instructions$"),
    re.compile(r"^/api/sessions/[^/]+/turn-requests$"),
]

PUBLIC_PATHS = {"/health", "/api/events/stream"}


def _api_description() -> str:
    return (
        "Self-hosted control plane for local coding agents. This reference is "
        "**generated from the running application** (`scripts/api_docs/"
        "generate_api_docs.py`) so it mirrors the code, not a hand-written copy "
        "that drifts.\n\n"
        "### Authentication\n"
        "All `/api/*` endpoints require a bearer token:\n\n"
        "```\nAuthorization: Bearer <DASHBOARD_TOKEN>\n```\n\n"
        "The token is compared in constant time. Public exceptions (no auth): "
        "`GET /health` and `GET /api/events/stream` (which authenticates in-handler).\n\n"
        "### Response envelope\n"
        "Lifecycle/mutation endpoints return a stable envelope:\n\n"
        "```json\n{ \"ok\": true, \"reason\": null }\n```\n\n"
        "On failure the body carries a machine-readable `reason` code and a "
        "human `detail`; the server owns the code, the client owns the wording. "
        "Every `reason` and its HTTP status is catalogued in "
        "[`ERRORS.md`](./ERRORS.md).\n\n"
        "### Trying it live\n"
        "This static spec is for browsing offline. For an authenticated "
        "**try-it** console, start the gateway with `CONTROL_API_DOCS=true` and "
        "open its same-origin Swagger UI at `/docs`."
    )


def _error_components() -> tuple[dict[str, Any], dict[str, Any]]:
    """Reusable schemas + responses for the uniform error contract."""
    schemas = {
        "ErrorEnvelope": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "example": False},
                "reason": {
                    "type": "string",
                    "description": "Stable machine code. See ERRORS.md.",
                    "example": "session_not_found",
                },
                "detail": {
                    "type": "string",
                    "description": "Human-readable explanation (optional).",
                },
            },
            "required": ["ok", "reason"],
        },
        "CommandResult": {
            "type": "object",
            "description": "Success envelope returned by lifecycle/mutation endpoints.",
            "properties": {
                "ok": {"type": "boolean", "example": True},
                "reason": {"type": "string", "nullable": True, "example": None},
            },
            "required": ["ok"],
        },
    }
    responses = {
        "Unauthorized": {
            "description": "Missing or invalid bearer token.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"},
                    "example": {"ok": False, "reason": "unauthorized",
                                "detail": "Invalid token"},
                }
            },
        },
        "PayloadTooLarge": {
            "description": "Request body exceeded the streamed pre-parse cap "
                           "(rejected before parsing).",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"},
                    "example": {"ok": False, "reason": "payload_too_large",
                                "detail": "Request body too large"},
                }
            },
        },
    }
    return schemas, responses


def _overlay(spec: dict[str, Any]) -> dict[str, Any]:
    spec["info"] = {
        "title": "AI-Team Control API",
        "version": API_VERSION,
        "description": _api_description(),
        "license": {"name": "See repository LICENSE"},
    }
    spec["servers"] = [
        {"url": "http://127.0.0.1:9003", "description": "Local gateway (default)"}
    ]
    spec["tags"] = [{"name": n, "description": d} for n, d in TAG_ORDER_AND_DESC]

    schemas, responses = _error_components()
    spec.setdefault("components", {}).setdefault("schemas", {}).update(schemas)
    spec["components"].setdefault("responses", {}).update(responses)

    for path, item in spec.get("paths", {}).items():
        segment = path.split("/")[2] if path.startswith("/api/") and len(path.split("/")) > 2 else None
        tag = TAG_FOR_SEGMENT.get(segment, "Health" if path == "/health" else "Monitoring")
        body_capped = any(rx.match(path) for rx in BODY_CAPPED)
        for method, op in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            op["tags"] = [tag]
            # Prettify auto summaries ("Api Create Session" -> "Create Session").
            summ = op.get("summary")
            if isinstance(summ, str) and summ.startswith("Api "):
                op["summary"] = summ[4:]
            responses_obj = op.setdefault("responses", {})
            secured = bool(op.get("security")) and path not in PUBLIC_PATHS
            if secured:
                responses_obj.setdefault(
                    "401", {"$ref": "#/components/responses/Unauthorized"}
                )
            if body_capped:
                responses_obj.setdefault(
                    "413", {"$ref": "#/components/responses/PayloadTooLarge"}
                )
    return spec


def generate_openapi() -> dict[str, Any]:
    app = build_control_api(MagicMock())
    spec = app.openapi()
    spec = _overlay(spec)
    OPENAPI_PATH.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    ops = sum(
        1
        for item in spec["paths"].values()
        for m in item
        if m in {"get", "post", "put", "patch", "delete"}
    )
    print(f"wrote {OPENAPI_PATH.relative_to(REPO_ROOT)}  ({len(spec['paths'])} paths, {ops} operations)")
    return spec


# --- Error catalog -----------------------------------------------------------
# Human copy per reason code. Derived from the inline comments in control_api.py.
REASON_DOC: dict[str, str] = {
    "unknown_backend": "The requested backend id is not recognized.",
    "backend_retired": "The backend has been permanently removed (e.g. the OpenCode CLI). Use `opencode-server` instead.",
    "unknown_model": "The requested model id is not valid for the selected backend.",
    "unknown_effort": "The reasoning-effort value is not one of the accepted levels.",
    "invalid_repo_path": "The repository path is missing or outside the allowed roots.",
    "session_not_found": "No session exists with that id.",
    "not_closed": "The operation requires a different session state (e.g. the session is not closed).",
    "turn_queue_unavailable": "A managed close could not reach the turn ledger; the request fails closed rather than silently.",
    "not_found": "The target resource (e.g. an approval) does not exist.",
    "already_resolved": "The approval/decision has already been resolved.",
    "invalid_decision": "The decision value is not one of the accepted verdicts.",
    "missing_action": "A required `action` field was absent from the request.",
    "no_repo_path": "The upload requires a repository path and none was provided.",
    "dangerous_extension": "The upload was rejected because its file extension is disallowed.",
    "file_too_large": "The uploaded file (or body) exceeds the configured size cap.",
    "harness_level3_needs_approval": "This is a Level-3 task and needs operator approval before it can run (HARNESS_LEVEL3_GUARD is armed). Retrying will not help.",
}

# HTTP-layer errors that are not reason-coded in _REASON_STATUS.
HTTP_LAYER_ERRORS: list[tuple[str, str, str]] = [
    ("401", "unauthorized", "Missing or invalid bearer token. Send `Authorization: Bearer <DASHBOARD_TOKEN>`."),
    ("413", "payload_too_large", "The raw request body exceeded the streamed pre-parse cap and was rejected before parsing (`src/control/body_cap.py`)."),
    ("422", "validation_error", "The request body failed Pydantic validation. The response `detail` lists the offending fields."),
    ("500", "server_misconfigured", "`DASHBOARD_TOKEN` is not configured on the gateway."),
]


def generate_errors(today: str) -> None:
    lines: list[str] = []
    lines.append("# Control API — Error Catalog")
    lines.append("")
    lines.append(f"_Generated {today} by `scripts/api_docs/generate_api_docs.py` "
                 "from `_REASON_STATUS` in `src/control/control_api.py`. Do not edit by hand._")
    lines.append("")
    lines.append("Failed requests return a stable envelope:")
    lines.append("")
    lines.append("```json")
    lines.append('{ "ok": false, "reason": "<machine_code>", "detail": "<human prose>" }')
    lines.append("```")
    lines.append("")
    lines.append("The server owns the `reason` code; the client owns how it is "
                 "worded for users. Map the code, not the prose.")
    lines.append("")
    lines.append("## Reason codes")
    lines.append("")
    lines.append("| HTTP | `reason` | Meaning |")
    lines.append("| ---- | -------- | ------- |")
    for reason, status in sorted(_REASON_STATUS.items(), key=lambda kv: (kv[1], kv[0])):
        doc = REASON_DOC.get(reason, "—")
        lines.append(f"| {status} | `{reason}` | {doc} |")
    lines.append("")
    lines.append("## Transport / HTTP-layer errors")
    lines.append("")
    lines.append("These are enforced by the framework or middleware rather than a "
                 "`CommandResult.reason`:")
    lines.append("")
    lines.append("| HTTP | `reason` | Meaning |")
    lines.append("| ---- | -------- | ------- |")
    for status, reason, doc in HTTP_LAYER_ERRORS:
        lines.append(f"| {status} | `{reason}` | {doc} |")
    lines.append("")
    ERRORS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {ERRORS_PATH.relative_to(REPO_ROOT)}  ({len(_REASON_STATUS)} reason codes)")


def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    # Stamp date from the system clock at run time; callers can override via argv[1].
    today = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    generate_openapi()
    generate_errors(today)


if __name__ == "__main__":
    main()
