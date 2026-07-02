# AGENT 8 — Operator Signal Track

**Dispatch created:** 2026-07-03
**Author:** planning pass over `.ai/CONTEXT.md` + `.ai/NEXT_TASKS.md`
**Branch to cut:** `feat/operator-signal` off `main`
**Theme:** Close the two operator-facing blind spots that make the phone gateway
feel untrustworthy — *silent completion* and *invisible quota* — then unblock the
M1/M2 observability release. No new architecture; only wiring across existing seams.

> **Test cost guard (READ FIRST).** Normal test command is plain `pytest`.
> Tests must NOT invoke the paid Claude/Codex CLI. Never run the full e2e suite
> "to verify." Never run `python main.py status` (kills the live PM2 gateway).
> Check a running gateway with `curl http://127.0.0.1:9003/health`.

---

## Why these three, in this order

Ranked by impact on real state / UX / reliability, grounded in the actual code —
not the doc's aspiration.

### T1 — #21 Web Push notifications  (HIGH — ship first)

**Real value:** the product intent is "your coding agents, controllable from your
phone." Right now a completed or failed turn only reaches **Telegram**. If the
operator is looking at the Web UI (or it's backgrounded as a PWA), they get
**nothing** — they have to poll. That is the single biggest gap between the Web UI
and the Telegram surface.

**Why it's cheap and safe (verified in code):**
- `src/services/notification_service.py::notify_task_outcome` **already emits** a
  sanitized `task_notification` event with `status=success|failed`, `task_id`,
  `session_id`. This is the fan-out seam — a push send is one more best-effort
  channel call in that method, exactly like the docstring promises ("adding a
  second delivery channel means one more handler call").
- `web/public/sw.js` is a real service worker with `install`/`activate`/`fetch`
  but **no `push` / `notificationclick` handlers** — the missing 20%.
- DB is versioned; `_CURRENT_VERSION = 19`, migrations are a simple
  `(N, "SQL")` append in `src/control/db.py::_get_migrations`. Next is **20**.
- Control API routes are plain `@app.<verb>(...)` with `Depends(_require_auth)`
  in `src/control/control_api.py`.

**Scope guard:** notification fan-out ONLY. Do **not** wire it to approval gates
(#25 is a future workflow track the operator told us to leave inert). Never put
prompts, assistant output, file contents, command lines, or raw errors in a push
payload — only the sanitized success/failure fact + IDs + a session URL.

### T2 — #30/#33 Backend Account + Usage Visibility  (HIGH — ship second)

**Real value:** this project drives **paid** Claude/Codex accounts. Today the
operator has no place to see remaining quota; they discover exhaustion only when
a turn fails mid-work. That is a reliability gap, not a nicety. The System tab was
deliberately made infrastructure-focused (#38 done) and this is the acknowledged
missing operator surface.

**The discipline that makes this correct:** *do not invent quota data.* Surface
only what a backend can prove locally right now — active backend name, configured
model, last-observed usage/rate-limit fields from telemetry, account identity if
present. Everything unknown returns `null` **plus a coverage/reason field**, and
the UI renders "unknown" honestly. A convincing-but-fabricated quota number is
worse than a blank.

### T3 — #5–#9 LLM Turn Observability: gateway-routed mesh smoke  (MEDIUM — last)

**Real value:** this is the *one* recorded blocker to marking M1/M2 shipped and
scheduling the M3 Claude adapter. Per both docs, the 2026-07-02 mesh smoke passed
but `gateway_node_id` was **null** because it bypassed the gateway submit path.
It's validation, not surface — so it ranks below T1/T2 for user impact, but it
unblocks the whole observability roadmap and closes an explicit honesty gap
(telemetry that claims mesh coverage it didn't actually exercise through the
gateway).

**Scope guard:** do NOT redesign telemetry. Do NOT start M3. #8 benchmark stays
as recorded unless ingestion/query/projection code is touched. Success = one
gateway-routed mesh Codex smoke with non-null `gateway_node_id`, then update both
docs to mark M1/M2 shipped.

---

## Execution plan

### T1 — Web Push notifications

**Read before editing:** `docs/DEFERRED.md`, `docs/CONTROL_CONTRACT.md`
(notification section), `src/services/notification_service.py`,
`src/control/control_api.py`, `src/control/db.py` (`_get_migrations`,
`_CURRENT_VERSION`, a `list_*`/insert helper for the pattern), `config/settings.py`,
`web/public/sw.js`, `web/src/main.tsx`, `web/src/screens/SystemScreen.tsx`.

1. **DB (migration 20).** New `push_subscriptions` table: `endpoint` (unique),
   `p256dh_key`, `auth_key`, `enabled`, `created_at`, `updated_at`, `last_error`,
   optional coarse `label`. Append `(20, "CREATE TABLE ...")`, bump
   `_CURRENT_VERSION` to 20. Add `db.upsert_push_subscription`,
   `db.list_push_subscriptions(enabled_only=True)`, `db.disable_push_subscription`,
   `db.mark_push_error`. DB unit tests.
   > **[F6] Migration-number collision:** before appending, grep all branches
   > (`grep -rn "(20," src/control/db.py`, `git log --all -S "_CURRENT_VERSION"`)
   > to confirm 20 is unclaimed — Web-UI and main already collided on 13
   > (`_ensure_merged_schema` exists because of it). If 20 is taken, use the next
   > free number.
2. **Control API** (`control_api.py`, all `Depends(_require_auth)`):
   `POST /api/push/subscribe` (idempotent upsert), `POST /api/push/unsubscribe`
   (disable), `GET /api/push/status`. Validate payload shape, reject malformed
   subscriptions with a structured error envelope.
   > **[F4] Request-size cap has a real enforcement point:** FastAPI does NOT cap
   > body size by default. Inspect `Content-Length` and reject `> 4 KB` **before**
   > parsing (subscribe payloads are tiny). A Pydantic model alone is not a size
   > guard.
3. **VAPID config + web-push transport** via existing settings/env patterns
   (`config/settings.py` + `.env.example`). If keys absent → push reports
   **disabled**; **must not crash** the gateway.
   > **[F3] Name the transport — do NOT hand-roll RFC 8291 encryption.** Add the
   > `pywebpush` dependency to `pyproject.toml` as an **optional extra**; import it
   > lazily so a missing package/VAPID key = push disabled (same rule as absent
   > keys). If you choose not to add the dep in this pass, scope T1 to
   > store-subscriptions + wire-the-seam + SW-handlers and gate the actual
   > encrypted send behind a feature flag — but state which you did.
4. **Delivery helper** used by `notify_task_outcome`. Fan out ONLY the sanitized
   `task_notification` fact: title, short body, task/session IDs, session URL when
   known. A failed/expired subscription must be marked (410 → disable).
   > **[F1] Push is independent of Telegram.** The push fan-out must sit on the
   > **unconditional** path next to `emit_event(...)`, NOT nested under the
   > `if chat_id and tg:` guard — Web-only sessions have no `chat_id` and are
   > exactly the ones that need push. (All three call sites
   > `orchestrator.py:525/1069/1882` route through this method — verified.)
   > **[F2] Never block completion — specify the mechanism.** `notify_task_outcome`
   > is `await`ed inline on the completion path, so a naive `await push_all()`
   > blocks for (timeout × N). Use fire-and-forget:
   > `asyncio.create_task(_push_fanout(...))` where `_push_fanout` bounds
   > concurrency with a `Semaphore` and wraps each send in `asyncio.wait_for`
   > (short timeout). The background task MUST swallow its own exceptions
   > (codebase rule: never raise into the caller). Bound payload size.
5. **Service worker** (`sw.js`): add `push` (show notification) and
   `notificationclick` (open/focus session URL) handlers. Keep the existing
   offline-shell and API-network-only behavior untouched.
6. **Frontend** (`SystemScreen.tsx` + adapter): quiet settings control showing
   permission/subscription state. Request browser permission **only on a user
   click** — no auto-prompt on load.

**Verify:** targeted backend tests (DB / API validation / delivery boundary with
an injected fake sender — no real web-push network), frontend adapter/UI-state
tests where practical, `cd web && npx tsc -b`, focused vitest. Manual browser
smoke on localhost with a fake sender path.

**Service-boundary checklist (must all hold):** capped fanout concurrency; short
per-send network timeout; bounded subscribe JSON; malformed input rejected before
DB write; if DB or VAPID unavailable → report disabled and keep task
execution + Telegram working; N=100 subscribers stays small and predictable.

### T2 — Backend Account + Usage Visibility

**Read before editing:** `src/backends/registry.py`, `src/backends/codex.py`,
`src/backends/claude_driver.py`, telemetry projection/store
(`src/control/telemetry_store.py`, `telemetry_sink.py`),
`web/src/components/timeline/SessionTurns.tsx`, `web/src/screens/SystemScreen.tsx`,
and the raw API adapter layer.

1. **Inventory** what each backend can prove locally *today*: active backend name,
   configured/selected model, known account identity if available, last-observed
   usage / rate-limit fields from telemetry, and explicit unknown-coverage reasons.
2. **Read-only endpoint** `GET /api/backends/usage` aggregating registry/config/
   telemetry facts only, via **bounded/O(1)-ish reads** (no full telemetry scan).
   Unknown daily/weekly limits, reset times, identities → `null` +
   `coverage`/`reason` fields.
   > **[F5] Honesty rule, inline:** observed usage counters are **not** limits.
   > Never *derive* a limit the provider didn't explicitly state. A blank is
   > correct; a fabricated number is a bug.
3. **Frontend:** a compact System section (or a dedicated Usage/Limits screen)
   consistent with the infra-focused System page. Render known vs. unknown
   explicitly; avoid noisy cards.

**Verify:** API-shape tests with missing data; known Codex telemetry rate-limit
fields; Claude usage-limit-error extraction where already parsed; frontend
adapter/UI rendering of known vs. unknown. `cd web && npx tsc -b`, focused vitest.

### T3 — Gateway-routed mesh Codex smoke (close #9)

**Read:** `docs/LLM_TURN_OBSERVABILITY_SPEC.md` handoff, and the 2026-07-02
validation logs in `.ai/CONTEXT.md` / `.ai/NEXT_TASKS.md`.

1. Run one controlled mesh Codex smoke **through the production controller/gateway
   submit path** (not a bypass), so events carry a non-null `gateway_node_id`.
2. Inspect graph / diagnostics / events / timeline; privacy-scan all `llm_%`
   tables, `logs/telemetry_spool`, and API JSON for fresh sentinel strings.
3. Clean up temp nodes/ports; mark the temp node offline via
   `MeshDB.mark_node_offline`.
4. Only after the smoke passes with `gateway_node_id` populated: update both docs
   to mark M1/M2 shipped and note M3 is now schedulable. Do **not** start M3 here.

---

## Sequencing & guardrails

- Land T1 → T2 → T3 as separate commits/PRs on `feat/operator-signal`. T1 and T2
  are independent; T3 depends on nothing here but is scheduled last by priority.
- No change to `MESH_ENABLED=false` behavior. No approval-gate wiring. No
  invented quota/provider data. No prompt/output/file/command content in any push.
- Every rung ends green: backend `pytest` targeted, `cd web && npx tsc -b`,
  focused vitest.

---

## Implementation log

### T1 — Web Push notifications — SHIPPED (2026-07-03)

Delivered on this pass. Files:
- **DB:** `src/control/db.py` migration **20** (`push_subscriptions`, endpoint PK),
  `_CURRENT_VERSION=20`, helpers `upsert_push_subscription` /
  `list_push_subscriptions` / `disable_push_subscription` / `mark_push_error`.
  (Confirmed 20 was unclaimed per **[F6]**.)
- **Config:** `config/settings.py` `PushConfig` (VAPID keys/subject + fanout
  concurrency, per-send timeout, max subscribe bytes) wired from
  `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT`. `configured`
  property gates availability.
- **Transport [F3]:** `pywebpush` added as an **optional** extra in
  `pyproject.toml` (`pip install -e ".[push]"`), imported lazily. Absent
  package OR absent VAPID ⇒ push reports disabled; gateway never crashes.
- **Service:** `src/services/push_service.py` `PushService.fanout` — bounded by
  `Semaphore` + per-send `asyncio.wait_for`; 410/404 ⇒ disable, transient ⇒
  `mark_push_error`; never raises. `build_task_payload` bounds title/body and
  emits ONLY `{title, body, task_id, session_id, url}` — no prompt/output/files.
- **Notification wiring [F1][F2]:** `notification_service._maybe_push_outcome`
  runs on the **unconditional** path (independent of `chat_id`/Telegram) and
  fires the fan-out via `loop.create_task(...)` — never blocks completion.
- **API [F4]:** `POST /api/push/subscribe` (Content-Length capped at 4 KB before
  parse; malformed ⇒ 422), `POST /api/push/unsubscribe`, `GET /api/push/status`.
- **SW [F7]:** `web/public/sw.js` cache bumped `v2→v3`; added `push` +
  `notificationclick` (focus existing tab, else open) handlers.
- **Frontend [F8]:** `usePushNotifications` (permission requested only on click),
  `PushSetting` quiet System→Settings control (hidden when unavailable),
  `apiClient` push methods. Session URL = `/sessions/:id`, fallback `/`.

**Verification:** `tests/test_push_notifications.py` (15 tests: DB idempotency/
re-enable/disable, payload bound+whitelist, unavailable-noop, fanout
gone→disable + timeout-bound, API auth/malformed/oversize/status) — pass.
`test_control_api_write.py`+`test_control_api.py` unaffected (66 total pass).
`cd web && npx tsc -b` clean; `vite build` green (dist/sw.js carries both
handlers); 62 vitest pass.

**Operator follow-ups (not code):**
1. Generate a VAPID keypair and set `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`,
   `VAPID_SUBJECT` (a `mailto:` or `https:` contact). Until set, the UI shows no
   push control and `/api/push/status.available=false` — by design.
2. `pip install -e ".[push]"` on the gateway host for the send transport.
3. `.env.example` could not be documented from this environment (harness blocks
   reading/writing env files); add the three VAPID vars there manually.

### T2 — Backend Account + Usage Visibility — SHIPPED (2026-07-03)

Delivered as an **honesty-first** read model. Files:
- **Service:** `src/services/backend_usage.py::build_backend_usage` — aggregates
  ONLY provable facts: configured/default model (config), observed models +
  recent token usage summed from `TelemetryStore.list_turns(backend=...)`
  (bounded, O(#backends) queries). Limits/reset/identity are ALWAYS `null` +
  a machine reason (`no_backend_limit_source` / `no_backend_identity_source`)
  because no backend emits them. Usage absent ⇒ `null`, never a fabricated 0.
  Coverage states: `observed` / `no_data` / `usage_fields_absent` /
  `telemetry_unavailable`. Survives `list_turns` failure.
- **API:** `GET /api/backends/usage` (auth-gated) over registry + config +
  telemetry only.
- **Frontend:** `BackendUsagePanel` in System (hidden on error/empty),
  `apiClient.backendsUsage` + typed `BackendUsageResponse`. Renders model +
  observed token count + an explicit "Limits & quota: unknown" line; a footnote
  states token counts are observed usage, not a quota.

**Honesty audit (self-review):** no code path derives a limit from usage; every
unknown is null + reason; the UI never promises provider-specific quota. The
telemetry read is bounded (≤200 turns × #backends). Verified.

**Verification:** `tests/test_backend_usage.py` (8: no-telemetry facts, always-null
limits/identity, usage summation, null-not-zero, usage-fields-absent, list_turns
failure survived, API auth + shape). 77 backend tests green together; `tsc -b`
clean; 62 vitest pass.

### T3 — gateway-routed mesh smoke — NOT started

Remains open per the ranking (validation-debt, lowest user impact). Needs a live
gateway-routed mesh Codex run with non-null `gateway_node_id`; deferred to a
session with the mesh available.
