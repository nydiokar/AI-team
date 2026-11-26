```yaml
job_id: AGENT_119_OPENCODE_V2_FOUNDATION
created_at: "2026-11-26T10:21:20.724107+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-26T10:21:20.724123+00:00"
```

---

# DISPATCH — A119 · OpenCode V2 migration foundation

**Level:** 3 (auth/secret handling + service-boundary probe + >~5 files; authored ready, operator authorization required before execution) · **Type:** audit + test harness + docs
**Authored:** 2026-11-26 · **Packet state:** ready (authored, not executed)
**Issue:** [#310](https://github.com/nydiokar/AI-team/issues/310)
**Depends on:** none. Coordinate with **A118** (issue #298, active) and **A102** (backend unification, merged S1/S2); do not duplicate either.
**Branch:** `feat/opencode-v2-foundation` from current `origin/main`; PR + review + push + self-merge under project policy. No worker restart.

> **Read this first — why this packet exists.** The gateway's only live OpenCode backend
> (`src/backends/opencode.py`) still speaks the V1 unprefixed HTTP API. The installed CLI
> is V2 (`opencode v2.4.2`), and the V2 server answers the adapter's unauthenticated
> readiness probe with `401`. V2 also removes the assistant `parentID` the gateway uses
> to attribute a reply to a managed turn and renames every SSE event the activity/telemetry
> reader recognizes. The later refactor cannot be planned honestly from the old API
> assumptions. This job builds the offline contract harness and decision record that the
> refactor will consume; it does not perform the cutover.

## Why (intent)

After this job, an agent picking up the OpenCode refactor can trust three things without
rediscovering them: (1) a reproducible, paid-free V2 contract probe and fake server that
pin routes, auth, prompt idempotency, message/event shapes and byte/time bounds; (2) a
decision record naming which V2 capabilities are adopted, deferred or rejected and why;
(3) an explicit seam map between the V2 contract and the carrier's `TurnControl` /
`ExecutionResult` ownership contract. The literal request is "make the integration work
on V2"; the real goal is a **reviewable foundation** that prevents another silent
cross-layer success (a green adapter that loses replies or telemetry on a live turn).

## TASK

1. **Ground and reconcile.** Read `docs/OPENCODE_V2_MIGRATION_AUDIT.md` end to end,
   the V2 OpenAPI (<https://opencode.ai/docs/openapi.json>) and the V1 contract at tag
   `v1.18.32` (`packages/sdk/openapi.json`). Re-derive the §2/§3/§4 tables against those
   sources; cite exact schema names. Do not use the V1 website as an API source — its
   server URL now serves V2 docs. Update the audit doc where a claim is wrong or too
   strong, with evidence. Verification: doc diff cites schema/route names; no new claims
   about the unproven incident.
2. **Author a read-only V2 probe.** Add `scripts/opencode_v2_probe.py` (Python 3.12,
   venv stdlib only). It must: report installed `opencode --version`; check `serve --help`
   flags; call `/api/info`, `/api/config` and `/api/provider` against either a supplied
   base URL or a short-lived locally spawned `serve`; verify that no-auth gets `401` and
   that Basic auth with username `opencode` succeeds; report config normalization
   (`mcp` → `mcp.servers`); and emit a machine-readable JSON summary plus a short human
   summary. Accept the password only from an env var or the managed `service.json`,
   never from argv; **never print, log or persist the password**. Fixed short deadline per
   request, bounded response bytes, structured failures on malformed JSON, and no prompt/
   session/message mutation. Verification: `python scripts/opencode_v2_probe.py --help`,
   a run against the already-running local service (no model call), and
   `python -m py_compile scripts/opencode_v2_probe.py`.
3. **Build the fake V2 contract server + tests.** Add `tests/test_opencode_v2_contract.py`
   with an in-process HTTP/SSE fake that implements the minimal V2 surface the refactor
   needs: authenticated `/api/info`; `POST /api/session` with `id`/`parentID`/`location`;
   `POST /api/session/{id}/prompt` with native `id` idempotency (same id returns original;
   reuse elsewhere `409`), `text`, `metadata`, `delivery`, `resume:false`;
   `GET /api/session/{id}/message` returning `{data, cursor}` with `Session.Message.Info`
   items; `/api/session/{id}/interrupt?resume=...`; `/api/session/{id}/compact` with
   `{id?, delivery?}`; and an SSE stream using V2 names (`session.inbox.delivered`,
   `session.execution.succeeded`, `session.step.ended`, `session.tool.success`,
   `session.usage.updated`, `session.message.content.updated`). Tests must assert the
   contract, the auth rejection, bounded body/frame sizes and malformed-input rejection.
   They must not call a real CLI, a model, or a paid endpoint. Verification:
   `.venv/bin/python -m pytest tests/test_opencode_v2_contract.py -q`.
4. **Write the adoption decision record.** Extend `docs/OPENCODE_V2_MIGRATION_AUDIT.md`
   §4/§5 so every capability row has a final recommendation, rationale, owner and the
   evidence that settles it. Resolve at minimum: prompt `id` + `metadata` as the native
   submission identity; rejecting `parentID`/proximity attribution; event allow-list for
   `turn.touch()`, activity and telemetry; auth/secret ownership; whether `steer|queue`
   and `resume:false` stay off at first cutover; why `wait`, child sessions and a Node
   sidecar are deferred. Record unresolved items as explicit operator gates, not guesses.
   Verification: doc review + a short checklist in this packet's TRAIL mapping each
   decision to its evidence.
5. **Map seams, do not change turn behavior.** Add a concise seam table to the same doc:
   V2 contract element → adapter symbol/line → carrier ownership seam (`TurnControl`,
   `ExecutionResult`, late-capture sink, quota/usage projection). State which seams this
   job verifies offline and which still need a live natural turn. Do **not** edit
   `src/backends/opencode.py`, config, migrations, the worker daemon or the gateway
   runtime in this job. Verification: `git diff --stat` touches only the allowed files.
6. **Targeted verification only.** Use the `running-targeted-tests` skill to choose exact
   targets. Run the new contract tests plus the existing OpenCode managed-turn/backend/
   server-integration tests. Do not run the full suite, `pytest --run-e2e`, or any paid
   CLI call. Verification: exact commands and pass/fail counts recorded in TRAIL.

## TYPE

Code + audit + docs → cut `feat/opencode-v2-foundation`, commit the scoped diff, open a PR,
run CI, and self-merge to `main` yourself. No `src/` runtime change, no DB migration, no
config flag flip, no gateway redeploy, no worker restart.

## CONTEXT (reuse verbatim)

- `docs/OPENCODE_V2_MIGRATION_AUDIT.md` (2026-11-26) is the durable audit: V2
  removes unprefixed routes, requires Basic auth, changes prompt/message/event schemas and
  removes assistant `parentID`. Treat its §8 "verified vs unverified" table as the
  honesty boundary.
- `src/backends/opencode.py:225-238` derives the managed `msg_` id; `:1232-1255` accepts
  only an assistant whose `parentID` equals it; `:1173-1208` recognizes V1 event names;
  `:1376-1489` spawns and health-checks an unauthenticated `opencode serve`; `:1505-1595`
  enforces 8 MiB HTTP and 256 KiB SSE caps.
- `src/core/turn_liveness.py` and `src/control/turn_queue.py` own stall/hard-cap, fencing
  and typed ownership outcomes. `src/worker/agent.py:2031-2070` handles late managed
  results. Any V2 correlation design must preserve that ownership contract.
- A102 moved turn policy to the carrier and deleted the CLI backend + PrintResume;
  `src/backends/registry.py` keeps `opencode` retired and `opencode-server` active.
  Do not reintroduce a CLI path.
- A118 / issue #298 owns the reported reply-loss incident. Its review gate forbids
  proximity-only or synthetic-marker-only attribution. This job must not declare the
  incident fixed or claim its root cause.
- Cost guard: real e2e is opt-in only (`AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`)
  and is out of scope. No paid model turn is authorized by this packet.

## ACCEPTANCE (proof, not vibes)

1. `scripts/opencode_v2_probe.py` exists, compiles, passes `--help`, and a read-only run
   against the local V2 service produces JSON showing version, auth status (`401` →
   `200`), config normalization and bounded request/response behavior. Password is
   absent from the output by construction.
2. `tests/test_opencode_v2_contract.py` exists and passes offline; it fails if the fake
   drops auth, native prompt `id` idempotency/`409`, `{data,cursor}` message shape, V2
   event names, byte caps or malformed-input rejection.
3. `docs/OPENCODE_V2_MIGRATION_AUDIT.md` contains the completed §4 decision
   record and §5 seam map with explicit operator gates; every claim that depends on a
   live paid turn is marked unverified.
4. Targeted pytest commands and exact results (counts) are recorded in TRAIL; no full
   suite or e2e was run.
5. `git diff --stat` shows no change to `src/backends/opencode.py`, `src/worker/agent.py`,
   `config/`, migrations, schema or Compose files. No gateway redeploy and no worker
   restart occurred.
6. PR is open/merged under project policy and the packet's YAML `evidence:` lists the
   probe script, contract test and updated audit doc; `dispatch_state.py --audit` shows
   no new gap from this job.

## RESERVED DECISIONS (surface, do not guess)

- **R1 — Native submission identity.** Default: prompt `id` carries the carrier
  `turn_uuid` identity and `metadata` carries versioned ownership lineage; no assistant
  `parentID` reliance. Operator/A118 ruling needed before any live turn.
- **R2 — Inbox delivery policy.** Default: keep `steer|queue` and `resume:false` OFF at
  first cutover; document the trade-off. Enabling them changes ordering and ownership.
- **R3 — Auth ownership.** Default: gateway owns a generated password in memory via
  `OPENCODE_PASSWORD`, sends Basic auth on HTTP and SSE, never logs it. Whether to move
  from per-repo `serve` to the managed V2 service is an operator decision.
- **R4 — Experimental/large capabilities.** Default: defer `wait`, `interrupt?resume`,
  child-session `<background-result>`, `fork/revert`, plugins and a Node sidecar until a
  natural-turn proof exists. None of these is silently adopted by this foundation job.
- **R5 — Incident truth.** A118 owns the incident ruling. If its evidence changes the
  ownership contract, update this record; do not resolve it from proximity heuristics.

## SCOPE OUT

- No production flag, no route/prefix swap in the adapter, no V1 deletion, no
  `arch-contract`. The later cutover job owns cutover and retirement.
- No edits to `src/backends/opencode.py`, worker daemon, config, migrations, schema,
  Compose, dashboard, or Telegram surfaces.
- No gateway restart/redeploy, worker restart, paid CLI call, real e2e, or full test suite.
- No claim that issue #298 is fixed, no claim about the incident worker's OpenCode version.
- No rewriting of host `~/.config/opencode/` files or plugin porting.
- No new top-level Markdown support files; durable output is the existing `docs/` audit.

## TRAIL / EVIDENCE (fill at close)

- Planned proof paths: `scripts/opencode_v2_probe.py`, `tests/test_opencode_v2_contract.py`,
  `docs/OPENCODE_V2_MIGRATION_AUDIT.md`.
- Record here: probe output summary (redacted), exact targeted pytest command + counts,
  doc decision checklist (R1–R5 → evidence), `git diff --stat`, PR number and merge SHA.
- Any supporting artifact must live in `.ai/dispatch/evidence/A119_*` with bidirectional
  links; never as a top-level dispatch sibling.

---

## Milestone (burndown)

- [ ] Re-derived §2/§3/§4 against v2 OpenAPI and v1.18.32 spec; corrected any wrong claim.
- [ ] `scripts/opencode_v2_probe.py` compiles, `--help` works, redacted read-only run recorded.
- [ ] `tests/test_opencode_v2_contract.py` passes offline with auth/idempotency/shape/event/bounds assertions.
- [ ] Audit §4 decision record completed for R1–R5 with evidence and unresolved gates.
- [ ] Audit §5 seam map completed; verified vs live-unverified seams stated.
- [ ] Targeted tests run with exact counts; no full suite/e2e/paid call.
- [ ] Scoped PR opened, CI green, self-merged; `evidence:` set; audit clean.

## Closure (fill on completion)

Honest summary at close per `docs/harness/generators/closure_summary.md`: what changed per
file, exact verification commands and results, R1–R5 outcomes, what remains gated, and the
handoff to the later OpenCode V2 cutover job and A118.
