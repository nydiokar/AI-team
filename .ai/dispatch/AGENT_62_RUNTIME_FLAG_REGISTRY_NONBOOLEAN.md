```yaml
job_id: AGENT_62_RUNTIME_FLAG_REGISTRY_NONBOOLEAN
created_at: "2026-08-01T13:48:33+03:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T13:20:23.070427+00:00"
```

# DISPATCH — A62 · Runtime flag registry: extend beyond booleans to numeric/string knobs

**Level:** 2 (internal config plumbing, no external provider calls) · **Type:** code
**Authored:** 2026-08-01 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (builds on the already-merged runtime flag registry: `src/control/db.py`
`RUNTIME_FLAG_DEFINITIONS`/`render_runtime_flag`/`set_runtime_flag`, `GET/PUT/DELETE /api/flags`
in `src/control/control_api.py`, and `scripts/ops_flag.sh` operator CLI, all live on `main`).

## Why (intent)
The operator asked to get "all controllable flags" into the registry so they stop being tracked
by hand across `.env`. Today `/api/flags` covers **23 of ~97** documented env keys
(`docs/ENV_FEATURE_FLAGS.md`) — all of them booleans. The registry's storage/type model
(`_truthy_flag` coercion, `"1"/"0"` raw values) is boolean-only by construction, so the other ~74
keys — mostly numeric tuning knobs and a few strings — cannot be added as-is. The two the operator
named explicitly while exploring this: **`CLAUDE_SDK_MAX_TURNS`** and **`CLAUDE_SDK_MAX_BUDGET_USD`**
(the M3.3/A53 turn/cost governor budget — currently `config/settings.py` env-only fields, visible
read-only via `GET /health` but with no write path at all, registry or otherwise).

## Current state (verify against tree before trusting this summary)
- `src/control/db.py:168-307` `RUNTIME_FLAG_DEFINITIONS` — 23 boolean entries only.
- `render_runtime_flag()` (`db.py:348`) and `set_runtime_flag()` — value handling assumes
  `"1"`/`"0"` boolean coercion throughout (`_truthy_flag`).
- `RuntimeFlagBody` (`control_api.py:221`) — `value: bool` only; API rejects non-bool JSON today.
- `config/settings.py:113-114` — `sdk_max_turns: Optional[int]`, `sdk_max_budget_usd: Optional[float]`,
  read once at settings construction from `CLAUDE_SDK_MAX_TURNS`/`CLAUDE_SDK_MAX_BUDGET_USD`. No
  registry involvement; `GET /health` surfaces them read-only (`control_api.py:691-701`).
- `scripts/ops_flag.sh` — CLI already handles arbitrary JSON values fine for `get`/`explain`/`list`
  (it just prints `.value`); the parts that assume boolean are `on`/`off` (hardcoded `true`/`false`)
  and `migrate` (reads `.value` back as a bare token for the PUT body, which happens to work for
  numbers too — verify this holds once numeric support lands, don't assume).

## TASK
1. **Extend the registry's type model.** Add a `value_type` (or similar) field to each
   `RUNTIME_FLAG_DEFINITIONS` entry (`bool` | `int` | `float` | `str`), and make
   `render_runtime_flag`/`set_runtime_flag`/`runtime_flag_enabled` type-aware: coerce/validate per
   `value_type` instead of assuming `_truthy_flag` everywhere. Boolean flags must keep working
   byte-identically (regression risk: don't change the 23 existing entries' external behavior).
2. **Widen the API.** `RuntimeFlagBody.value` needs to accept `bool | int | float | str` (a proper
   discriminated/union Pydantic model, not `Any`), validated server-side against the target flag's
   declared `value_type` — reject a string value for an int flag with a clear 422, not a silent
   coercion.
3. **Add the two governor knobs as the first real numeric case:** `CLAUDE_SDK_MAX_TURNS` (int,
   nullable — `None` means uncapped, decide how "unset" is represented for a non-bool registry
   entry) and `CLAUDE_SDK_MAX_BUDGET_USD` (float, nullable). Route the SDK driver's read of these
   through the same registry-then-env-then-default precedence that booleans already use (today it's
   a one-shot `os.getenv` read in `config/settings.py:448-458` — decide whether this needs to become
   a live per-request read for the flag to actually be "live"-scoped, or whether `startup`-scope
   is acceptable given these are per-session SDK options, not global state — **surface this seam
   choice, don't guess**).
4. **Update `scripts/ops_flag.sh`** — `on`/`off` remain boolean-only (clear error if pointed at a
   non-bool flag); add a `set <FLAG> <VALUE>` command for numeric/string flags, type-validated
   client-side to match the API's rejection with a decent message. `migrate`'s "read current value,
   PUT it back" loop must keep working for non-bool flags without change (verify, don't assume).
5. **Do NOT bulk-migrate the other ~72 documented env keys in this job.** Land the type-extension
   machinery + the two governor knobs as the proven case. A follow-up job (reference this one) can
   walk the rest of `docs/ENV_FEATURE_FLAGS.md` category-by-category once the pattern is validated
   live — many of those 72 are bootstrap/worker-side and architecturally excluded from ever being
   registry-writable (same reasoning as the 11 non-writable booleans today); don't assume they all
   qualify.

## TYPE
code. Branch `feat/runtime-flag-registry-nonbool`; PR at close; merge per branch policy
(`src/` change ⇒ branch + PR, agent owns push/merge). Flags/behavior must stay byte-identical for
every existing boolean entry — this is additive type-model work, not a rewrite.

## CONTEXT (reuse verbatim)
- Registry core: `src/control/db.py:168-307` (`RUNTIME_FLAG_DEFINITIONS`), `:314-372`
  (`runtime_flag_registry_writable`/`runtime_flag_enabled`/`render_runtime_flag`), `:997-1063`
  (`get_runtime_flag`/`set_runtime_flag`/`delete_runtime_flag`/`list_runtime_flags`).
- API: `src/control/control_api.py:221` (`RuntimeFlagBody`), `:705-772` (`GET/PUT/DELETE /api/flags`).
- Governor knobs: `config/settings.py:16,19-20,113-114,448-458` (env read), `control_api.py:690-701`
  (`/health` read-only surface).
- Operator CLI: `scripts/ops_flag.sh` (this session's new tool — `list`/`explain`/`get`/`on`/`off`/
  `unset`/`migrate`).
- Reference inventory: `docs/ENV_FEATURE_FLAGS.md` — the full ~97-key surface, categorized; use this
  to scope what's plausibly registry-writable later, not to bulk-migrate now.
- Existing tests: `tests/test_control_api.py` (flag routes, incl. the `flag_not_registry_writable`
  409 path — must stay green), `tests/test_sdk_governor.py` (governor knob behavior — must stay
  green through the read-path change).

## ACCEPTANCE (proof, not vibes)
1. All 23 existing boolean flags: `GET/PUT/DELETE /api/flags/*` behave byte-identically (existing
   tests green, no regressions).
2. `CLAUDE_SDK_MAX_TURNS`/`CLAUDE_SDK_MAX_BUDGET_USD` appear in `GET /api/flags` with correct
   `value_type`, current effective value, and correct source (`registry`/`env`/`default`).
3. `PUT /api/flags/CLAUDE_SDK_MAX_TURNS` with a valid int succeeds and is reflected in `/health`'s
   governor block (respecting whatever live-vs-startup scope decision was made and documented).
4. `PUT` with a wrong-typed value (e.g. string into the int flag) returns a clear 422, not a silent
   coercion or a 500.
5. `scripts/ops_flag.sh set CLAUDE_SDK_MAX_TURNS 40` works end-to-end; `migrate` still round-trips
   correctly for both the old boolean flags and the two new numeric ones.
6. Targeted `pytest` on touched modules only (TEST COST GUARD — no full/e2e suite).

## RESERVED DECISIONS (surface, do not guess)
- **R1 — live vs startup scope for the governor knobs.** Per-session SDK options aren't obviously
  "live" the way a boolean gate checked per-request is. Decide and document the actual read timing
  before claiming `effect_scope: live`.
- **R2 — null representation.** Booleans have no "unset" concept beyond falling through the
  registry→env→default chain; a nullable numeric flag (uncapped turns/budget) needs an explicit
  convention (e.g. empty string in the row = "no cap") — pick one and document it in
  `RUNTIME_FLAG_DEFINITIONS`'s docstring/comment.
- **R3 — bulk-migration follow-up.** Explicitly scope-out extending to the other ~72 keys here; file
  it as a separate future job once this pattern is proven live, not assumed safe to batch.

## SCOPE OUT
Migrating any of the other ~72 documented env keys beyond the two governor knobs. Building a Web UI
surface for numeric flags (CLI/API only, matching current boolean-flag UX). Changing the 23 existing
boolean entries' semantics.

## TRAIL / EVIDENCE (fill at close)
- Type-model diff · governor knob round-trip via API + `/health` · 422 on wrong-typed PUT ·
  `ops_flag.sh set`/`migrate` transcript · targeted pytest result · byte-identical re-verify for the
  23 existing booleans.

---
## Milestone (burndown)
- [ ] Registry type model extended (`value_type` field + type-aware coerce/validate), 23 existing
      booleans byte-identical
- [ ] `RuntimeFlagBody` widened + server-side type validation (422 on mismatch)
- [ ] `CLAUDE_SDK_MAX_TURNS`/`CLAUDE_SDK_MAX_BUDGET_USD` added as registry entries, R1/R2 decided
      and documented
- [ ] `scripts/ops_flag.sh set` command added; `on`/`off`/`migrate` verified compatible
- [ ] Targeted tests green; PR opened per branch policy

## Closure (fill on completion)
(fill when executed)
