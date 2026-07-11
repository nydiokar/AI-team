# A33 — M3 Phase 3.0: `mcp_manager` sends `parent_flow_run_id` + poll retry tolerance

**Level:** 3 (code) · **Branch:** `feat/m3-phase30-mcp-manager` (continues A31/A32) · **Date:** 2026-07-10
**Reads with:** `AGENT_32_LINEAGE_ENDPOINT.md` (the endpoint this consumes),
`AGENT_31_M3_PHASE30_MCP_MANAGER.md` §"NOT done" items 1 & 6, `scripts/mcp_manager.py`.

---

## Objective-lock (bounded)

Now that A32 gave `POST /api/instructions` a `parent_flow_run_id`, make the Manager tool
actually **use** it, and harden the wait path:

1. `dispatch_worker` **sends** `parent_flow_run_id` (flip A31's deliberate "not sent"
   invariant — the honest reason for it, an endpoint gap, is gone).
2. `wait_for_worker` **tolerates transient poll failures** (A31 item 6) instead of aborting a
   long wait on a single gateway blip.

Out of scope (still): the live paid F4 spike + session wiring (A31 items 2/3).

---

## What shipped (this dispatch)

- **`scripts/mcp_manager.py`**
  - `_dispatch_worker`: when `parent_flow_run_id` is given it is now put in the POST body
    (omitted entirely when absent — no null-field leak). Module docstring, tool description,
    input-schema help, and the human reply are rewritten from "NOT yet persisted (endpoint gap)"
    to an **honest SHADOW-record** framing: recorded when the gateway runs `HARNESS_FLOW_DRIVE`
    ON, visible in `/api/flows`, *confirm — don't assume*, and always review the child's git diff.
  - `_wait_for_worker`: the poll body is wrapped so a transient `RuntimeError` from `_api_request`
    (network/HTTP blip) is tolerated. Up to `_MAX_CONSECUTIVE_POLL_ERRORS` (5) **consecutive**
    failures are absorbed (a clean poll resets the streak); beyond that it returns a clean `ERROR`
    string. Still bounded by the overall deadline; TIMEOUT now also surfaces the last poll error.
    Validation (`ValueError`) still raises before the loop — only transport errors are caught.
- **`tests/test_mcp_manager.py`** — replaced the "does not send" test with `sends_parent_lineage`
  + an `omits_when_absent` companion; added `tolerates_transient_poll_errors` (recovers → DONE)
  and `gives_up_after_persistent_errors` (clean ERROR, no raise, before a 3600s timeout).
  **22 passed** (was 19).

Run: `.venv/bin/python -m pytest tests/test_mcp_manager.py -q` (plain pytest — cost-guard clean).

## Adversarial review (self, pre-commit)

| Challenge | Resolution |
|---|---|
| Sending `parent_flow_run_id` to a gateway that predates A32 — does it 422? | No. `InstructionBody` is a plain pydantic model (no `extra="forbid"`) ⇒ an unknown field is ignored, not rejected. And on THIS branch the tool + endpoint ship together, so they agree. The reply no longer *claims* persistence — it says "recorded when HARNESS_FLOW_DRIVE ON; confirm via /api/flows" ⇒ no false lineage claim. |
| Could retry tolerance mask a real outage and burn the whole timeout? | No — 5 *consecutive* failures short-circuits to a clean ERROR (~12–15s at the default 3s poll), well before a long deadline. Test locks this against a 3600s timeout. |
| Flapping gateway (fail/ok/fail/ok) loops forever? | The streak resets on a clean poll, so flapping keeps polling — but the overall deadline still bounds it, ending in TIMEOUT with `last_error` surfaced. Intended "tolerate blips" behavior. |
| Non-transport exception swallowed? | Only `RuntimeError` (what `_api_request` raises) is caught; `ValueError` validation runs before the loop and still raises → surfaces as an MCP `isError`. `.get()`/`classify_status` don't raise. |
| Token/secret leak in the new ERROR/TIMEOUT strings? | No — they carry the `_api_request` message (host:port + server body), never the bearer token. |
| Blast on the live gateway? | None — `mcp_manager.py` is a standalone script, not imported by the app; the running gateway was not restarted (health `ok` before/after). |

---

## NOT done (unchanged — the live-spike gate)

Phase 3.0 acceptance still needs the **🟡 live F4 spike** (A31 item 2) + **session wiring**
(A31 item 3: `~/.claude.json` mcpServers `"manager"` entry, `_mcp_manager_configured()` gate +
`allowed_tools` append in `claude_driver`, session env carrying `DASHBOARD_TOKEN`+`SESSION_ID`).
Both are paid-CLI / global-config / live-gateway changes ⇒ operator or an explicit wiring
dispatch (Phase 3.1), not this loop. Open question A31 item 5 (does a plain worker dispatch reach
a terminal `flow_runs.status`?) is validated during that spike.

---

## Closure

**Status: `built` (branch `feat/m3-phase30-mcp-manager`).** The tool surface now records the
Manager→worker lineage edge end-to-end (A32 endpoint + A33 sender) and survives transient poll
blips. Phase 3.0's remaining gate is the live spike + session wiring — a distinct, higher-blast
step deliberately left to the operator/Phase 3.1. Per branch policy the code loop opens a PR at
close once the operator decides on the live spike; the three-commit stack (A31→A32→A33) lands
together.
