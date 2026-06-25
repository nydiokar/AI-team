# Quota Window Coordinator Specification

Status: proposal only - no implementation in this document
Owner: Nyd
Date: 2026-06-23
Source review:
- `docs/README.md`
- `docs/CONTROL_CONTRACT.md`
- `docs/BACKEND_HOOKS_STRATEGY.md`
- `src/core/interfaces.py`
- `src/services/session_service.py`
- `src/backends/{claude_code,codex,opencode}.py`
- OpenAI Codex manual fetched 2026-06-23
- Claude Code status line, CLI reference, and cost docs reviewed 2026-06-23

---

## 0. Executive Summary

The product should not be a blind "window warmer."

The correct product is a **quota window coordinator**:

1. Observe provider quota state from authoritative telemetry.
2. Classify each quota bucket's reset semantics experimentally.
3. Align optional activation with likely user work periods.
4. Activate only when the bucket is proven to be first-use anchored and policy permits the exact automation method.

Synthetic activation is a subordinate capability, not the default behavior.

Automatic activation must remain disabled unless all of these are true:

- `window_semantics == ANCHORED`
- `telemetry_quality == AUTHORITATIVE`
- `automation_policy == ALLOWED`
- `adapter_version == VALIDATED`
- `cost_budget_remaining == true`
- no active user session is detected

Any uncertainty forces observe-only mode.

---

## 1. Product Intent

The user wants to avoid starting a fresh multi-hour coding-agent quota window at the moment they begin serious work.

If a provider's quota window is anchored by the first chargeable interaction, then a tiny authorized activation before the user starts work can make the next reset arrive sooner during the real work session.

Example:

- Bad timing: user starts work at 10:00, opens a fresh 5-hour window, exhausts quota by 11:00, waits until 15:00.
- Better timing: coordinator activates an anchored window at 06:30, user starts at 10:00, exhausts quota by 11:00, reset arrives around 11:30.

This only works for quota systems whose reset window is actually anchored by first use. It has no value for fixed schedules, sliding windows, token buckets, or windows already active from other usage.

---

## 2. Hard Premise

The core premise is unverified by default:

> A five-hour reset timestamp does not prove a first-use anchored five-hour window.

The same visible fields can be produced by several algorithms:

- fixed schedule;
- anchored/tumbling window;
- sliding/rolling window;
- token bucket or leaky bucket;
- dynamic provider policy.

Therefore, the system must measure semantics per provider quota bucket before enabling activation.

Do not build scheduling around `last_success + 5h`.
Do not send prompts every five hours around the clock.
Do not infer provider behavior from terminal error wording when telemetry exists.

---

## 3. Modes

### OBSERVE_ONLY

Default mode. The coordinator reads provider quota state and records:

- bucket identity;
- usage percentage;
- reset timestamp;
- limit reached state;
- window duration if exposed;
- active user-session state;
- changes across time.

No synthetic model requests are sent.

### MANUAL_ACTIVATE

The operator can request one isolated activation for a specific bucket. The coordinator still enforces safety checks, cost limits, version validation, and idempotency.

This mode is useful during experimental classification and is lower risk than unattended activation.

### AUTO_ACTIVATE

The coordinator schedules synthetic activation automatically, but only for buckets that have passed classification and policy gates.

AUTO_ACTIVATE is never enabled globally by accident. It is per provider bucket.

---

## 4. Definitions

**Provider:** The service that owns the quota. Examples: OpenAI/Codex, Anthropic/Claude, or the specific provider behind OpenCode.

**Quota bucket:** The provider-defined limit unit. The key is not just backend or model. It must be the provider's bucket identifier when available.

**Principal:** The account, workspace, credential, or subscription identity whose quota is being observed.

**Quota snapshot:** One point-in-time read of bucket telemetry.

**Activation:** One synthetic, isolated, noninteractive model request whose only purpose is to start a proven anchored window.

**Window semantics:** The experimentally classified reset behavior of a bucket: `ANCHORED`, `FIXED`, `SLIDING`, `TOKEN_BUCKET`, or `UNKNOWN`.

**Work horizon:** A user-configured likely work interval, such as weekdays 09:00-19:00, used to avoid wasting quota outside useful hours.

---

## 5. Non-Goals

- No rate-limit circumvention.
- No account rotation.
- No credential sharing.
- No scraping of private web UIs.
- No unofficial OAuth-token reuse.
- No interactive terminal automation through keystrokes.
- No activation inside user repositories.
- No activation inside user conversations.
- No hidden productive work.
- No continuous keepalive prompts.
- No OpenCode-level quota abstraction; quotas belong to providers.

---

## 6. Provider Findings

### Codex With ChatGPT Authentication

Codex supports several official automation paths:

- `codex exec` for noninteractive scripted use;
- `--ephemeral` to avoid persisted session rollout files;
- `--sandbox read-only` for least-permission execution;
- `--ignore-user-config` and `--ignore-rules` for controlled automation;
- `--skip-git-repo-check` for safe non-repo directories;
- Codex access tokens for trusted Business/Enterprise automation;
- API-key authentication for usage-based automation.

OpenAI documentation says API keys are the right default for automation when they work, while ChatGPT-managed Codex access tokens exist for trusted workflows that specifically need ChatGPT workspace identity or Codex entitlements.

Policy risk remains for subscription-window activation: OpenAI terms prohibit circumventing rate limits or restrictions. A feature marketed as manipulating reset timing must stay behind a policy gate unless OpenAI explicitly confirms the use case.

Important verification gap:

- The research claims `account/rateLimits/read` returns `rateLimitsByLimitId`, `limitId`, `usedPercent`, `windowDurationMins`, `resetsAt`, and `rateLimitReachedType`.
- This is the right architecture if present, but it was not found in the public Codex manual during this review. Treat it as an app-server/schema feature that must be validated from the installed Codex version before implementation.

Correct Codex stance:

- Observation is safe when it uses documented or locally verified telemetry.
- Manual activation can be used for classification.
- Auto activation of subscription buckets requires policy approval and proven bucket semantics.
- API-key mode is usage-based and should usually not participate in subscription-window optimization.

### Claude Code With A Claude Subscription

Claude Code status lines expose:

- `rate_limits.five_hour.used_percentage`
- `rate_limits.five_hour.resets_at`
- `rate_limits.seven_day.used_percentage`
- `rate_limits.seven_day.resets_at`

The fields may be absent and appear only for Claude.ai subscribers after the first API response in the session. The status line command itself does not consume API tokens.

Claude Code also supports official noninteractive usage through `claude -p`, and the CLI has flags useful for isolation:

- `--print`, `-p`
- `--output-format`
- `--max-turns`
- `--max-budget-usd`
- `--no-session-persistence`
- `--safe-mode`
- `--bare`
- `--setting-sources`

Policy constraint:

- Anthropic consumer terms prohibit automated or non-human access except through an API key or where explicitly permitted.
- Therefore the adapter must use only official Claude CLI or Agent SDK paths and must reject unofficial browser/OAuth automation.

Correct Claude stance:

- Observe via status-line telemetry where available.
- Use `claude -p` only when the installed version, auth mode, and terms allow it.
- Validate whether the run uses subscription limits, API billing, or separate credits.
- Do not treat a visible reset timestamp as proof of first-use anchoring.

### OpenCode

OpenCode is not itself a quota owner in the general case. It is a client/router over many providers.

Incorrect bucket key:

```text
opencode/default
```

Correct bucket key:

```text
provider + credential/principal identity + provider quota bucket
```

OpenCode may be used as an execution harness only when the selected provider adapter can observe and classify the provider's quota. It should not be treated as a universal warmable backend.

OpenCode Zen, OpenCode Go, or any provider behind OpenCode must each be classified from their own telemetry and policy.


---

## 6A. Policy And Detection Posture

The coordinator must be transparent automation, not stealth automation.

The goal is to avoid provider false positives caused by brittle, clock-exact behavior, not to bypass provider policy, hide automation, or defeat abuse detection. Timing variation is allowed only for normal scheduler hygiene:

- avoid every configured bucket firing at exactly the same second;
- avoid duplicate activation after process restart, sleep/resume, or clock drift;
- avoid synchronized retries during provider incidents;
- align activation with a user-declared work horizon.

Timing variation must not be used to disguise automation. Do not tune randomization to evade provider detection, do not imitate human typing cadence, do not create fake conversational behavior, and do not spread requests across accounts, IPs, machines, or credentials.

Preferred detection-safe posture:

- use official CLI, SDK, or access-token mechanisms;
- identify the integration honestly where the protocol supports client metadata;
- keep activation prompts deterministic and boring;
- keep activation frequency low and explainable;
- log every activation locally with reason codes;
- provide an operator-visible disable switch;
- stop immediately on provider warnings, policy errors, or ambiguous limit behavior.

A provider should be able to inspect the traffic and conclude: this is a user-authorized scheduler making sparse, non-mutating requests through official automation surfaces. If a proposed change would make that explanation harder, reject it.

---

## 7. Window Classification Protocol

Automatic activation must remain disabled until each bucket passes this test.

At a point where the provider reports zero usage or freshly reset usage:

1. Read quota telemetry without sending a model request.
2. Record `used_percent`, `resets_at`, `window_duration`, and bucket id.
3. Wait a controlled interval if needed.
4. Send exactly one isolated minimal request at `T1`.
5. Read telemetry immediately after.
6. Send a second isolated minimal request at `T2 = T1 + 30-60 minutes`.
7. Read telemetry again.
8. Repeat across at least three windows.

Interpretation:

| Observed behavior | Classification | Activation value |
|---|---|---|
| no reset timestamp before first use; after first use `resets_at ~= T1 + window`; second request does not move it | `ANCHORED` | valuable |
| reset timestamp exists before use and remains unchanged | `FIXED` | none |
| reset timestamp moves after later requests or usage expires incrementally | `SLIDING` | little or none |
| reset timing changes by replenishment rate rather than one boundary | `TOKEN_BUCKET` | none |
| behavior varies across cycles | `UNKNOWN` | disabled |

The second request is mandatory. A single post-request timestamp cannot distinguish an anchored window from some rolling implementations.

---

## 8. Adapter Contract

Add provider-specific quota adapters. The scheduler must never parse provider terminal output directly.

```python
class QuotaAdapter:
    def identify_principal(self) -> str: ...
    def discover_buckets(self) -> list["QuotaBucket"]: ...
    def observe(self, bucket_id: str) -> "QuotaSnapshot": ...
    def activate(self, bucket_id: str) -> "ActivationResult": ...
    def classify_limit_error(self, error: Exception) -> "LimitSignal": ...
    def detect_active_user_session(self) -> bool: ...
```

Provider parsing belongs inside versioned adapters. Any schema or CLI version change can disable the adapter until revalidated.

---

## 9. Persistent State

Persist state in a dedicated quota file first:

```text
state/quota_windows.json
```

Fields per bucket:

```text
provider
authentication_mode
principal_hash
workspace_or_account_hash
bucket_id
bucket_name
window_duration_seconds
window_semantics
semantics_confidence
used_percent
observed_reset_at
last_observed_at
last_activation_attempt_at
last_successful_activation_at
activation_idempotency_key
activation_cost_delta
next_activation_at
consecutive_failures
backoff_until
last_limit_error
adapter_version
policy_mode
```

Do not persist:

- raw access tokens;
- prompt contents;
- user account IDs;
- repository paths;
- full stderr/stdout;
- conversation transcripts.

Hashes must be stable enough for local state continuity but not reversible without local secrets.


---

## 9A. Principal Identity And Cross-Node Dedupe

The coordinator must be account-aware, but it must not assume providers expose a username or account id in every telemetry path.

The adapter must produce a `principal_hash` before any activation is allowed. The hash represents the quota-owning identity, not the local gateway session. It should be derived in this priority order:

1. Provider-exposed stable workspace/account/user id from an official telemetry or auth-status API.
2. Provider-exposed workspace id plus authentication mode.
3. Operator-configured principal key shared across nodes, for example `codex-personal-main` or `claude-max-nyd`.
4. Local credential fingerprint only when it can be derived without persisting or logging raw secrets.

If none of these is available, the adapter may still run in `OBSERVE_ONLY`, but `MANUAL_ACTIVATE` and `AUTO_ACTIVATE` are disabled with reason `principal_unknown`.

Do not persist raw usernames, emails, account ids, access tokens, refresh tokens, or auth file contents. Persist only `principal_hash` and a human-safe local label such as `principal_label="codex personal"` when the operator configured it.

Cross-node activation requires a shared coordination record. A local JSON file is not enough when multiple nodes are online. The activation lock key is:

```text
provider + authentication_mode + principal_hash + bucket_id + observed_reset_at/window_id
```

The shared lock must include:

- lock owner node id;
- acquired timestamp;
- lease expiry;
- activation idempotency key;
- observed reset timestamp or provider window id;
- activation status: `planned`, `running`, `succeeded`, `failed`, `skipped`.

Rules:

- A node must observe current telemetry before acquiring the lock.
- A node must acquire the shared lock before activation.
- If another node already succeeded for the same lock key, skip activation.
- If another node is running and the lease is fresh, skip activation.
- If a lease expires, another node may take over only after re-observing telemetry.
- A completed activation remains idempotent for the observed provider window.
- If the shared store is unavailable, AUTO_ACTIVATE is disabled; do not fall back to per-node activation.

For this repository, the natural shared store is the mesh DB when mesh is enabled. Single-node deployments may use `state/quota_windows.json`, but multi-node deployments must use a DB-backed lock or equivalent shared coordinator.

Provider notes:

- Codex may expose workspace/auth identity through official auth or app-server surfaces, but the exact fields must be validated against the installed version.
- Claude Code status-line rate limit data exposes quota percentages and reset timestamps, not necessarily account identity. `claude auth status --json` may help identify auth mode, but any stable identity fields must be validated from the installed version.
- If provider telemetry has quota bucket ids but no account id, require an operator-configured principal key shared across all nodes using that account.

---

## 10. Scheduler Rules

The scheduler is telemetry-driven.

Rules:

- Observe before acting.
- Schedule from provider reset timestamps, not local five-hour assumptions.
- Activate at most once per bucket per observed window.
- Never activate while a real user session is active.
- Never activate `UNKNOWN`, `FIXED`, `SLIDING`, or `TOKEN_BUCKET` windows.
- Never activate during quiet hours unless explicitly configured.
- Stop after one failed model request.
- Respect `Retry-After`, `resetsAt`, and provider backoff data.
- Apply bounded jitter around scheduled activation for scheduler hygiene, not concealment.
- Enforce daily and weekly activation budgets.
- Open a circuit after ambiguous responses or unexplained cost spikes.

Work horizons are required for AUTO_ACTIVATE. Example:

```yaml
weekdays:
  likely_work_start: "09:00"
  likely_work_end: "19:00"
  desired_lead: "2h30m"
```

This schedules an activation near 06:30 for a 09:00 work start, with a small bounded jitter window for operational hygiene. It must not burn one synthetic prompt every five hours around the clock, and it must not randomize timing to imitate human behavior.

---

## 11. Active-Session Protection

Before activation, acquire a provider-specific global lock and verify:

- no foreground CLI process with recent interaction;
- no active Codex/Claude task;
- no provider lock already held;
- no response currently streaming;
- no model request recorded in the previous safety interval;
- no user activity detected in the target client;
- no active gateway session using the same provider bucket.

If any condition is true, skip activation. Real usage either already started the window or may be about to start it naturally.

---

## 12. Activation Environment

Activation must run in an isolated environment:

- empty temporary directory;
- read-only sandbox;
- no project workspace;
- no user repository;
- no project instructions;
- no MCP servers;
- no tools where configurable;
- no shell execution where configurable;
- no persistent conversation;
- bounded process timeout;
- bounded output;
- one model turn maximum.

Recommended Codex command shape after version validation:

```powershell
codex exec `
  --ephemeral `
  --skip-git-repo-check `
  --sandbox read-only `
  --ignore-user-config `
  --ignore-rules `
  --model <validated-cheapest-model-for-target-bucket> `
  "Return only: 0. Do not use tools."
```

Recommended Claude command shape after version and auth validation:

```powershell
claude -p `
  --no-session-persistence `
  --max-turns 1 `
  --max-budget-usd <small-budget> `
  --output-format json `
  "Return only: 0. Do not use tools."
```

The exact command is adapter-owned. The coordinator only asks the adapter to activate a specific bucket.

---

## 13. Cost Controls

Prompt length is not a sufficient cost guarantee. Coding agents may load:

- system instructions;
- account configuration;
- MCP schemas;
- project rules;
- repository metadata;
- tool definitions;
- cached context.

Measure activation cost by authoritative telemetry delta:

```text
activation_cost = used_percent_after - used_percent_before
```

Reject an activation strategy when:

- activation cost exceeds threshold;
- output is not bounded;
- more than one model turn occurs;
- tools are invoked;
- files are modified;
- reset timestamp does not behave as expected.

After two unexplained cost spikes, open the circuit and require manual revalidation.

---

## 14. Observability Events

Emit through the existing event envelope.

| Event | Meaning |
|---|---|
| `quota.observed` | telemetry snapshot recorded |
| `window.classified` | bucket semantics classified or changed |
| `activation.scheduled` | next activation scheduled |
| `activation.skipped_existing_window` | current active window makes activation unnecessary |
| `activation.skipped_active_session` | real user activity blocked activation |
| `activation.started` | isolated activation began |
| `activation.succeeded` | activation completed and telemetry matched expectations |
| `activation.failed` | activation failed without policy/version disable |
| `activation.cost_exceeded` | activation cost exceeded threshold |
| `limit.reached` | provider limit reached signal observed |
| `window.reset_detected` | provider reset observed |
| `adapter.disabled_policy` | adapter disabled by policy gate |
| `adapter.disabled_version` | adapter disabled by schema/CLI version gate |
| `circuit.opened` | profile disabled until manual review |

Events include:

- provider;
- bucket id;
- bucket name;
- principal hash;
- used percentage;
- reset timestamp;
- window semantics;
- reason code.

Events must not include:

- credentials;
- prompt contents;
- raw account IDs;
- repository paths;
- full provider output.

---

## 15. Integration With This Gateway

Add a new service:

```text
src/services/quota_window_coordinator.py
```

Responsibilities:

- load/save quota state;
- call provider quota adapters;
- run classification protocol;
- schedule eligible activations;
- emit observability events;
- expose read-only status.

It must not:

- construct backend adapters directly;
- write Telegram state;
- mutate user sessions;
- dispatch through ordinary task flow;
- parse provider terminal output outside adapter boundaries.

Add a disabled-by-default lifecycle task in `TaskOrchestrator.start()` only after config, backends, and `SessionService` exist.

Add read-only Control API status later:

```text
GET /api/quota-windows
```

Optional commands later:

```text
POST /api/quota-windows/{bucket}/observe
POST /api/quota-windows/{bucket}/classify-step
POST /api/quota-windows/{bucket}/manual-activate
POST /api/quota-windows/{bucket}/enable-auto
POST /api/quota-windows/{bucket}/disable-auto
```

Write endpoints must return `{ok, reason}` machine codes and must not embed user-facing prose in the protocol.

---

## 16. Acceptance Criteria

The feature is ready only when all of these hold:

- The backend's window semantics have been reproduced across three consecutive cycles.
- Activation produces the expected reset timestamp.
- A later interaction does not unexpectedly move the timestamp.
- Activation consumes no more than the configured quota threshold.
- Exactly one model turn is generated.
- No tools are invoked unless the adapter explicitly proves harmless no-op behavior.
- No filesystem mutation occurs.
- No user project, conversation, or active session is touched.
- Duplicate scheduler executions remain idempotent.
- Restarting the service preserves the correct next action.
- Clock changes, sleep/resume, and timezone changes do not duplicate activation.
- Provider outage creates backoff rather than retries.
- Schema or CLI version change disables the adapter.
- Unknown principal identity disables activation.
- Shared-store failure disables AUTO_ACTIVATE in multi-node deployments.
- Unsupported authentication modes are rejected.
- Sliding, fixed, token-bucket, or unknown windows remain observe-only.
- Provider automation terms have been reviewed for the exact authentication method.

---

## 17. Implementation Order

1. Rename internal concept to quota window coordinator.
2. Add state model and read/write tests.
3. Add adapter interfaces and fake adapter tests.
4. Implement observe-only Codex adapter using locally verified app-server or CLI telemetry.
5. Implement observe-only Claude adapter using status-line or supported CLI telemetry.
6. Add read-only Control API status.
7. Add manual activation for one provider behind policy/version gates.
8. Run the classification protocol across at least three cycles.
9. Add AUTO_ACTIVATE only for a classified anchored bucket.
10. Add work-horizon scheduling.

---

## 18. Critical Decisions

- Observation is the product baseline.
- Activation is optional and exceptional.
- Provider bucket id is the scheduling key.
- Provider telemetry beats inference.
- Local five-hour timers are fallback diagnostics, not scheduler truth.
- OpenCode is a harness, not a quota provider.
- Work horizons prevent waste.
- Any ambiguity disables automation.



