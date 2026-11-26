# OpenCode v2 migration audit — gateway/task-server integration

**Date:** 2026-11-26 · **Repo state:** `3553f9f` · **Installed CLI:** `opencode v2.4.2`
**Role:** durable reference doctrine for the OpenCode refactor. It is not a live status
surface; current work lives in `.ai/CONTEXT.md` and `.ai/dispatch/`.

## Executive summary

The current `opencode-server` adapter is a **V1-era HTTP client**. V2 is not a URI-prefix
rename: it removes the unprefixed routes the adapter uses, changes authentication,
replaces the prompt/ack contract, removes the assistant `parentID` that the gateway's
managed-turn attribution depends on, and renames almost every SSE event the activity
and telemetry reader recognizes. A V2 `serve` started the way the adapter starts it
answers `401` to the adapter's unauthenticated readiness probe (observed locally).

**Principal-engineer verdict:** the refactor must be treated as a protocol-boundary
rewrite with an explicit ownership contract, not a compatibility patch. Do it behind a
contract-test harness and a feature gate, in coordination with A118 (reply-recovery
incident #298). Do not claim the incident is fixed by this migration: A118's evidence
and ownership ruling remain open.

---

## 1. Evidence base and limits

Verified here:

- Official migration doc: <https://opencode.ai/docs/migrate-v1/> (read 2026-11-26).
- Official V2 API/OpenAPI: <https://opencode.ai/docs/api>, <https://opencode.ai/docs/openapi.json>.
- V1 contract: `anomalyco/opencode` tag `v1.18.32`, `packages/sdk/openapi.json`
  (the version the current adapter code says it mirrored).
- Local CLI: `opencode --version` → `v2.4.2`; `opencode serve --help` still exposes
  `--hostname`, `--port`, `--service`, `--stdio`.
- Local V2 probes (read-only, no model calls):
  - managed background service `/api/info`: `401` without Basic auth, `200` with
    username `opencode` and the configured password.
  - freshly spawned `opencode serve --hostname 127.0.0.1 --port <free>`: `401` with no
    auth and `401` with the *background service's* password; stderr prints a separate
    generated server password. Setting `OPENCODE_PASSWORD=<test>` makes the same probe
    `401` without auth and `200` with `opencode:<test>`.
  - `opencode api get /api/config` shows the host's V1 `mcp` block normalized to
    `mcp.servers`; V1 config files are still read (no repo `opencode.json` exists).
- Repo map: `src/backends/opencode.py` is the only live OpenCode backend; the CLI
  backend is retired (`src/backends/registry.py`, A102 R3).

Not verified, and not claimed:

- The incident runtime for session `a5940c108ba8` / task `task_f0cd17ef`, or the
  OpenCode version on the worker that produced it. A118 owns that evidence gap.
- Any paid model turn, real completion, or live e2e. Real e2e is opt-in only
  (`AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`) and is out of scope here.
- Production behavior of the V2 managed service under gateway launch conditions.
- End-to-end persisted-result → UI projection for V2 message shapes.

---

## 2. Breaking changes that hit this integration

### 2.1 Routes: unprefixed V1 routes are gone

`v1.18.32` exposes both unprefixed and `/api/`-prefixed routes. V2 exposes only the
`/api/` family. The adapter currently calls the unprefixed family exclusively.

| Adapter call (`src/backends/opencode.py`) | V2 route | Notes |
|---|---|---|
| `GET /global/health` (`:1469`) | `GET /api/info` | V2 has no `/global/health`. `/api/info` still requires Basic auth. |
| `POST /session` (`:620`) | `POST /api/session` | V2 adds `id`, `parentID`, `location`, `metadata`, `permissions`; V1 `model.id` becomes V2 `modelID`. |
| `POST /session/{id}/prompt_async` (`:805`) | `POST /api/session/{id}/prompt` | Contract changed, see §2.3. |
| `GET /session/{id}/message?limit=N` (`:854`, `:890`, `:957`) | `GET /api/session/{id}/message` | Response is `{data, cursor}`; each item is a `Session.Message.Info`, not `{info, parts}`. |
| `GET /session/{id}/message/{id}` (`:937`) | same under `/api/` | Useful for ack reconciliation, but the correlated message is now an inbox item id. |
| `POST /session/{id}/abort` (`:974`, `:1551`) | `POST /api/session/{id}/interrupt` | `resume` moved to a query flag (`true`/`false`). |
| `GET /session/status` (`:927`) | no direct route | Use `GET /api/session/{id}` (`time.busy`/`time.idle`, `outcome`) and `session.status` events; `POST /api/experimental/session/{id}/wait` is available but experimental. |
| `GET /config/providers` (`:879`) | `GET /api/provider` | V2 response is `{location, data}`. |
| `POST /session/{id}/summarize` (`:905`) | `POST /api/session/{id}/compact` | Body is `{id?, delivery?}`, not `{providerID, modelID}`. |
| `DELETE /session/{id}` (`:412`) | same under `/api/` | — |
| `GET /event` SSE (`:1127`) | `GET /api/event` | Event names changed, see §2.5. |

**Impact:** every HTTP path in the adapter is stale. A route-prefix-only patch would still
fail on auth, payload, response shape, and event names.

### 2.2 Authentication: V2 is always authenticated

V1 allowed a local unauthenticated `serve`; the adapter sends no `Authorization` header
and deliberately skips an `auth.json` pre-flight (`:1387-1391`). V2 servers use HTTP
Basic with the fixed username `opencode`; `OPENCODE_PASSWORD` (or legacy
`OPENCODE_SERVER_PASSWORD`) sets the password, `OPENCODE_SERVER_USERNAME` is ignored.
A standalone `serve` generates a password and prints it to stderr — which this adapter
captures into a pipe and never drains until process death.

**Impact:** current readiness probe cannot pass on V2; all subsequent calls fail before
any turn is submitted. Any migration must own password generation/injection without
writing it to logs, argv, DB, or the dashboard, and must add Basic auth to HTTP *and*
SSE requests.

### 2.3 Prompt/ack contract changed

| V1 `prompt_async` body | V2 `prompt` body |
|---|---|
| `parts: [{type:"text", text}]`, required | `text: string`, required |
| `messageID` (client-chosen id) | `id` (native idempotency key) |
| `model`, `agent`, `system`, `tools`, `variant`, `noReply` | `files`, `agents`, `skills`, `metadata`, `delivery: steer|queue`, `resume: false` |
| `204 No Content` | `200 {data: Session.Inbox.User}` |

V2 semantics matter for the gateway's retry/fencing logic:

- Re-sending the same `id` in the same session returns the original item; reusing it in
  another session or for another item returns `409`.
- `metadata` is stored on the message and is opaque to the model — a native home for
  gateway `turn_uuid`, session/task identity, and ownership version.
- `resume:false` admits the item to the inbox without starting the agent.
- `delivery` defaults to `steer`; `queue` waits for the current work to finish and then
  delivers one item at a time. This is a native alternative to the current
  "busy/unknown ⇒ pre-submit `OwnershipConflictError`" policy, but it changes turn
  ownership and must not be enabled silently.

### 2.4 Correlation: assistant `parentID` is gone

V1 assistant messages required `parentID` (verified in the v1.18.32 schema); the
gateway derives a deterministic `msg_…` id from the carrier's `turn_uuid` and accepts
only a terminal assistant message whose `parentID` equals it
(`managed_message_id` at `:225-238`, `_find_correlated_response` at `:1232-1255`).

V2 `Session.Message.Assistant` has no `parentID`; it carries `id`, `metadata`, `type`,
`agent`, `model`, `content`, `finish`, `native`, `cost`, `tokens`, `error`, `retry`, and
timestamps. Assistant text is in `content[{type:"text", text}]`, not `parts[]`.

**Impact:** the current attribution key cannot survive the protocol change. This is a
separate, stronger break than A118's known compaction-continuation defect: the field
the current code reads does not exist in V2 at all. The replacement must be designed
and reviewed, not inferred:

- carry gateway ownership in prompt `id` + opaque `metadata`;
- correlate completion through `session.inbox.delivered`, `session.execution.*`,
  `session.step.*` / `session.message.content.updated`, and message history;
- never fall back to "latest reply by time" or a synthetic marker — A118's review gate
  explicitly forbids proximity-only attribution.

### 2.5 Events: the current SSE reader will see almost nothing

The adapter recognizes `session.status`, `message.part.updated`, `session.idle`,
`permission.asked`, and `session.error` (`:1173-1208`). In V2:

- `message.part.updated`, `session.error`, `message.updated`, `message.part.delta`,
  `file.edited`, and `command.executed` are absent from the OpenAPI event types.
- Replacements include `session.tool.called|input.*|progress|success|failed`,
  `session.step.started|first-token|streamed|ended|failed`, `session.text.*`,
  `session.reasoning.*`, `session.execution.started|succeeded|failed|interrupted`,
  `session.inbox.enqueued|delivered|cancelled|delivery.changed`,
  `session.message.content.updated`, `session.usage.recorded|updated`, and
  `session.compaction.started|ended|failed`.

**Impact:** every native progress event stops calling `turn.touch()`, so a long working
turn can hit stall limits; activity labels disappear; tool and usage telemetry emit
nothing. This is a silent correctness/observability regression, not just missing UI
decoration.

### 2.6 Configuration and default behavior

- V1 `opencode.json(c)` files and `.opencode/` definitions remain readable; conversion
  to native V2 shape is optional. Do not rewrite host config from the gateway.
- Native V2 renames: `permission`→ordered `permissions`; `bash`→`shell`, `task`→`subagent`,
  `write`/`patch`→`edit`; `agent`/`mode`→`agents`; `prompt`→`system`; `snapshot`→`snapshots`;
  `attachment`→`media`; `mcp`→`mcp.servers`; `plugin`→`plugins`; `command`→`commands`;
  `reference`→`references`; `provider`→`providers`; `npm`→`package`; `api`→`settings.baseURL`;
  model `id`→`modelID`; compaction `preserve_recent_tokens`/`reserved`→`keep.tokens`/`buffer`.
- V2 groups MCP under `mcp.servers`, inverts `enabled` to `disabled`, and splits timeouts.
  The host's existing V1 `mcp` block was normalized on load; `scripts/setup_mcp.py` still
  writes the compatible V1 shape.
- V1 plugin implementations do not run in V2; this repo uses MCP, not plugins, so no
  plugin port is currently required.
- V2 does not ask before `external_directory`; add an explicit `ask` permission rule to
  preserve V1 behavior if that matters for a workspace.
- Todos are off unless `experimental.todo`; LSP config is accepted but not run.
- Terminal `tui.json(c)` → global `cli.json`; the host already has `cli.json`. No repo
  file needs this migration.

### 2.7 Generated clients

V2 ships `@opencode/client` (network) and `@opencode/sdk` (embedded, no listener), with
types generated from the same OpenAPI contract. The gateway is Python and currently
hand-rolls `urllib` HTTP/SSE plus 8 MiB / 256 KiB caps. A Node sidecar would remove
schema drift but add a process boundary and new resource/secret surface; defer it until
the Python adapter's contract tests prove drift is a recurring cost.

---

## 3. Current workarounds and fallacies the refactor must retire or re-justify

| Current assumption | Why it was reasonable | Why V2 breaks it |
|---|---|---|
| Module docstring: `POST /session/{id}/message` is blocking send+receive (`:6`) | V1 had that route; A102 later moved live code to `prompt_async` | Stale even on V1; V2 removes the blocking route. Delete the claim. |
| Deterministic `msg_` id mirrors V1 `Identifier`; attribution via assistant `parentID` (`:225-238`, `:1232`) | V1 assistant messages required `parentID` | V2 assistant schema has no `parentID`; native idempotency moved to prompt `id` + `metadata`. |
| No auth pre-flight because `auth.json` is an unreliable logout signal (`:1387-1391`) | Avoided false negatives in V1 | V2 auth is the Basic service password; no auth means 401 before any prompt. |
| Event stream is advisory; status + history are authority (`:1201-1207`) | Still sound in principle | The status/history *shapes* changed and all recognized event names vanished. Reconciliation must be rewritten, not merely kept. |
| Session-wide `abort` is safe only while our latest user message is running (`:952-981`) | V1 had a latest-user-message hint and `parentID` | V2 inbox messages have no assistant parent link; "latest user" is no longer exclusive ownership. Use native id/metadata/inbox provenance or keep fail-closed. |
| `prompt_async` returns 204; a lost ack is reconciled by polling for the message id (`:797-824`) | V1 contract | V2 returns the admitted inbox item; id reuse is a native `409`. Reconcile with native semantics. |
| Compaction via `POST /summarize` with provider/model and pre-snapshot ids (`:869-920`) | V1 contract plus A118's known compaction gap | V2 `compact` takes `{id?, delivery?}`. A118's structural defect is independent and must be resolved by its own reviewed ownership contract. |
| One unauthenticated server per repo dir, max 8 slots (`:1415-1489`) | V1 loopback service | V2 still permits standalone `serve`, but requires password; if the managed service is adopted instead, re-certify slot/FD/memory bounds and shared-service isolation. |
| No per-session MCP (`:494-496`) | OpenCode config is per process/directory | Still true in V2; keep capability off. Do not leak a sender token to neighbouring sessions. |
| `OPENCODE_SERVER_ENABLED=false` in `.env.example` | Legacy flag | Dead since A102; remove from examples/docs when touching config. |

---

## 4. V2 capabilities this integration is not using (adopt / defer)

| Capability | Evidence | Why it matters here | Recommendation |
|---|---|---|---|
| Prompt `id` idempotency + `409` on reuse | V2 prompt schema | Replaces hand-rolled deterministic-id reconciliation; native duplicate safety after carrier crash | **Adopt** as the primary submission identity, with gateway `turn_uuid` also in `metadata`. |
| Opaque `metadata` on prompt/user message/session | V2 prompt + `Session.Message.User` | Carries ownership/task lineage without relying on message genealogy | **Adopt**; define a versioned key namespace and never expose secrets there. |
| `delivery: steer|queue`, `resume:false` | V2 prompt schema | Native backpressure and deferred admission | **Defer** until the ownership contract is settled; enabling queue silently changes turn ordering. |
| `session.inbox.*` events | V2 OpenAPI | Gives enqueue/deliver/cancel provenance for the id we submitted | **Adopt** in the SSE parser; correlate by `inboxID`, never by proximity. |
| `session.execution.*`, `session.step.*`, `session.text.*`, `session.tool.*` | V2 OpenAPI | Replaces vanished `message.part.updated`/`session.error`; restores `turn.touch()`, activity, tool telemetry | **Adopt** with a bounded allow-list; keep unknown events ignored, never trusted as success. |
| `session.usage.recorded|updated`, assistant `tokens`/`cost` | V2 OpenAPI | Current usage reader only understands `message.info.tokens` | **Adopt**; keep `telemetry.coverage/unavailable` when fields are missing. |
| `/api/experimental/session/{id}/wait` (204) | V2 OpenAPI | Could replace 1s poll + idle grace loops | **Defer**: experimental, 204 has no payload, must be proven against retry/compaction. |
| `/api/session/{id}/interrupt?resume=true|false` | V2 OpenAPI | More controlled cancellation than old abort | **Defer** until A118's cancel ownership is resolved; session-wide interruption can still hit another owner's work. |
| Session `parentID` + automatic `<background-result kind="subagent">` to parent | V2 session create schema | Native child-session result delivery | **Do not adopt for gateway mesh delegation** in this refactor; gateway workers/tasks remain the ownership layer. Evaluate separately. |
| `fork`, `revert`, `diff`, `synthetic`, `inbox` routes | V2 OpenAPI | Native checkpoints/recovery/visibility | **Defer**; useful for A118 recovery design, not required for a protocol cutover. |
| `@opencode/client` / `@opencode/sdk` | V2 docs | Generated contract, fewer parser bugs | **Defer** a Node sidecar; revisit only if contract drift is proven recurring. |
| Native V2 compaction `keep.tokens`/`buffer`, `snapshots` | Migration doc | Config-level retention control | **Evaluate** with A118, not as a workaround for correlation. |
| V2 plugin API (`id` + `setup(ctx)`) | Plugin migration guide | Only relevant if custom hooks/tools are added | **Not needed** today; MCP is unchanged and V1 plugin code must not be assumed portable. |

---

## 5. Migration sequencing and gates

1. **A119 foundation (this audit + contract harness).** Produce a V2 probe/fake-server
   contract, a decision record for the table in §4, and a test matrix. No production
   flag, no code path changes, no paid turns.
2. **Protocol shim.** Add a single V2 transport/auth/message/event adapter behind a
   flag; translate to the carrier's existing `ExecutionResult`/`TurnControl` contract.
   Keep V1 adapter intact until the shim is green offline; do not run both against a
   live paid session.
3. **Correlation rewrite.** Replace `parentID` acceptance with prompt `id` + `metadata`
   + inbox/execution provenance. This is a Level 3 ownership change and needs A118's
   ruling before any live activation.
4. **Compaction / cancel / recovery.** Map `summarize → compact`, `abort → interrupt`,
   and decide whether `wait` is trustworthy. Re-certify late-capture, fencing, and the
   8-slot server bound with authenticated V2 servers.
5. **Cutover and retirement.** Operator-approved flag flip, read-only live checks, then
   natural turns. Only then delete V1 names (routes, event names, `parentID` lookup,
   dead env flags) under an `arch-contract` in the cutover job.

**Do not** combine steps 2-5 in one worker delivery. Cross-layer "green" on the adapter
does not prove end-to-end reply ownership.

---

## 6. Service-boundary checklist for the V2 adapter

Carry these forward; each must be re-checked against V2, not assumed:

- **Concurrency:** 8 resident servers/slots today. If switching to one managed service,
  bound concurrent prompt/inbox calls and re-measure memory/FDs; never restart a worker.
- **Memory at scale:** keep 8 MiB request/response caps and 256 KiB SSE frame/payload
  caps; V2 event volume is higher, so confirm no unbounded per-session buffers.
- **Request size:** cap prompt `text`, `files`, and `metadata` explicitly; opaque metadata
  must not become a secret smuggling channel.
- **Timeout:** keep `TurnControl` stall/hard-cap authority; read-only auth probes get a
  fixed short deadline. A V2 `409` is not a timeout and must not trigger resubmission.
- **Malformed input:** reject unknown event shapes early; never treat a synthetic
  marker, latest-by-time reply, or unauthenticated 200 as success.
- **Backing failure:** a 401 means misconfigured credentials (fail per request, do not
  spawn loops); a dead process or continuous refusal streak keeps the current
  restart/fencing policy but must be re-certified with Basic auth on the health probe.

---

## 7. Verification plan for the foundational job

Offline only; no paid CLI:

```bash
# Environment facts
opencode --version
opencode serve --help | head -20
opencode api get /api/info

# Contract probe (to be authored by A119): version, auth mode, route presence,
# event type inventory, config normalization. No model call.

# Targeted tests only — use the running-targeted-tests skill before choosing targets
.venv/bin/python -m pytest tests/test_opencode_managed_turns.py \
  tests/test_opencode_backend.py tests/test_opencode_server_integration.py -q
```

Never run the full suite or `pytest --run-e2e` to "verify" this work. The paid CLI cost
guard and the A102/A118 ownership contracts remain authoritative.

---

## 8. Seams verified vs unverified

| Verified in this checkout / locally | Not verified |
|---|---|
| V1 (v1.18.32) vs V2 (2.4.2 docs/OpenAPI) route and schema differences | Incident runtime, incident OpenCode version, A118 root cause |
| V2 auth behavior for managed service and freshly spawned `serve` | Gateway-managed V2 server behavior under production load |
| V2 event type inventory and vanished event names | Real V2 SSE stream during a long turn |
| V2 message/assistant schema and absence of `parentID` | End-to-end reply attribution with V2 |
| Current adapter code map, config/env wiring, retirement registry | Natural live turns, worker promotion, any paid e2e |

---

## 9. References

- <https://opencode.ai/docs/migrate-v1/>
- <https://opencode.ai/docs/api> · <https://opencode.ai/docs/openapi.json>
- <https://opencode.ai/docs/build/client/> · <https://opencode.ai/docs/build/sdk/>
- <https://opencode.ai/docs/compaction/> · <https://opencode.ai/docs/network/>
- <https://opencode.ai/docs/build/plugins/migrate-v1>
- <https://github.com/anomalyco/opencode/tree/v1.18.32> (`packages/sdk/openapi.json`)
- Repo: `src/backends/opencode.py`, `config/settings.py`, `src/worker/agent.py`,
  `src/core/turn_liveness.py`, `src/control/turn_queue.py`, `src/backends/registry.py`
- Jobs: A102 (backend unification), A118 (reply recovery, issue #298), A119 (foundation, issue #310)
