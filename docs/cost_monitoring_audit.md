# Cost-monitoring truthfulness audit (A65 Phase 0)

**Authored:** 2026-08-03 · **Owner:** A65 `COST_MONITORING_VISIBILITY` · **Status:** current (gates
the A65 read-model + Cost dashboard)

This is the Phase-0 gate of [`AGENT_65_COST_MONITORING_VISIBILITY.md`](../.ai/dispatch/AGENT_65_COST_MONITORING_VISIBILITY.md):
before any projection/UI is built, we validate the telemetry the cost view will be built on. Every
number below was re-derived from `state/mesh.db` (read-only) on 2026-08-03 — the same way the
"manager vs workers" report was produced. Sources: `llm_model_requests`, `llm_turns`, `sessions`,
`flow_runs`, `flow_links`.

---

## 1. Data provenance — is this real usage?

**Yes for Claude; yes for Codex too, but the numbers need a semantics correction.**

| usage_source | rows | what it is |
|---|---|---|
| `claude.result.usage` | 983 | Real per-request usage from the Claude SDK `result` messages (M3 telemetry). Cache is reported **separately** (`cache_read_input_tokens`), **exclusive** semantics: `input_tokens` = uncached input. |
| `codex.rollout.token_count.last_token_usage` | 2075 | Real Codex CLI per-turn usage. **Inclusive** semantics: `input_tokens` **already includes** the cached portion (adapter flags `input_token_semantics="includes_cache"`). |
| `turn.completed.usage` | 85 | Turn-level completed usage. |

- Claude cost is already **real usage**, and cache reads are billed at their real discounted rate
  (`pricing.py` cache-read = 0.10× input). Cache reads ARE counted — at the correct discounted
  price, not zero and not full price.
- Codex totals are **inflated by double-counting the cached portion** (see §3).

## 2. Standalone sessions — "orphan runs" producing millions of tokens

**Yes, real — but it's the *standalone sessions*, not orphan flow runs.**

- **91% of all token volume (4.97B of 5.45B tokens) comes from sessions with no case affiliation**
  (`case_role IS NULL`) — i.e. ordinary interactive sessions (Web/Telegram) that were never part of
  a Manager case. These dominate every ranking:
  - `3549863198d4` — codex `gpt-5.6-terra`, **885M tokens**, tokens_ingest repo
  - `338460eb3d04` — codex (empty model), 217M, /home/cifran/dev/observer
  - `c905e1b377e4` — codex `gpt-5.5`, 210M, tokens_ingest
  - (…top 15 are all `role=none`; several claude `opus` sessions at 50-80M each)
- **Orphan flow runs** (a `flow_runs` row with no session link AND no task link): 64, but they are
  **not** the burners — all from 2026-07-08 (M2 substrate experiments), all `cancelled`, 1 turn /
  0-2 requests each. Negligible tokens.
- The cost view must therefore surface **unaffiliated sessions as a first-class dimension** — they
  are ~91% of spend, and invisible to any case-centric view.

## 3. Codex double-count bug (real, hits the biggest sessions)

All `gpt-*` rows carry `input_token_semantics="includes_cache"` **and** a separate
`cache_read_tokens` (e.g. gpt-5.6-terra: 458M input + 451M cache_read). Because input already
includes the cache, `input + cache_read` double-counts the cached portion. The current aggregation
(`db.get_session_token_totals`) sums all four columns blindly, so **every codex session total is
inflated by its cached-input overlap** — and the top 3 burners are all codex. Fix (Phase 1 read-model):
for `includes_cache` rows compute `uncached_input = input_tokens - cache_read_tokens`, then bill
`uncached_input` + `cache_read` at the discounted rate + output. Never sum input+cache_read for these.

## 4. Unpriced usage — what "unpriced" means and how much there is

`pricing.py` only prices Claude families (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5 per MTok). Any
model without an entry yields `cost.known=false` + a reason — never a fabricated number.

| bucket | tokens (M) | share | reason returned |
|---|---|---|---|
| priceable claude (opus/sonnet/haiku) | 2,678 | 49% | priced |
| codex/gpt (all `gpt-*`, fable) | 1,581 | 29% | `unknown_model_pricing` |
| empty-model rows (claude `aggregate_only` + codex) | 1,189 | 22% | `no_model` |

**~51% of all token volume is currently unpriceable.** Two sub-causes:
- **codex/gpt:** no price entries at all.
- **empty-model:** 780 rows carried usage but no model name — 278 are claude
  `claude.result.usage / aggregate_only` statusline events (667M cache_read + 0.5M input = real
  claude usage we cannot price), the rest codex events with no model.

**"Honest pricing + coverage %"** = two promises the dashboard will keep: (1) never fabricate a
price — unpriceable models show `cost unavailable + reason`; (2) always show **coverage** — the
share of usage that IS priceable, so the reported total is read as a floor, not the truth. Today
coverage is ~49% by tokens; the single biggest session in the DB is unpriceable.

## 5. Attribution gaps (small, but must be explicit)

- 26 `llm_turns` reference a `session_id` absent from `sessions`; 14 turns have no `session_id`.
- Total unattributable tokens: **11M** (≈0.2%) — not a material cost error, but the read-model
  needs an explicit `unattributed` bucket so it doesn't silently vanish.
- 366 turns have **no per-request rows at all**; ~276 of those carry aggregate token metrics
  (`metrics_json`) that are NOT in any current sum → totals slightly **under-count**.
- `is_duplicate` is currently `0` everywhere (the dedup filter is a no-op safety guard — keep it).

## 6. `total`-definition inconsistency (live truthfulness bug)

`db.get_session_token_totals` sets `total = input + output` (cache excluded) while
`pricing.TokenTotals.total` sums **all four** buckets. The Web UI's "Total tokens" and its own
cost estimate can therefore disagree. Phase 1 must define ONE contract (recommended: `total` = all
four buckets; `billable` = the USD number from `estimate_cost`, which already prices cache reads at
0.10× and writes at 1.25×).

## 7. Per-session cost must be per-request-model, not `sessions.model`

The requirement "by session, stating the model" has a data wrinkle: `sessions.model` is often empty
and a session **mixes models** (e.g. 321 `gpt-5.5` requests ran inside `gpt-5.6-sol` sessions; claude
sessions run opus and sonnet). Per-session cost must therefore aggregate **request-level `mr.model`**
and label a session by its dominant priced model (or per-model sub-buckets) — never `sessions.model`
alone.

## 8. What this gates (Phase 1 decisions)

1. Read-model sums per-request buckets; for `includes_cache` rows, correct to `input - cache_read`
   before billing (§3).
2. One `total` definition; `billable` = `estimate_cost` USD (§6).
3. An explicit `unpriced`/`unattributed` handling + a **coverage %** field on every cost payload (§4, §5).
4. `project (repo_path)`, `backend`, `model`, `role/case`, `session` as first-class dimensions —
   standalone sessions included (§2).
5. **R1 open:** whether to add real, sourced gpt price entries (raises coverage) vs stay honest
   unpriced. Default: stay honest; table extension is a separate evidence-backed decision.

## 9. Issue #144 — context and recovery economics (2026-11-16)

**Delivery status (2026-11-18):** PR #293 final corrections verified; shipping in progress. [A117](../.ai/dispatch/AGENT_117_TELEMETRY_CONTEXT_ECONOMICS.md) owns deployment and live acceptance. Original measurements below are prior-run evidence; fresh verification follows in §9.1. Worker activation is not claimed.

The original adapter incorrectly called Claude's uncached input inclusive-cache.
Invocation totals also could not establish a current context size. The corrected
path keeps latest/entry parent-message context separate from cumulative work and
spend, excludes child messages, preserves the observed model/window and the SDK's
`total_cost_usd`, and does not reconstruct request context from legacy aggregates.
Provider cost is shown with priced-turn coverage over the full session; it is the
provider's API cost report, not a subscription invoice. Missing counters stay unknown.

Cold in-place proposals use the latest measured context/model/time when available;
the legacy largest-write fallback is explicitly a potentially stale proxy. Proposal
snapshots persist tokens, context percentage, price and assumptions. Recovery producer
tokens retain an execution-time snapshot, linked through the canonical managed turn to
its provider-reported cost and both old/new sessions. These two snapshots may differ;
no counterfactual difference is claimed as realized savings. Fresh bootstrap/output cost
is unknown before execution. Pause age no longer proves an hour-long cache hit. A positive
auto-resume ceiling cannot authorize an unknown fresh boot or a legacy proxy estimate;
it bounds only the stated cold-context scenario, not total future SDK turn spending.

Compaction advice is advisory at **80% of a provider-reported window**. Unknown windows
produce no percentage-based recommendation. Manual actions require adjacent context
observations with native-session continuity; another compaction makes attribution
ambiguous. Native `compact_boundary` records supply their own before/after evidence.
Duplicate manual/native records represent one intervention. Capacity reclaimed is signed;
a failed/no-benefit intervention cannot be turned into savings. Warm-read amortization,
when measurable, includes the assumed retained-context cold rewrite premium. Dollar
scenarios use the current price table, not a claim about historical realized savings.

Measured acceptance:

| Evidence | Result | What it proves |
|---|---|---|
| Two-request regression | current context **165,200**, total input work **325,300**, total token work **325,330**, window **200,000**, fill **82.6%** | Current context and cumulative spend/work remain distinct. |
| Sanitized natural trace from 2026-11-04, `claude-opus-5-5` | **206,931 → 57,016**, **149,915 reclaimed (72.4469%)** | Real native compaction capacity reduction, replayed through the adapter/projector. |
| That trace at verified 2026-11-16 Opus 5.5 API rates | **$0.029983** avoided warm-read equivalent/request; **$0.749575** avoided 5-minute-write equivalent/request | Explicit future scenarios only. Trace has no window or isolated compaction USD, so threshold calibration and net break-even remain unknown. |
| Durable DB→timeline recovery regression | old cold-context alternative **$1.00**, new provider-reported receipt **$0.47**, visible from both sessions | Estimate/actual lineage and full-session provider-cost coverage survive persistence; these are deterministic test values, not a paid production run. |
| Adversarial query-plan inspection of production DB, read-only | session/continued-from indexes + primary-key joins; warm median **3.49 ms** over ten calls, first cold read **575.337 ms** | No whole-task scan, automatic index build or N+1. Native IO remains variable. |
| New economics projection, 500 turns + 500 manual compactions | **80.2 ms**, **311,696 bytes** peak traced Python allocation, **9,207 bytes** JSON, **20 outcomes** | Warm incremental computation only; excludes imports and the existing history-read allocation. |

The natural trace fixture contains only anonymized message identities, token counters
and the native boundary: `tests/fixtures/context_economics/natural_compaction.ndjson`.
Reproduce its measurement with `pytest tests/test_claude_telemetry_adapter.py -k natural_compaction --tb=short`.
All verification uses the existing Python 3.12 test image with `--network none`; the host
Python 3.11 virtualenv cannot parse current `main`. Fake SDK tests drive the real reader,
adapter, projection, SQLite and timeline; UI tests exercise the displayed numbers and
unknown/error states. No paid call, production history rewrite or schema migration.

Service boundary (§7): timeline/usage share four permits through serialization; at
100 calls only four execute (the rest receive 429). Identity ≤128, cursor ≤64, no request
body; existing 1–200 item pages and 500-row source windows remain. Added evidence is
capped at 20 outcomes/recoveries/proposals, 32 native boundaries/turn and 100 direct child
sessions. One ten-second cooperative SQLite snapshot covers the read; snapshot errors
return structured 503, individual recovery failures remain visibly unavailable. Existing
native IO/Python/socket deadlines and historical payload-byte deferrals in CONTEXT.md
remain; the incremental measurements above do not establish an absolute process RSS bound.
No new endpoint, queue, background decision loop or database was introduced.

Independent adversarial review corrected context/work conflation, child usage pollution,
stale cache assumptions, wrong model pricing, incomplete cost coverage, false compaction
attribution, unbounded proposal display, query scans and misleading zero/error displays.
**Remaining acceptance limits:** 80% is an explicit policy, not an empirically optimized
threshold; historical aggregates cannot recover a missing window/context; provider SDK
activation needs an authorized worker rollout and a natural new receipt. Gateway deployment
alone cannot update an already-running worker. No worker restart is implied by these tests.

### 9.1 Fresh completion and adversarial review — 2026-11-18

Resumed checkpoint `4f1c61c` and merged current main `30f1f53` using merge commit
`d7e3e17`. No feature rewrite, schema migration, flag activation or historical replay.
The final self-adversarial pass traced native input through the actual SDK types, SQLite,
HTTP response, TypeScript adapter and display, plus proposal→producer→managed-turn lineage.

| Finding | Resolution and regression |
|---|---|
| **P1:** persisted v3 Claude metrics and UI aggregate fallback could present old/unknown context as measured; recovery could accept that measurement | Read-time projection-version guard shared by telemetry reads and recovery estimates; explicit unknown final context stops UI fallback. `test_legacy_claude_context_is_unavailable_without_rewriting_receipts`, `test_positive_auto_ceiling_requires_measured_in_place_cost`, frontend unknown-final regression. Canonical stored metrics remain unchanged. |
| **P1:** native `/compact` with boundary + cost-only result discarded its intervention | Flush unmatched boundaries with unknown after-context; retain explicit zero cost; join only an unambiguous subsequent native-session sample. Real SDK-reader cost-only regression plus manual/native completed/failed/session-replacement/intervening matrix. |
| Zero/unknown, malformed identities/counter overflow and child model contamination | Six new adapter regressions first failed against checkpoint and pass after correction. Explicit zero cache buckets survive; rejected samples invalidate context; child messages cannot replace parent model/context. Invalid/non-finite/oversized USD is rejected. |
| Failure-honesty and provenance | Unavailable compaction/recovery evidence remains visible; transport retains cost basis and distinguishes provider-reported versus token-derived cost. Foreign cache writes cannot borrow Claude's premium. Unknown reclaimed fractions remain unknown. |

No P0 found; no unresolved P1 in the reviewed implementation. This pass is a self-review,
not a newly delegated independent review. Production/calibration gates below remain open.

Fresh checks use Python 3.12.16, `ai-team:dependency-closure-tests`, `--network none`, mounted
feature checkout and `PYTHONPATH=/workspace`, invoking `python -m pytest` with `--tb=short` on:
`test_context_economics`, `test_claude_telemetry_adapter`, `test_session_timeline`,
`test_case_quota_resume`, `test_telemetry_projection`, `test_case_respawn`, `test_control_api`,
`test_sdk_driver_proactive`, `test_pricing`, `test_watched_job_consumption`,
`test_turn_queue_respawn_crash_safety`, `test_turn_queue_respawn_revalidation`,
`test_telemetry_contract`, `test_telemetry_privacy`, `test_telemetry_store`, `test_claude_driver`,
`test_claude_driver_manager_tools`, `test_turn_queue_sdk_ownership` (each under `tests/`, `.py`).
The final **363-test run passed in 36.79s**, including failed-compaction cases and both native
boundary counter spellings (a null snake-case field must not mask a valid camel-case field).
CI additionally exposed an incomplete fake SDK (`SystemMessage` absent) and a historical-repair
assertion pinned to v3; their fixtures now reflect the actual SDK/current projector contract.
`pytest tests/test_backend_activity.py tests/test_job_review_regressions.py --tb=short`
passes **13 tests in 3.34s** in the same offline Docker environment; no runtime workaround was added.
Related vitest: **41 passed across eight files**. `pnpm --dir web typecheck` passes; production
build passes with a separate writable output directory (the old `web/dist` is root-owned).

The native-stream→SQLite→HTTP integration reproduces **165,200 current-context tokens,
82.6% fill, 325,330 cumulative token work and $0.47 provider cost**. A separate durable
recovery test reconciles both session identities and the cold-alternative estimate. Transport
and render regressions preserve proposal tokens/fraction, execution receipt, zero and unknown.
Natural fixture assertions still reproduce **149,915 reclaimed tokens (72.4469%)** without
inventing a window or net savings. The added Opus 5.5 rates match the
[Anthropic pricing page](https://platform.claude.com/docs/en/about-claude/pricing), checked 2026-11-18.

Reproduce maximum-cardinality measurement with the same offline image and
`pytest tests/test_context_economics.py -k maximum -s --tb=short`: **500 turns × 32 native
boundaries + 500 manual tasks**, **2,978.599 ms** under tracemalloc, **9,923,724 peak traced
Python bytes**, **10,268 JSON bytes**, **20 returned outcomes**. Inputs/imports, complete
history allocation, native memory and sockets are excluded. Tracemalloc timing is instrumented,
not a production latency claim. Query-plan regression verifies the existing session/child/task
indexes and no automatic index; a fresh read-only production plan confirms indexed task,
session and telemetry primary-key joins. Ten empty-result reads for the most recently updated
production session measured first **0.811 ms**, warm median **0.026 ms**; this is not a cold
disk or populated recovery-history benchmark and does not supersede the prior populated measurement.

Service-boundary review: four shared permits span snapshot, projection and serialization;
at N=100, at most four execute and excess readers get 429. Serialization failure releases the
permit (regression); synchronous handlers keep it until actual work returns even if an HTTP
caller disconnects. Worst measured incremental projection at four admissions is about **40 MB**;
absolute RSS/history/native/socket bounds retain CONTEXT's trusted-operator deferral. Identity
128, cursor 64, item page 1–200, sources 500, native boundaries 32/turn, outcomes 20, child sessions
100; no new request body. Existing ten-second cooperative SQLite budget and per-lock timeout
apply; no hard Python/native IO/response socket deadline is claimed. Parameter validation and
structured 429/503 cover malformed/failed requests; individual evidence failures mark coverage
unavailable. Provider USD is finite, nonnegative and at most signed-64-bit maximum. No new DB,
endpoint, background loop, queue or worker restart.

Reserved acceptance remains: authorized worker promotion, natural linked recovery/compaction
receipts, and an adequacy evaluation using actually reported windows. Eighty percent is advisory
policy, not proven optimal calibration; keep issue #144 open for those observations.
