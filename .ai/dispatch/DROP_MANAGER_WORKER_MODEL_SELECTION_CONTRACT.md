# DROP - Manager worker model selection contract

**Date:** 2026-07-28
**Status:** merged + deployed on `main` (`63e1447`, 2026-07-28)
**Suggested branch:** `feat/manager-worker-model-selection-contract`

## Why this exists

The Manager can currently dispatch workers without explicitly choosing a model. In this checkout,
that omission resolves to the Claude default (`CLAUDE_DEFAULT_MODEL=opus`, and the Claude catalog
default is also `opus`), so the cheapest Manager behavior is accidentally the most expensive worker
behavior.

This is a behavioral/substrate contract problem, not a Manager-model problem. The operator can
choose the Manager model at invocation time and does not want that changed here. It is also not a
warm-session retiering problem: changing a model mid-session would invalidate the useful cache and
is not worth designing into this slice. A reused worker keeps its boot model.

The desired state is simple: when a Manager opens a new worker, it must deliberately decide which
model is suitable for that specific task and encode that decision in the dispatch call. The system
should prevent the Manager from silently omitting the decision.

## Current behavior, verified against code

- `docs/harness/roles/manager.md` already tells the Manager to tier workers with
  `dispatch_worker(model=...)` and to put hard architecture/design on stronger models while using
  lighter models for cheap plumbing and verification.
- `scripts/mcp_manager.py::_dispatch_worker` accepts optional `model` and sends it through the
  session-create seam (`POST /api/sessions` body), not through `/api/instructions`.
- The `dispatch_worker` MCP schema documents `model`, but the only required field is `objective`.
- `config.models.resolve_model()` resolves an unpinned Claude session through config/catalog
  defaults. With the current `.env` and catalog, omission means `opus`.
- Reused worker sessions intentionally keep their boot model. The dispatch tool warns when `model`
  is passed with `session_id`, but it cannot and should not retier that live SDK session.

Targeted offline verification already passed:

```text
.venv/bin/pytest \
  tests/test_mcp_manager.py::test_dispatch_worker_tiers_model_on_new_session \
  tests/test_mcp_manager.py::test_dispatch_worker_model_ignored_on_reused_session \
  tests/test_model_picker.py::test_resolve_falls_back_to_catalog_default_when_unpinned
```

## Root cause

The Manager has optional guidance, but the substrate accepts omission. Because the default is
expensive, the missing decision is charged as an expensive decision.

This is the wrong failure mode. A senior Manager should be forced to assign the right person for
the job; if it wants the sharpest person, it should say why. It should not get the sharpest person
by forgetting to choose.

## Goal

Make worker model selection explicit at every new-worker dispatch.

Acceptance meaning:

- A Manager opening a new worker session cannot omit model selection silently.
- The selected model is visible in the dispatch request, worker session, Case roster/telemetry, and
  tests.
- The Manager prompt/schema nudges the decision as a task-fit judgment, not as a hardcoded default.
- Existing warm-worker reuse remains valid and does not attempt mid-session model changes.
- The Manager invocation model is untouched.

## Recommended implementation

### 1. Introduce an explicit model-decision contract on `dispatch_worker`

Change `dispatch_worker` so a new worker dispatch (`cwd` present, no `session_id`) must include
either:

- `model`: the exact worker model to boot, or
- `model_decision`: a structured decision object that includes at least `model` and `rationale`.

The implementer should inspect MCP client/tool-schema behavior before choosing the final shape.
If nested objects are reliable in this MCP path, prefer:

```json
{
  "model_decision": {
    "model": "sonnet",
    "difficulty": "medium",
    "rationale": "Touches a focused backend path with tests; no architecture fork."
  }
}
```

If simple scalar schemas are materially more reliable, use flat fields:

```json
{
  "model": "sonnet",
  "model_difficulty": "medium",
  "model_rationale": "Touches a focused backend path with tests; no architecture fork."
}
```

Do not implement an automatic classifier as the primary mechanism. The point is to make the
Manager decide. A deterministic fallback may be useful for legacy compatibility, but it must not
hide omission in the new Manager path.

### 2. Refuse missing model selection for new workers

In `scripts/mcp_manager.py::_dispatch_worker`:

- If `session_id` is absent and `cwd` is present, require an explicit model decision before calling
  `/api/sessions`.
- Return a structured refusal/error that tells the Manager to classify the work and retry with a
  model choice.
- Keep existing refusal for no `cwd` and no `session_id`.
- Keep reused-session behavior unchanged: a `session_id` dispatch uses the existing boot model.
  Passing `model` with `session_id` should continue to be ignored/warned, or should be rejected as
  contradictory if that proves clearer in tests. Do not retier the live session.

### 3. Add a small, non-binding model rubric to the Manager prompt and tool schema

The rubric should guide judgment without pretending every task can be classified mechanically:

- `haiku` / light tier: narrow read-only checks, grep/code search, formatting-only edits, simple
  test updates, small plumbing where failure is easy to detect.
- `sonnet` / standard tier: most bounded implementation, bug fixes, integration work with tests,
  moderate diagnosis, code review where the blast radius is local.
- `opus` / strong tier: architecture decisions, unclear root cause across subsystems, high-risk
  migrations, security-sensitive work, adversarial review of major changes, ambiguous strategy.

Also require the Manager to include a short rationale in the dispatch envelope, e.g. a `MODEL
SELECTION:` line before `TASK:` or as a field near `TASK TYPE:`. The wording should frame expensive
models as scarce senior capacity: use them when outcome risk justifies the cost, not by default.

### 4. Preserve current model plumbing

The selected model must still flow through the existing proven path:

```text
dispatch_worker(model) -> POST /api/sessions model -> create_session -> session.model
-> resolve_model(session) -> ClaudeAgentOptions(model=...)
```

Do not add `model` to `/api/instructions`; that endpoint currently has no model field and would be
the wrong seam for newly opened worker sessions.

### 5. Make omission observable in tests and operator surfaces

Tests should prove:

- new worker with no model decision is refused before any API call;
- new worker with model decision creates a session with that model;
- reused worker with no model decision still dispatches, because the decision was made at boot;
- reused worker with conflicting model is not silently retiered;
- Manager role/tool text contains the model-selection contract;
- no change to `/api/manager` default/model behavior.

If the Case roster already shows session model, no UI change may be needed. If omission/refusal is
not visible enough in the Manager transcript, improve the tool error text rather than building a
new surface.

## Alternative considered: worker default model

A separate worker default such as `DISPATCH_WORKER_DEFAULT_MODEL=sonnet` would reduce spend, but it
does not produce the desired management behavior. It replaces one silent default with another. The
operator wants the Manager to understand that different workers have different cost/capability
profiles and to actively choose among them.

This may be acceptable as a later safety net for non-Manager/legacy callers, but it should not be
the primary fix for Manager dispatch.

## Alternative considered: automatic difficulty classifier

A hardcoded classifier inside `dispatch_worker` would be brittle and would move judgment out of
the Manager. The tool can validate that a decision exists and can reject contradictory inputs, but
it should not pretend to understand task complexity better than the Manager reading the objective,
repo state, and active Case context.

If a future implementation wants defaults, prefer policy hints plus explicit Manager override, not
an opaque classifier.

## Service boundary checklist

This task touches an MCP tool that accepts external input.

- **Concurrency:** No new long-running handler is required. The dispatch path already creates a
  worker task through the existing control API. The new validation should run before any network
  call and adds no scarce resource.
- **Memory at scale:** The proposed fields are bounded strings / small enum-like values. Preserve
  existing `_MAX_ID_CHARS` and `_MAX_OBJECTIVE_CHARS` style bounds.
- **Request size:** Keep model/rationale bounded. If `model_decision` is an object, validate every
  string field explicitly; do not accept arbitrary nested payloads.
- **Timeout:** No new blocking operation. Existing `_api_request` timeouts remain the boundary.
- **Malformed input:** Reject missing/invalid model decisions with a clear structured error before
  dispatching anything.
- **Backing resources:** If the control API is unavailable, existing `_api_request` behavior remains
  correct. The new validation should not mask API failures after a valid decision.

## Advisory review of this drop

Findings:

- **No false manager-model coupling:** This drop deliberately excludes `/api/manager` model defaults
  and Manager session retiering.
- **No warm-session cache trap:** It preserves the current rule that reused workers keep their boot
  model. That avoids cache invalidation and avoids implying an unsupported SDK behavior.
- **No hidden default:** The recommended fix refuses omission instead of silently substituting a
  cheaper model, because the desired outcome is Manager judgment, not merely lower average cost.
- **Schema risk noted:** Nested `model_decision` is better semantically, but MCP/schema reliability
  should be verified before implementation. A flat-field fallback is explicitly allowed.
- **Potential over-enforcement bounded:** The refusal should apply to newly opened worker sessions.
  Reused sessions already embody a prior model choice and should not be blocked just because the
  current turn omits a model.
- **Operational activation caveat:** Manager MCP schema changes are seen by newly booted Manager
  sessions. Existing live Manager sessions may need their MCP/tool subprocess/session restarted to
  see the new contract.

Recommended verdict: build this as a narrow MCP/role/tests slice. Do not add UI or broad policy
machinery unless implementation evidence shows the existing roster/transcript cannot expose the
decision.

## Closure — 2026-07-28

Implemented and adversarially reviewed on `main` (`63e1447`). The contract uses the existing flat
`model` field rather than a nested decision object: this MCP server publishes raw JSON Schema and
performs server-side validation, while the existing scalar model seam is already proven end-to-end.

- New worker dispatches require a non-empty `model` before any control API call. Strict Claude
  aliases are validated locally; advisory backend models preserve their existing pass-through policy.
- A malformed `POST /api/sessions` response without a valid `session_id` is now refused rather than
  falling through to a sessionless dispatch.
- Reused `session_id` dispatches keep their boot model and retain the existing explicit non-retiering
  response.
- `docs/harness/roles/manager.md` now requires a `MODEL SELECTION:` rationale and gives the
  haiku/sonnet/opus task-fit rubric. Case roster/telemetry already surfaces `session.model`; no UI
  or new persistence was added.
- `/api/manager` defaults remain unmodified and are regression-covered.

Verification: 110 targeted hermetic tests passed across the MCP tool, Manager role, Case worker
projection, and model picker. `ai-team-gateway` was restarted by PM2 after the commit; health probe
returned `{"status":"ok"}`. New Manager sessions now load the refreshed MCP tool contract.
