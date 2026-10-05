```yaml
job_id: AGENT_94_SYSTEM_ONE_CORE_DELIVERY_SCORECARD
created_at: "2026-10-05T08:46:32.966001+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A94             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-05T08:46:47.362691+00:00"
```

# DISPATCH — A94 · System-One (Jev) core + replay harness + Move 1 "scorecard in the wake"

**Level:** 3 (DB migration + new secret `TYPESAFE_API_KEY` + agent-behaviour change to the Manager's
wake text; > 5 files) · **Type:** code
**Authored:** 2026-10-05 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (independent of A82 Stage 8; covers both wake producers)
**Branch:** `feat/system-one-core` + PR + self-merge at close. Gateway rebuild/restart to deploy is
delegated. **No worker restart is needed or allowed.**
**Canonical spec:** [`docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md`](../../docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md)
(`SYSTEM_ONE_DECISION_LAYER_V1`). **Read §0, §1, §4, §5, §6.1, §7 before writing code.** The spec is
normative. This packet only scopes the job.

> **Read this first — why this packet exists.** Every Manager review begins with a fat, paid wake turn
> (200–300k-token context, re-read on every tool round). That turn often only discovers that a worker's
> first return is a draft: no proof, an acceptance item missing, or an off-target answer
> (`manager.md:181-214`; the PR #55 rubber-stamp scar). A calibrated System-One model can pre-score each
> delivery against its own dispatch envelope for about $0.0003 in about 300 ms. This job builds the thin
> S1 primitive once. It measures it against the Manager's historical verdicts *before* anything ships,
> then puts an advisory scorecard into the wake text behind flags with an A/B arm. It is the enabler for
> A95/A96 and for the Governor programme's S1 slots (G1/G3/G4/G5; spec §8).

## Why (intent)
Give the Manager (and later the Governor) a cheap, measured, auditable pre-review signal on every worker
delivery, and produce the **baseline numbers** that decide whether S1 earns more authority (spec §7).
The literal deliverable is a wake-text block. The real outcome is a **proven-or-falsified gate**:
- AUROC against real verdicts;
- a fitted threshold;
- a decision log that makes every later claim measurable.

## TASK
1. **Core package** `src/system_one/` (spec §5.1–§5.3):
   - `client.py`: direct `httpx` async POST to `/v1/systemone`; Pydantic request/response models;
     total budget ≤ 2.5 s with one retry on 429/529; returns `None` on any failure (P6).
   - `battery.py`: the `Battery` protocol, `Outcome`, `run_battery()`, secret redaction (P14), trimming
     rules with trim record (P5).
   - Pinned `jev-1.13.0` (P8).
   - Verify: `pytest tests/test_system_one_client.py` with `httpx.MockTransport` (timeout, 429→retry,
     529, 401, malformed JSON, missing key → `None`).
2. **Decision log** (spec §5.4): `system_one_decisions` DDL as a new numbered migration in
   `_get_migrations()` (`src/control/db.py:10209`).
   - `main` is at 42; held PR #185 reserves 43. Take the next free number at merge.
   - `MeshDB.append_system_one_decision(...)` / `get_system_one_decision(battery, version, kind,
     subject_id)`.
   - Unique index gives the per-subject cache.
   - Verify: migration test on a temp DB (fresh + upgrade from 42); writes go through
     `asyncio.to_thread` from async callers.
3. **Flags and config** (spec §5.5):
   - `S1_DELIVERY_SHADOW` and `S1_DELIVERY_ANNOTATE` in `RUNTIME_FLAG_DEFINITIONS`
     (`src/control/db.py:246`), default `0`, live, registry-writable.
   - `TYPESAFE_API_KEY` (+ optional `TYPESAFE_BASE_URL`) in `_MANAGED_ENV_KEYS`
     (`config/settings.py:13`) and the controller env in `compose.yaml`.
   - Document in `docs/ENV_FEATURE_FLAGS.md`.
4. **Delivery battery** `src/system_one/batteries/delivery.py`: state, questions, gate and render
   exactly as spec §6.1.
   - Tolerant envelope section parser for TASK / ACCEPTANCE / SCOPE OUT / RESERVED DECISIONS (the
     `manager.md:95-121` template); falls back to `dispatch.raw`.
   - Unit tests over fixture envelopes and replies, including parse-failure and no-acceptance cases.
5. **Replay harness** `scripts/system_one/replay.py` (spec §5.6): read-only `mode=ro`; refuses the live
   DB path; requires `AI_TEAM_ALLOW_JEV_LIVE=1` for live calls; local response cache.
   - **First output: the label join check.** Does `flow_events.entity_id` (tagged `review.*`) match
     `mesh_tasks.id`? Report the count.
   - Then compute AUROC, precision/recall, the threshold reaching precision 0.90, the code baseline,
     cost, latency, and option-order agreement for the `outcome` Choice.
   - Write the report to `results/s1/delivery_<version>_<date>.json`. That file is this job's evidence.
6. **Wake integration** (spec §6.1):
   - `async _s1_wake_scorecard(case_id, presented)`, called before `_render_wake_turn` in **both**
     producers (`src/orchestrator.py:2124` legacy after the claim; `:2194` managed before
     `_make_task`).
   - `_render_wake_turn(..., scorecard=None)`; `None` is byte-identical.
   - `asyncio.gather` over ≤ 6 presented tasks.
   - A/B arm by case-id hash; both arms log; only `annotate` renders.
   - Verify: tests for flags OFF byte-identical, shadow logs without rendering, annotate renders, fail-open
     renders nothing, managed replay reuses the cached decision.
7. **Run the replay** on a read-only copy of the controller DB, provided by the operator (RESERVED R1).
   - Tune question wording at most 2 rounds (Appendix A of the spec; bump `battery_version` each round).
   - Set `T_FLAG` / `T_ACT` from the report.

## TYPE
code — `feat/system-one-core` + PR + self-merge at close. Docs updates (`ENV_FEATURE_FLAGS.md`, spec
corrections found during build) ride the same PR.

## CONTEXT (reuse verbatim)
- **Seams verified on `main` @ `8e46afd`:**
  - `_render_wake_turn` (`src/orchestrator.py:2335`) and its two call sites (`:2124`, `:2194`);
  - `compute_continuation_tick` (`src/control/db.py:7180`; last `worker.wait_pending` per group wins at
    `:7211-7216`);
  - `get_flow_run` (`db.py:5970`, `objective_lock`, `completion_criteria`);
  - `get_task` (`db.py:5582`);
  - `mesh_tasks.prompt` / `reply_text` / `file_changes_json` (migration-added, written by `enrich_task`);
  - `record_review` tags verdicts to tasks (`src/orchestrator.py:6683`).
- **Jev facts and limits:** spec §3. Use the HTTP API, not the SDK (spec §5.2: no `httpx2`/`tenacity`).
- **Lessons to obey:** no DB lock across HTTP; no background scans; to_thread for DB (PRs #136/#137/#145/#147).
- **Telemetry privacy:** store no state text (`tests/test_telemetry_privacy.py` spirit; spec §5.4).

## ACCEPTANCE (proof, not vibes)
1. `results/s1/delivery_<version>_<date>.json` exists, from a real read-only DB copy, and shows:
   - label count ≥ 100 (or the documented hand-label fallback);
   - **AUROC of the max-gate ≥ 0.75 and ≥ 0.10 above the code baseline** — or an honest FAIL with the
     stop rule applied (spec §7 M3). A FAIL still closes this job; it does not ship the annotate arm.
2. Targeted pytest green: `tests/test_system_one_*.py`, the migration test and the touched orchestrator
   wake tests. **No network in tests.**
3. Flags OFF ⇒ wake text and DB behaviour byte-identical. Proven by test.
4. After deploy with `S1_DELIVERY_SHADOW=1`: `system_one_decisions` rows appear for real wakes; p95
   latency < 1.5 s; error share < 5% (`/health` unaffected).
5. Annotate arm enabled **only if** item 1 passed, with operator approval (Level 3).

## RESERVED DECISIONS (surface, do not guess)
- **R1 — DB copy.** The operator provides a read-only copy of `~/ai-team-data/controller/state/mesh.db`.
  The executor never reads the live file. Default: the job waits at TASK 7.
- **R2 — API key.** The operator places `TYPESAFE_API_KEY` in the controller `.env`. Default: S1
  stays silently off (fail-open).
- **R3 — Enabling annotate in production.** Operator approval after ACCEPTANCE 1. Default: shadow only.
- **R4 — Egress.** Harness-generic content only (spec P14). `tokens_ingest` content is forbidden.

## SCOPE OUT
- Move 2 (A95) and Move 3 bounce (A96).
- Governor slots (spec §8).
- Structured worker report block (G1).
- Any change to `compute_continuation_tick`, `record_review`, `close_case`.
- Any worker restart.
- SDK adoption.
- Retention for the decision table.

## TRAIL / EVIDENCE (fill at close)
- `evidence:` → `results/s1/delivery_<version>_<date>.json`, test files, PR link.
- DISPATCH_LOG row A94 closure line.
- CONTEXT shift note with the measured AUROC and the decision (annotate / stop).

---
## Milestone (burndown)
- [ ] Core client + battery protocol + tests (MockTransport)
- [ ] Migration + MeshDB methods + migration test
- [ ] Flags + env keys + compose + ENV_FEATURE_FLAGS.md
- [ ] Delivery battery + envelope parser + tests
- [ ] Replay script + label-join report
- [ ] Wake integration (both producers) + A/B arm + tests
- [ ] Replay on DB copy (R1) → thresholds → report artefact
- [ ] PR merged, gateway rebuilt, shadow ON, live rows verified
- [ ] Decision recorded: annotate (with R3) or stop

## Closure (fill on completion)
