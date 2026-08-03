```yaml
job_id: AGENT_65_COST_MONITORING_VISIBILITY
created_at: "2026-08-03T16:59:09.868948+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T16:59:09.868970+00:00"
```

# DISPATCH — A65 · Cost monitoring & visibility: cost explorer + dashboards + budgets/alerts

**Level:** 3 (code) · **Type:** backend read-model + Web UI + flag-gated alerting
**Authored:** 2026-08-03 · **Status of this packet:** ready (authored, adversarially reviewed, not executed)
**Depends on:** — (independent; must NOT cross into the blocked A61/A63 quota-coordinator scope — see R2)

> **Read this first — why this packet exists.** On 2026-08-03 the operator asked, by hand, "for the
> past 5-6 manager sessions, was the expensive thing the manager session or the dispatched workers?"
> Answering took ~10 hand-written SQL queries against `llm_model_requests`/`llm_turns`/`sessions`/
> `flow_links` (report delivered to the operator). The findings that drive this job:
>
> 1. **All cost data is already captured** (`llm_model_requests` per-request tokens, `llm_turns`
>    session attribution, `flow_links` case membership). The gap is **projection, surfacing, and
>    action**, not capture.
> 2. **Tokens are a misleading cost proxy.** ~95-97% of the token volume in the six recent manager
>    cases was **cache reads**, billed at 0.10× input. A big company bills USD, not tokens. Today's
>    UI leans on token counts that overstate "cost" ~10×.
> 3. **No case rollup.** When workers were dispatched they cost 58-81% of the case; when not, the
>    manager was 100%. There is NO endpoint that answers "manager vs workers" for one case — the
>    Work tab shows per-session roster tokens, not a case total or split.
> 4. **The single biggest burner in the DB is unpriced.** One codex session (`3549863198d4`, ~885M
>    tokens) uses `gpt-5.6-terra`, which `pricing.py` cannot price → a cost dashboard built today
>    shows "cost unavailable" for the #1 consumer.
> 5. **No spend-over-time, no top-spenders, no budgets, no alerts.** A big company's minimum is
>    Cost-Explorer + Budgets + alerting. None exist here.

## Why (intent) — what a big company would do, right-sized

The four pillars of production cost-observability at scale, translated to THIS single-operator
SQLite gateway (respecting `context/production_vision.md` anti-goals: no opaque tooling, no
external infra, no overbuild):

| Big-company practice | Translation here |
|---|---|
| **Cost Explorer / attribution** (every $ tagged by service·env·team·feature; queryable over time) | A read-model over the already-captured telemetry: spend by **project (repo_path), backend, model, role/case, session**, bucketed by day. Bounded SQL aggregates only. |
| **Budgets + burn-rate + alerts** (budget per dimension; alarm near threshold; forecast vs budget) | Daily / per-case / per-session **USD-burn alerts** (billable tokens, NOT raw cache-read volume) surfaced in-UI + via the existing push subscription infra. **Enforcement stays OFF** — alerting is the default; killing is a flag-gated escalation that reuses the existing governor knobs. |
| **Dashboards** (at-a-glance spend, top consumers, trend) | One Web-UI **Cost** tab: spend 1d/7d/30d by project, top-N sessions by USD (with an honest "unpriced" callout + coverage %), and a per-case manager-vs-workers panel. |
| **Cost governance / kill switches** | Already partially built (`dispatch_worker` model contract, `sdk_max_turns`/`sdk_max_budget_usd`). This job's alerting must SURFACE those levers, not duplicate them. |

The #1 job-design constraint: **build the projection on validated data, not vibes.** The report's
numbers depended on careful session attribution; a dashboard that double-counts or mis-prices lies
loudly. Hence Phase 0 gates everything.

## TASK (phased; land each phase as its OWN `feat/<slug>` branch + PR + self-merge)

### Phase 0 — Truthfulness audit (gate, not a feature; do FIRST, do NOT build UI before this)
Produce a written verdict (file it under `docs/cost_monitoring_audit.md`) on every aggregation the
read-model will depend on:
1. **`is_duplicate` semantics** — `db.get_session_token_totals` filters `r.is_duplicate = 0`; prove
   the filter is required (count duplicates) and that the new read-model replicates it everywhere.
2. **Empty-model rows** — ~780 `llm_model_requests` rows have `model=''` (~688M tokens). Where do
   they come from, and are they priceable/attributable? Do NOT silently drop them; decide explicit
   disposition (e.g. carry-through with `model=None` → honest "unpriced").
3. **Unpriced codex/gpt models** — quantify USD-coverage: % of sessions and % of tokens that
   `pricing.py` can price today. This is the R1 decision input.
4. **Attribution gaps** — 26 `llm_turns` reference a session not in `sessions`; 14 turns have no
   `session_id`. Decide where they land in every projection (attributable bucket vs "unattributed").
5. **The `total` definition is inconsistent TODAY** — `db.get_session_token_totals` sets
   `total = input + output` (cache excluded) while `pricing.TokenTotals.total` sums all four
   buckets. The existing UI's "Total tokens" and its own cost estimate can disagree. Pick ONE
   definition (recommend: `total` = all four buckets; **`billable` = the `estimate_cost` USD
   number, which already prices cache reads at their real 0.10× and writes at 1.25×** — cache IS
   real usage, at its discounted rate, never zero) and make both code paths agree — this is a real
   truthfulness bug fix, not cosmetics.
6. **Price-table drift** — `pricing.py` is hardcoded (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5).
   Note in the audit that it's the single source of truth and must be bumped by a human when
   Anthropic revises; do not silently invent current prices (that's a data-quality decision — R1).

### Phase 1 — Cost read-model (`src/control/cost_read_model.py` + endpoints)
1. `GET /api/cost/explorer?dimension=project|backend|model|role|session&granularity=day&from=&to=&limit=`
   — one bounded SQL aggregate per call (reuse the `get_session_token_totals` batched pattern;
   `is_duplicate=0`; no N+1; hard `limit`). Returns per-bucket `{input, output, cache_read,
   cache_creation, billable, total, usd_est, usd_known}` using `estimate_cost` per model. USD
   coverage % is part of the payload (the honest-unpriced contract).
2. `GET /api/cases/{case_id}/usage` — **the operator-question endpoint**: manager session + its
   `flow_links`-joined workers, each with token buckets + USD, plus a `mgr_vs_workers` summary. The
   acceptance proof: the six-case table from the 2026-08-03 report is reproducible via this API.
3. `GET /api/cost/top?by=usd|billable&limit=` — top consumers (big-company "top spenders").
4. All three are read-only, additive, behind existing auth. No behavior change when unused ⇒ no
   flag needed for read endpoints (mirror `/api/flows` precedent); flag-gate only what can change
   behavior (Phase 3).

### Phase 2 — Cost / Monitoring dashboard (Web UI)
1. New **Cost** tab (follow the existing Work-tab component conventions): spend 1d/7d/30d by
   project; top-N sessions by USD with an explicit "unpriced" marker + coverage %; trend sparkline
   from `explorer?granularity=day`.
2. **Per-case manager-vs-workers panel** wired to `/api/cases/{id}/usage` (the highest-value
   artifact: it answers the report's question at a glance). Cache-vs-billable callout so token
   volume doesn't read as spend.
3. Reuse `useSessionUsage`, `compactTokens`/`formatUsd`, timeline components. Vitest for the new
   components; no new deps.

### Phase 3 — Budgets + burn-rate + alerts (flag-gated; enforcement stays OFF)
1. Budget knobs (numeric, via the A62 `/api/flags` non-boolean registry — reuse, don't invent):
   e.g. `COST_ALERT_DAILY_BUDGET_USD`, `COST_ALERT_SESSION_BURN_USD` (USD in rolling window),
   `COST_ALERT_CASE_TOTAL_USD`.
2. An alerting check on the existing loop: alert **USD/billable-based only** (never raw cache-read
   volume — the report proved that fires constantly and means nothing). Surface in-UI + existing
   `/api/push/subscribe` infra. Alerts on by default once knobs are set; **enforcement
   (interrupt/kill) OFF by default** behind a flag, and when enabled it must route through the
   existing governor path — never a new kill mechanism.
3. Explicit seam note (R2): cost-monitoring budgets are **usage-derived**; they are NOT the
   observe-only provider quota-window coordinator (A61/A63, blocked). Do not integrate or block on
   it; document the seam in the code + audit doc.

## TYPE
Level 3 (code) → every phase is `src/` or `web/` ⇒ **`feat/<slug>` branch + PR + self-merge per
phase** (branch policy). Docs-only artifacts (audit doc) ride the same branch. Do NOT dangle a
branch; do NOT merge over another loop's unmerged edits. Gateway restart only where the merged code
needs it.

## CONTEXT (what exists — reuse, don't reinvent)
- `src/services/pricing.py` — `estimate_cost(model, TokenTotals)`; the single cost source of truth
  (claude families only). `src/control/db.py::get_session_token_totals` — the batched, dedup'd
  aggregation pattern to extend (note the `total=input+output` wrinkle, Phase 0 item 5).
- `src/control/control_api.py` — `GET /api/sessions/{id}/usage` (per-session, line ~974),
  `/api/backends/usage` (per-backend, ~1859), `/api/work/{id}/roster` (per-case roster tokens).
- Web UI — `SessionDetailScreen.tsx` (`SessionUsage` panel, `useSessionUsage`),
  `CaseRosterView.tsx` (per-session roster tokens), `SessionTimeline.tsx`/`SessionTurns.tsx`
  (per-turn token badges). Navigation: Work tab exists; a Cost tab is new surface.
- `flow_links` = authoritative case membership (used in the report; `role='worker'` links the
  workers to a case).
- Push subscriptions: `src/control/control_api.py` `/api/push/subscribe|unsubscribe` (line ~1807).
- A62's `/api/flags` non-boolean registry is the numeric-knob home (budgets).
- Reference for honest-cost discipline: A63 (independent audit), `pricing.py` docstring
  ("never a fabricated number").

## ACCEPTANCE (proof, not vibes)
1. `docs/cost_monitoring_audit.md` written: all six Phase-0 items answered with real numbers
   (duplicate counts, empty-model share, USD-coverage %, attribution-gap counts) and the
   `total`-definition inconsistency fixed in code (both paths agree).
2. `/api/cost/explorer`, `/api/cases/{case_id}/usage`, `/api/cost/top` implemented with bounded
   SQL + tests; **the 2026-08-03 report's six-case manager-vs-worker table is reproduced via
   `/api/cases/{id}/usage`** (the operator-question regression proof).
3. Cost tab renders spend-by-project, top-N by USD (with honest unpriced marker + coverage %),
   and the per-case manager-vs-workers panel; vitest green.
4. Phase-3 alert fires on a billable-USD threshold in a test; enforcement path is flag-gated OFF
   by default and routes through the existing governor seam; no new dependencies anywhere.
5. Per-phase: `pytest` on touched modules only (TEST COST GUARD — never the full/e2e suite).

## RESERVED DECISIONS (surface, do not guess)
- **R1 — pricing policy for codex/gpt.** Extend `_PRICE_TABLE` with real current gpt prices
  (needs sourced data, risk of stale/fabricated numbers) vs honest "unpriced + coverage %".
  Default: **honest unpriced + coverage metric** in Phase 0/1; table extension is a separate,
  evidence-backed decision. Do not invent prices.
- **R2 — budget/alerts vs the quota coordinator seam.** Coordinator = observe-only provider
  quota-window observation (A61/A63, blocked, `quota_windows.db`). This job = usage-derived spend
  + budgets. Do NOT integrate or block on it; write the seam down.
- **R3 — alert thresholds & enforcement.** Thresholds sized from Phase-0 real data, not guessed.
  Alert default ON once knobs exist; enforcement default OFF behind a flag, via the existing
  governor path only.
- **R4 — `total` token definition.** One definition everywhere. Recommended: `total` = all four
  buckets, `billable` = input+output+cache_write. Confirm against what the UI already shows before
  flipping (Phase 0 item 5).

## SCOPE OUT
- External observability infra (Prometheus/Grafana/OLAP/TSDB) — explicitly out per
  production_vision anti-goals.
- Auto-kill / enforcement by default — OFF; flag-gated via existing governor only.
- Wiring into the A61/A63 quota coordinator (blocked; seam documented, not integrated).
- Backfilling or "repairing" historical token data — the audit may FLAG gaps; a repair job is a
  separate dispatch, not a silent backfill here.
- Redesigning the dispatch process or the DISPATCH_LOG/CONTEXT structures.
- This job's own dispatch files (`.ai/dispatch/AGENT_65_*.md`) stay point-in-time records.

## ADVERSARIAL REVIEW (recorded 2026-08-03, before dispatch — fold these in, don't argue them)
1. **"Is a dashboard overbuild? The operator just wanted one question answered."** — Absorbed: the
   minimal pain-relief IS Phase 0+1 (the case-usage endpoint). Dashboards/budgets are additive
   phases that each land as their own PR; the plan never ships "dashboard before data."
2. **Data-truthfulness is the kill-risk.** Any UI on unvalidated numbers lies (double-count
   without `is_duplicate=0`; 688M tokens with `model=''`; 26 orphan turns). — Absorbed: Phase 0 is
   a hard gate; ACCEPTANCE item 2 proves the report is reproducible via the API, not hand-SQL.
3. **The biggest consumer is unpriced (codex).** A dashboard that can't price the #1 burner is
   theater. — Absorbed: R1 default = honest unpriced + coverage %, surfaced in the UI.
4. **Alerting on tokens = noise.** Cache reads fire constantly; alerts must be billable-USD.
   — Absorbed: Phase 3 rule is USD/billable-only, explicit in the task.
5. **Enforcement is dangerous by default.** A wrong threshold killing a legit long run is worse
   than no alert. — Absorbed: alerts ON, enforcement OFF+flagged, existing governor path only.
6. **Quota-coordinator overlap (A61/A63).** Duplicating it would create two competing "budget"
   systems. — Absorbed: R2 seam, explicitly out of scope.
7. **SQLite perf / N+1.** An arbitrary-dimension explorer can degrade. — Absorbed: one bounded SQL
   aggregate per call, hard limits, reuses the proven batched pattern; tests assert no N+1.
8. **Phase-granularity risk (one giant job).** — Absorbed: each phase is a separate branch+PR so a
   bad Phase 2 never blocks Phase 1 on main.

## TRAIL / EVIDENCE (fill at close)
- `docs/cost_monitoring_audit.md` · `/api/cost/explorer` + `/api/cases/{id}/usage` +
  `/api/cost/top` tests · the six-case table reproduced via API · Cost-tab component + vitest ·
  Phase-3 alert test + flag-gated enforcement path · per-phase PR numbers.

---
## Milestone (burndown)
- [x] Phase 0 audit doc written (`docs/cost_monitoring_audit.md`, committed to `main` 2026-08-03): real-usage provenance confirmed, codex `includes_cache` double-count found, 51% unpriced share quantified, standalone-session 91% finding, attribution gaps, `total`-definition bug
- [ ] Phase 0 remaining: `total`-definition fix lands WITH Phase 1 code on its `feat/` branch (not on main)
- [ ] Phase 1: three read-model endpoints + tests; six-case report reproducible via `/api/cases/{id}/usage`
- [ ] Phase 2: Cost tab (spend-by-project, top-N by USD w/ unpriced marker, manager-vs-workers panel) + vitest
- [ ] Phase 3: budget/burn-rate alerts (billable-USD only), enforcement flag-gated OFF via governor seam
- [ ] Adversarial-review items re-checked against the shipped diff (no drift into scope-out)

## Closure (fill on completion)
(fill when executed)
