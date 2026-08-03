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
