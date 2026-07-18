#!/usr/bin/env python3
"""Operator driver — invoke the Manager loop from the command line.

There is no UI button for the Manager. The only invocation surface is
``POST /api/manager`` (control_api.ManagerInvokeBody): it boots a Case-owning
Manager session with the canonical ``docs/harness/roles/manager.md`` profile,
opens one Case, and delivers the objective as the first assignment turn. The
Manager then autonomously dispatches workers (``dispatch_worker(role='worker')``,
which presets the ``worker.md`` profile), reviews their committed diffs, and
closes the Case.

This script is a thin, self-authenticating wrapper over that endpoint so the
operator can fire the loop in one line. It pulls the dashboard token exactly the
way the gateway does (``config.config.mesh.dashboard_token``), so no token paste
is needed.

Three ways to give the Manager its objective:

  * ``--mode advance``  — read the project's own priorities and drive the next
                          UNBLOCKED item to completion (dispatch -> review -> close).
  * ``--mode derive``   — assess project state and DERIVE the next direction WITH
                          the operator; propose candidates, escalate the strategic
                          choice rather than dispatching blind.
  * ``-o "<objective>"`` — a concrete free-form objective (a specific fix/build).

Usage (run with the repo venv, from the repo root):

    .venv/bin/python scripts/invoke_manager.py --mode advance
    .venv/bin/python scripts/invoke_manager.py --mode derive
    .venv/bin/python scripts/invoke_manager.py -o "Fix X. TASK TYPE: fix. ..." \
        --criteria "tests green; diff verified; PR opened"

Add ``--dry-run`` to print the exact request body without invoking (no paid run).
The Manager loop invokes the live, paid Claude CLI — only fire it deliberately.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# Repo root on path so ``config`` imports resolve when run from anywhere.
_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# --- Objective templates for the two standing modes ------------------------

_ADVANCE_OBJECTIVE: str = (
    "Advance the AI-Team project. Ground yourself FIRST in .ai/CONTEXT.md (the "
    "'Current Priorities' table) and .ai/dispatch/DISPATCH_LOG.md, then verify state "
    "against git. Identify the single highest-ranked UNBLOCKED priority and drive it to "
    "completion through your loop: compose a grounded dispatch envelope, dispatch a "
    "worker, review the committed diff in git, and close (or rework) on the evidence. "
    "Do NOT invent new scope — pull the next real priority. If nothing is genuinely "
    "unblocked, stop and escalate with a recommendation instead of manufacturing work."
)
_ADVANCE_CRITERIA: str = (
    "The selected priority is delivered — committed diff verified in git, plain-pytest "
    "checks green, PR opened for any src/config/migration change — AND the DISPATCH_LOG / "
    "CONTEXT priority status is updated to reflect it; OR, if nothing was unblocked, a "
    "written escalation with a recommendation is surfaced to the operator."
)

_DERIVE_OBJECTIVE: str = (
    "Assess the AI-Team project's state and DERIVE the next direction WITH the operator — "
    "do not assume there is a queued task. Ground in .ai/CONTEXT.md, "
    ".ai/dispatch/DISPATCH_LOG.md, and git history. Determine honestly whether the current "
    "milestone arc is complete and what, if anything, is genuinely next. If there is no "
    "clear next spec/task, propose 2-3 candidate directions — each with rationale, risk, "
    "cost, and payoff — pick the one you would recommend, and ESCALATE the strategic choice "
    "to the operator. Do not dispatch code work without operator direction in this mode."
)
_DERIVE_CRITERIA: str = (
    "A written next-direction recommendation exists (candidate directions + rationale + "
    "your recommended pick) and is surfaced to the operator as an escalation. No code is "
    "dispatched or merged without explicit operator direction."
)


def _dashboard_token() -> str:
    """Resolve the control-API bearer token the same way the gateway does."""
    try:
        from config import config as _cfg  # type: ignore[import-not-found]

        token: str = _cfg.mesh.dashboard_token or _cfg.mesh.worker_token
    except Exception:  # pragma: no cover - fallback mirrors control_api._dashboard_token
        import os

        token = os.getenv("DASHBOARD_TOKEN", "") or os.getenv("WORKER_TOKEN", "")
    return token


def _resolve_objective(args: argparse.Namespace) -> tuple[str, Optional[str]]:
    """Return (objective, completion_criteria) from the chosen mode / free-form."""
    if args.objective:
        return args.objective, args.criteria
    if args.mode == "advance":
        return _ADVANCE_OBJECTIVE, args.criteria or _ADVANCE_CRITERIA
    if args.mode == "derive":
        return _DERIVE_OBJECTIVE, args.criteria or _DERIVE_CRITERIA
    raise SystemExit("Provide either --mode {advance|derive} or -o/--objective.")


def _invoke(url: str, token: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
    data: bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body: str = resp.read().decode("utf-8")
            return {"status": resp.status, "body": json.loads(body)}
    except urllib.error.HTTPError as exc:
        detail: str = exc.read().decode("utf-8", errors="replace")
        return {"status": exc.code, "body": detail}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Invoke the Manager loop (POST /api/manager).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["advance", "derive"],
        help="advance = drive the next unblocked priority; derive = propose the next direction.",
    )
    parser.add_argument("-o", "--objective", help="Free-form Case objective (overrides --mode).")
    parser.add_argument("--criteria", help="Completion criteria close_case will demand.")
    parser.add_argument(
        "--repo",
        default=str(_REPO_ROOT),
        help="repo_path the Manager grounds in (default: this repo root).",
    )
    parser.add_argument("--branch", help="Working branch context hint for the Manager.")
    parser.add_argument("--node", help="Pin the Manager session to a node (default: local host).")
    parser.add_argument("--model", help="Backend model override (default: backend default).")
    parser.add_argument(
        "--host", default="http://127.0.0.1:9003", help="Gateway control-API base URL."
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout seconds.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the request body; do NOT invoke (no paid run)."
    )
    args = parser.parse_args()

    objective, criteria = _resolve_objective(args)
    payload: dict[str, object] = {"objective": objective, "repo_path": args.repo, "backend": "claude"}
    if criteria:
        payload["completion_criteria"] = criteria
    if args.branch:
        payload["branch"] = args.branch
    if args.node:
        payload["node_id"] = args.node
    if args.model:
        payload["model"] = args.model

    if args.dry_run:
        print("DRY RUN — would POST /api/manager with:")
        print(json.dumps(payload, indent=2))
        return 0

    token: str = _dashboard_token()
    if not token:
        print("ERROR: no dashboard token resolved (config.mesh.dashboard_token / WORKER_TOKEN).", file=sys.stderr)
        return 2

    result = _invoke(f"{args.host}/api/manager", token, payload, args.timeout)
    print(json.dumps(result, indent=2))

    body = result.get("body")
    if isinstance(body, dict) and body.get("ok"):
        case_id = body.get("case_id")
        session_id = body.get("session_id")
        print("\nManager invoked. Watch it with:")
        if case_id:
            print(f"  curl -s -H 'Authorization: Bearer <token>' {args.host}/api/flows/{case_id} | jq")
        if session_id:
            print(f"  curl -s -H 'Authorization: Bearer <token>' {args.host}/api/sessions/{session_id}/timeline | jq")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
