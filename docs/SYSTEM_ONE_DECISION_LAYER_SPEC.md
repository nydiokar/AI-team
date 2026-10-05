# SYSTEM_ONE_DECISION_LAYER_V1 — a calibrated System-One layer (TypeSafe Jev) for AI-team

**Status:** canonical design, accepted 2026-10-05. The operator delegated the prioritisation decision
to the owning agent; owner rulings still pending are listed in §10. Implementation is dispatched as
A94 → A95 → A96 (plus the unrelated A97 found along the way).
**Logical id:** `SYSTEM_ONE_DECISION_LAYER_V1` (short form in code, flags and docs: **S1**).
**Model under contract:** TypeSafe `jev-1.13.0`, pinned. The `jev-latest` alias is never used in production.
**Facts verified on:** 2026-10-03 → 2026-10-05. Jev docs were downloaded and read in full for the
pages cited in §3. Repo seams were checked against `main` @ `8e46afd`.
**Relationship to other specs:**
- [`.ai/context/GOVERNOR_OUTER_LOOP_V1.md`](../.ai/context/GOVERNOR_OUTER_LOOP_V1.md) is the
  Governor programme, G1→G6→X1–X3. S1 is its System-One layer; see §8.
- [`.ai/context/BOOTSTRAP_GOVERNOR_PROGRAMME_JOBS.md`](../.ai/context/BOOTSTRAP_GOVERNOR_PROGRAMME_JOBS.md)
  is the programme bootstrap task. It now carries the S1 slots.
- [`docs/SPEC_COMPLETION_PLAN.md`](SPEC_COMPLETION_PLAN.md) defines the product end-state.
- [`docs/harness/roles/manager.md`](harness/roles/manager.md) holds the review rubric that S1 encodes.
- [`docs/MANAGER_CONTEXT_CONTINUITY_SPEC.md`](MANAGER_CONTEXT_CONTINUITY_SPEC.md) is the source of
  the "pick the knee empirically" requirement S1 can later serve.

> **If you are starting cold, read §0, §1, §4 and §6. Then open the dispatch packet for the move you
> are building (A94/A95/A96).** Everything an implementer needs is in this file and the packet. Nothing
> lives only in chat history.

---

## 0. Executive summary

AI-team already has an execution-grade harness: sessions, Cases, workers, leases, Wake-Dispatcher,
telemetry and a Case ledger. What it lacks is **cheap, calibrated judgment at the points where work
changes hands**. Today that judgment is either:

- **hand-written keyword lists over free text.** Examples: `_classify_error` at
  `src/orchestrator.py:9747`, `_looks_intent_only` at `src/backends/opencode.py:154`, and
  `claims_edit_markers` at `src/validation/engine.py:138`; or
- **a full paid Manager turn.** A Manager context runs 200–300k tokens, observed in `.ai/CONTEXT.md`
  2026-08-17. The turn re-reads that context on every tool round just to conclude "this draft is not
  ready".

**TypeSafe Jev** is a *System One* model. It never generates text. You send it a `state` plus typed
questions, and it returns:

- a **choice** from a list of options;
- a **score** on an ordered rubric; or
- a **noul**, the probability that a yes/no statement is true.

Answers come with calibrated probabilities in 70–500 ms, at **$0.042 per 1M input tokens, with output
free**.

**The bet.** Build **one thin S1 core** and plug it into the **two choke points the harness already
has**:
- the **wake-turn renderer** (`_render_wake_turn`), the single place every Manager review begins;
- **`dispatch_worker`**, the single place every worker is born.

Then earn every further step with measurements. The three moves:

| Move | What happens | Why it matters |
|---|---|---|
| **1. Scorecard in the wake** (A94) | Each finished worker delivery is pre-scored against its own dispatch envelope and the Case objective. The Manager's wake text carries a compact advisory scorecard. | Better reviews from day one, zero control-flow change, and every decision becomes a label. |
| **2. Soft pre-flight on dispatch** (A95) | Before a worker session opens, the envelope is checked: does it serve the objective, is it an outcome rather than the literal request, is the acceptance checkable, is it duplicate work? A high-risk envelope is returned to the Manager *in the same turn*. The Manager may revise or override. | Catches the most expensive failure class ("built the literal words", off-objective work) at the cheapest possible moment. |
| **3. Bounce instead of wake** (A96, gated) | A delivery with a high-confidence critical failure goes back to the worker once with bounded findings. The fat Manager turn never runs. | The money move: it removes the most expensive turn type. It is allowed only after Move 1 has proven precision on real verdicts. |

The same core later serves the Governor programme with no new infrastructure:
- the **G4** adversary risk trigger, which is the open question in GOVERNOR §34;
- **G3/G4** semantic known-failure guards;
- **G5** wake materiality and information-gain checks;
- the **§26** measurement plane.

---

## 1. Where this fits: the bigger picture

### 1.1 The product goal and the gap

The end-state is fixed in `docs/SPEC_COMPLETION_PLAN.md` §0: *"One operator invocation of a Manager
drives one bounded objective to verified closure, hands-off."* The whole-loop hands-off validation (V4)
is still pending. The operator remains *"the engine at every session/turn boundary"*
(`docs/PERSISTENT_MANAGER_LOOP_ANALYSIS.md` §4).

The failures that keep the operator in the loop are **judgment failures, not plumbing**. They are
catalogued from shift notes, packets and git history:

| Recurring failure (evidence) | What fixed it so far | What S1 adds |
|---|---|---|
| **Rubber-stamp review.** PR #55, 2026-08-02: *"a diligent-but-off-target delivery scored 12/12 and passed."* | Prose doctrine (Gate 0 in `manager.md`) and count gates (PR #83) | A calibrated Gate-0 / critical-failure scorecard on every delivery (Move 1) |
| **Agents building the literal words, or drifting out of scope.** Rows in the `docs/harness/loop_config_map.md:285-298` failure table | Prompt doctrine | Envelope pre-flight (Move 2); G1 scope-drift check (§8) |
| **"Claimed done but the work wasn't really there"** (same table; PR #80 salvage dishonesty) | Milestone cadence, salvage fixes | `unproven_completion` and per-acceptance questions (Move 1) |
| **Manager stuck in a hallucinated wait, rescued by an operator poke.** Case `150d…`, 2026-09-22 | Structural fixes; A83 `open_case_idle` label (UI-only; "triangulation" deliberately unbuilt) | Belief-vs-truth check: G1/G5 slot (§8) |
| **Quality decay in long sessions "by feel at ~300k–400k"** (`MANAGER_CONTEXT_CONTINUITY_SPEC.md:26,239-242`: "pick the knee empirically") | Nothing yet | Ledger-derived quality signals plotted against `context_used_ratio` (§8, deferred) |

### 1.2 How S1 relates to the Governor programme

S1 is **not a separate lane**. It is the System-One layer that the Governor architecture needs. It
enters at the foot of the chain, in G1.

```text
                 S1 core + Moves 1–3  ──────────────┐  (A94–A96; useful even if G2+ never ships)
                        │                            │
G1 Manager behavioural hardening ◄── Moves 1–3 ARE G1's relevance/rigor/integration gates (§13.3),
   │                                  scope-drift escalation (§13.5) and closure checks (§13.6)
   │
G2 Governor core outer loop        ◄── no S1 (structural: campaigns, leases, idempotency)
   │
G3 Adversary benchmark             ◄── S1 batteries as benchmark arms; S1 as localization/scoring helper
   ↓
G4 Adversary implementation        ◄── S1 adversary RISK TRIGGER (answers GOVERNOR §34) + attack routing
   │
G5 Governor control plane          ◄── S1 wake materiality, information gain, activation tier, context ranking
   ↓
G6 External project adapter        ◄── no S1 (fail-closed envelope/schema code)
   ↓
X1 tokens_ingest adapter → X2 pilot → X3 verdict   ◄── S1 only on harness-generic content (§4 P14)
```

**Division of labour:**
- **The Governor and the Adversary are System Two.** They are LLM roles: slow, expensive, rare, and
  they *decide*.
- **S1 is the fast layer underneath them.** It never decides direction or truth. It keeps System Two
  dormant until something matters, and hands it precise signals when it does.

### 1.3 Why the dropped "Supervisor" argument does not apply

`docs/harness/operating_model.md:18-21` dropped the Supervisor role: *"A third agent to supervise one
task is friction, not safety."* The friction was the **cost and latency of an LLM** on every
supervised step. An S1 check costs about $0.0003 and about 300 ms, and needs no session, no context
cache and no Claude quota. So S1 provides supervision *without* re-introducing that friction.

### 1.4 Compliance with standing repo constraints

| Constraint | Where | How S1 complies |
|---|---|---|
| Nothing acts without an operator invocation bounding it | `docs/Task_Harness_v0.7_AUTOMATION.md` §0.2 | S1 runs only inside existing operator-invoked Cases and existing wake/dispatch paths. It creates no new triggers or spend. Move 3 sends at most one bounded instruction to an *already-dispatched* worker. |
| "Do not implement an automatic classifier as the primary mechanism" for model choice | `.ai/dispatch/DROP_MANAGER_WORKER_MODEL_SELECTION_CONTRACT.md` | S1 never picks models. A tier hint may appear as advisory text in the scorecard; the Manager decides. |
| No supervisor agent; the Manager absorbs review | `operating_model.md` | S1 is not an agent. It is a function call whose output the Manager reads. |
| Per-turn audit data mandatory; DB canonical | `.ai/CONTEXT.md` architecture rules | Every S1 decision is persisted (§5.4) and replayable (§5.6). |
| Telemetry privacy: `llm_events` carries no prompt/response text | `tests/test_telemetry_privacy.py` | S1 stores no state text, only refs, a hash and answers (§5.4). |
| TEST COST GUARD | `CLAUDE.md` | Tests never call the live API (§4 P16). Live evaluation is opt-in behind an env guard. |
| Anti-goals: no opaque memory, no swarm | `.ai/context/production_vision.md` §6 | Answers are typed, logged and auditable. No memory, no agents. |

---

## 2. What we considered, and why these choices

This is the reasoning record, kept so future owners don't re-litigate it. Each candidate was scored on
six criteria (1–5), weighted:
- **I:** measurable impact on harness cost or quality (×3);
- **F:** fit to Jev's strengths and avoidance of its documented weak spots (×2);
- **G:** ground truth available today for before/after (×2);
- **D:** a seam exists, it can be flag-gated, shadow mode is possible (×2);
- **S:** a wrong answer is bounded or reversible (×1);
- **N:** a plain regex/code fix would *not* do as well (×2).

Maximum is 60.

**Kept (folded into Moves 1–3 or Governor slots):**

| Candidate | Score | Disposition |
|---|---|---|
| Worker delivery gate before the Manager wakes | 53 | **Moves 1 + 3** |
| Judgment ledger: label history and operator messages | 53 | S1 decision log + replay now; full ledger with G3/§26 |
| System-One governor triggers (drift, decision conflict, belief vs truth) | 49 | G1 scope-drift + G5 materiality slots |
| Logic link checks (claim↔evidence, diagnosis↔change, finding↔conclusion) | 49 | Move 1 questions; structured worker report via G1 |
| Dispatch envelope pre-flight | 49 | **Move 2** |
| Lessons-learned library (packet-closure findings, incidents) as guards or injection | 49 | G3/G4 semantic guards |
| Empirical context-quality knee | 49 | Deferred; revisit when replay data exists |
| Self-improving gates (TypeSafe's question-discovery loop, run as a harness Case) | 47 | Deferred; needs ≥ 300 labels |

**Rejected or deferred, with the reason:**

| Candidate | Score | Reason |
|---|---|---|
| Error classification by Jev | 40 | A code fix is better. The real defect is that `_failure_text` (`src/orchestrator.py:1006-1025`) feeds the agent's own reply into keyword matching. Dispatched separately as **A97**. |
| Same turn-outcome labels across all backends | 51 | Owner verdict: not strategic enough. Its valuable part lives in Move 1 and the belief-vs-truth slot. |
| Cheap-first model/backend escalation ladder | 44 | Too complex; conflicts with the model-selection contract. Kept only as advice text in the scorecard. |
| Session hygiene (fresh session vs reuse) | 47 | Valid, but secondary to the Governor programme; revisit via the knee analysis. |
| Telegram intent routing, approval risk scoring, tool-call permission gates | ≤ 39 | Low leverage, or weak against adversarial content (community evidence, §3.3) |
| Skill suggestion | 31 | `skills/` holds 3 skills; the measured cookbook gain needs a large roster. |
| Best-of-N worker tournament | 34 | N× worker cost; Jev can't judge code correctness. |
| Session compaction by Jev | 27 | The harness does not own backend context; community results negative or unproven. |
| Reset-time / date extraction | 24 | Date comparison is a documented Jev weak spot; the provider supplies `resetsAt`. |

Why **Jev** and not Haiku as the classifier:
1. **It does not consume the Claude subscription window.** Quota pauses are a recurring theme of this
   repo, and an S1 check keeps working during a Claude 429/529.
2. **Calibrated probabilities make thresholds tunable.** You can compute AUROC and pick a cutoff. LLM
   yes/no answers drift between runs even at temperature 0 (TypeSafe self-consistency cookbook).
3. **About 25× cheaper on input than Haiku, output free, and fast enough for the hot path.**

---

## 3. Jev: verified facts that constrain the design

Source pages: `https://docs.typesafe.ai/{introduction,concepts/state,primitives,confidence,models,api,model-jaggedness/jev-1.13,patterns/*,cookbooks/*}.md`.
Index: `https://docs.typesafe.ai/llms.txt`.

### 3.1 Model and API

| Fact | Value |
|---|---|
| Endpoint | `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer $TYPESAFE_API_KEY` |
| Request | `{state, model, questions: {<id>: Question}}`. The question id is never sent to the model. |
| Question types | **noul** `{type, instructions, criteria?: {true, false}}`; **choice** `{type, instructions, criteria: {option: description\|null}}` (≤ 255 options); **score** `{type, instructions, criteria: [level0..levelN]}` (2–10 levels) |
| Structured instructions | `instructions` may be an object. Put the question in one field and data in others, and refer to fields by name in backticks. |
| Response | `{model: "jev-1.13.0", answers: {<id>: {type, noul}\|{type, choice, probabilities, confidence}\|{type, score, legend, probabilities, confidence}}, usage: {input_tokens, output_tokens}}` |
| Confidence | Choice and Score only. Choice: `(k·p_max − 1)/(k − 1)`. Score: 1 − probability-weighted distance from the peak, normalised by an even spread. A noul **has no confidence field**: the noul *is* the probability. |
| Errors | 401, 422 (validation), 429 (rate limit), 529 (overloaded). Back off on 429/529. |
| Model | `jev-1.13.0`. Aliases `jev-latest` and `jev-preview` both currently point to it, and **move without notice**. |
| Price | $0.042 per 1M input tokens; output free |
| Rate limits | 100K tokens/s and 80 requests/s, "adjusting dynamically" |
| Context | 64k tokens per request (state + all questions); **32k for state + the longest question** |
| Input | Text only; English is best |
| Data | Not trained on customer data; zero data retention only on enterprise plans |
| SDK | `typesafe-sdk` 0.7.2 on PyPI; depends on `httpx2`, `tenacity`, `pydantic>=2.12` |

### 3.2 Documented weak spots (jev-1.13 jaggedness page, reviewed 2026-10-02) → binding design rules

| Weak spot | Our rule |
|---|---|
| Literal reading; answers the question as written | Write the exact condition. Put boundary cases in `criteria`. When explaining a wrong answer, that explanation is the missing half of the question. |
| Math, counting, numeric representations | Counts, sizes, costs and thresholds are computed **in code** and passed as facts or named buckets. |
| Date/time comparison | Never ask Jev to compare times. |
| Indirection, multi-hop | One hop per question. Refer to named state fields. |
| Large state full of irrelevant detail | Filter in code first. Target ≤ 8k state tokens per battery. |
| Adversarial content can move answers | State is data. S1 is never a sole security control. Prefer agent-authored over externally-authored state. |
| Contradictory instructions/criteria | `true` must mean the literal yes of the instruction. Always phrase "bad = true" consistently. |
| Choice option order bias (first option favoured) | In shadow phases, run Choice questions in two orders and log agreement. |
| Not a generator | Extraction becomes a Choice over code-extracted candidates. |

### 3.3 Evidence: what works and what doesn't

**TypeSafe cookbooks (vendor-run, with published method):**
- **Skill suggestion:** wrong skill loads 16.8% → 7.3% and needless loads 9.8% → 4.0%, over 488
  requests with an agent. Pattern: a wide ranking, then a re-check of the top 3; either step may return
  empty.
- **Structured-data-extraction cascade:** cheap model → per-field Jev verifier → escalate. The cascade
  frontier sits above every single model. Verifier rules: narrow and grounded, **bad = TRUE**,
  per-field, aggregate with **max**, independent and cheap.
- **Self-consistency (nouls):** Jev mean latency 111 ms. LLM answers moved between runs even at
  temperature 0.
- **Parallel questions:** batching 13 questions into one call was 12.2× cheaper and 10× faster, with
  unchanged answers.

**Community harness integrations** (catalogue: `github.com/AbdelStark/awesome-typesafe-jev`):
- **jev-belay**, a Claude Code stop hook checking "claims done without verification": **AUROC 0.976**
  on 100 labeled stops, against 0.777 for wording rules. At its 0.70 threshold it caught 7 of 12 false
  "done" claims with 1 wrong block per 100 stops, at $0.00005 per question and 346 ms median. This is
  the closest published analogue to Move 1.
- **Foreman**, a Codex supervisor with 10 nouls in one call (completion, verification, worker health,
  drift, needs-human). Thresholds 0.65–0.80, deterministic arbiter. Self-described *"accuracy unproven"*.
- **Negative or null results, taken seriously:**
  - **jev-axi:** agents read fewer files but cost the same.
  - **pi-jev-context:** context shortening returned a negative result.
  - **jev-certify:** threshold calibration missed its target by 3.6× under distribution shift.
  - **jev-engineering:** authority-framed injections moved 3 of 30 decisions.
  - **jevcal:** thresholds fitted on fewer than about 100 labels "should not be trusted".
- **Independent evaluation:** an action-gate study found Jev matched 100/111 labels against Claude's
  102/111, each with one unsafe allow.

**Design consequences:**
- S1 is an **asymmetric** gate: it may reject early or annotate, never accept.
- Thresholds are fitted on our own labels and re-checked.
- S1 is never the sole guard for anything security- or BLOCKER-class.

### 3.4 The Jev techniques we use

| Technique (TypeSafe doc) | Where we use it |
|---|---|
| **Speculative fan-out** (`patterns/fan-out`): all questions in one request; code ignores the irrelevant ones | Every battery is one request. Per-acceptance-item nouls are generated dynamically. |
| **Verifier "bad = true", per-field, max-gate** (SDE cascade appendix) | Move 1 critical-failure gate; Move 2 pre-flight gate |
| **Confidence-gated routing** (`patterns/confidence-routing`): a floor plus a per-action threshold scaled to the consequence | Annotate at the low threshold; bounce only at the high-precision threshold (Move 3) |
| **Composite scoring** (`patterns/composite-scoring`): atomic scores, weights in code | The G1 six-dimension rubric when exposed as Scores (§8) |
| **Line-id Choice** (`cookbooks/semantic_find`): tag lines with ids, Choice over ids, plus a Noul "is the answer present at all?" | Evidence localization for G3 (adversary localization accuracy); pointers in the scorecard |
| **Two-stage shortlist** (`cookbooks/skill_suggestion`) | Lessons-learned guard selection; attack-class routing in G4 |
| **Structured instructions** (`primitives/advanced`) | Per-criterion questions: `{"criterion": "...", "question": "Does <worker_report> show evidence that <criterion> is satisfied?"}` (in the real request, the field names inside the question are wrapped in backticks) |
| **Question-discovery loop** (`cookbooks/autoresearch_feature_discovery`): LLM proposes questions, Jev answers, a classical model fits outcomes, iterate on the worst errors | Deferred self-improving gates; run as a harness Case once ≥ 300 labels exist |

---

## 4. Design principles (normative)

**MUST** and **SHOULD** follow RFC 2119. Every S1 change is reviewed against this list.

- **P1. Asymmetric authority.** S1 MAY annotate, soft-refuse (with Manager override) or bounce
  (bounded, Move 3 only). It MUST NOT accept work, close Cases, record `review.accepted`, decide
  direction, or establish truth. *Manager completion is not truth* (GOVERNOR invariant 12), and an
  S1 "pass" is not either.
- **P2. Code owns the deterministic parts.** Arithmetic, counts, dates, thresholds, control flow,
  side effects and parsing all live in code. S1 gets computed facts (`files_changed: 0`), never raw
  material to count.
- **P3. Atomic, literal questions.** One judgment per question, one hop, named state fields in
  backticks, explicit `criteria`. The problem case is always `true`.
- **P4. One request per battery.** Fan out all questions in one call; code decides relevance after.
- **P5. Bounded state.** Only the fields the questions need. Target ≤ 8k state tokens; the hard
  limit is 32k for state plus the longest question. Trim *by rule*: last N chars of a report, ≤ 30 file
  paths, objective plus criteria verbatim. Record the trim in the decision row.
- **P6. Fail open.** Any S1 error, timeout, 429/529, missing key or flag OFF MUST yield exactly
  today's behaviour. S1 never blocks a harness path.
- **P7. Off the event loop, bounded time.** Async HTTP with a total budget of ≤ 2.5 s (connect +
  read + one retry). No DB write lock is held across an HTTP call. DB writes go through
  `asyncio.to_thread`. These are the lessons of PRs #136/#137/#145/#147.
- **P8. Pinned and recorded.** Requests use `jev-1.13.0`. Every decision row records the model the
  API reports, the battery version and the thresholds applied. A model change is a battery version
  bump plus a replay re-check.
- **P9. Pure batteries, logged decisions.** A battery is `build_state(record) → State`,
  `questions(state) → dict`, `gate(answers, facts) → Outcome`, all pure. The same code runs live, over
  history and inside benchmarks. Every live evaluation writes one decision row.
- **P10. Earn every authority.** The ladder is shadow (log only) → annotate (text the Manager reads)
  → act (soft-refuse or bounce). Each step needs the measured bar in §7. No step is skipped.
- **P11. Thresholds from our labels.** Fit on ≥ 100 labeled outcomes (prefer ≥ 200); record the fit
  set. Re-check monthly and whenever the label mix shifts (jev-certify lesson).
- **P12. Option-order check.** Shadow phases run Choice questions in two orders. Disagreement over 5%
  blocks promotion of that question.
- **P13. State is data.** Agent-authored content only (worker reports, envelopes, ledger rows). S1 is
  never the sole security control and never gates on externally-authored text.
- **P14. Egress boundary.** Only harness-generic content goes to api.typesafe.ai: envelopes, reports,
  Case objective and criteria, computed facts. Before sending, redact strings matching the repo's
  secret/token patterns (reuse any existing redaction helper; else a minimal regex set for bearer
  tokens, `sk-…`, `ghp_…`, `KEY=…`). **No `tokens_ingest`/project-domain content until the owner rules
  on X1** (§10, R-1).
- **P15. No duplicate state.** One append-only decision table; everything else is read from existing
  Case/task state. No shadow ledger (GOVERNOR §13.7, bootstrap §10).
- **P16. Tests never hit the network.** Unit tests use `httpx.MockTransport` and recorded fixtures.
  Live evaluation runs only with `AI_TEAM_ALLOW_JEV_LIVE=1`, mirroring the e2e cost guard.

---

## 5. Architecture: the thin core

### 5.1 Components

```text
src/system_one/__init__.py
src/system_one/client.py        async HTTP client (httpx), Pydantic request/response models, fail-open evaluate()
src/system_one/battery.py       Battery protocol (pure functions), run_battery() orchestration, redaction, trimming
src/system_one/batteries/delivery.py    Move 1 (A94)
src/system_one/batteries/preflight.py   Move 2 (A95)
src/control/db.py               + system_one_decisions DDL (new numbered migration) + append/get methods on MeshDB
src/control/db.py               + RUNTIME_FLAG_DEFINITIONS entries (S1_* flags)
config/settings.py              + TYPESAFE_API_KEY in _MANAGED_ENV_KEYS (+ optional TYPESAFE_BASE_URL)
scripts/system_one/replay.py    offline replay over a COPY of mesh.db → JSON report (evidence artifact)
tests/test_system_one_*.py      MockTransport-based unit tests; replay smoke test on a fixture DB
```

A new top-level package is justified because S1 is a cross-cutting primitive used by orchestrator,
control API and scripts, and it has no natural home in `services/` or `core/`. Keep it small. If it
grows past about 600 lines excluding batteries, stop and re-justify.

### 5.2 Client decision: direct `httpx`, not `typesafe-sdk`

The owner allowed either. We choose **direct `httpx`** (already pinned: `httpx==0.28.1` in
`constraints.txt`):
- one endpoint, about 80 lines;
- **zero new dependencies.** The SDK would add `httpx2` (deliberately removed from the prod venv on
  2026-09-19 as a stray) and `tenacity` to the constraints lock;
- full control over the P6/P7 time budget.

The public surface mirrors the SDK's shape, so switching later is a one-file change. Sketch:

```python
class S1Answers(BaseModel):            # Pydantic, per repo Python rules
    model: str
    nouls: dict[str, float]
    choices: dict[str, ChoiceAnswer]   # choice, probabilities, confidence
    scores: dict[str, ScoreAnswer]     # score, probabilities, legend, confidence
    input_tokens: int
    latency_ms: int

async def evaluate(state: dict, questions: dict[str, dict], *, timeout_s: float = 2.5) -> S1Answers | None:
    """POST /v1/systemone. Returns None on ANY failure (P6). One retry on 429/529 inside the budget."""
```

### 5.3 Battery contract

```python
class Battery(Protocol):
    name: str                 # "delivery", "preflight", ...
    version: str              # bump on any question/threshold/state change → forces replay re-check
    def build_state(self, record: Mapping[str, Any]) -> dict | None   # None = not applicable (skip, log nothing)
    def questions(self, state: dict) -> dict[str, dict]               # Jev question objects keyed by id
    def gate(self, answers: S1Answers, facts: Mapping[str, Any]) -> Outcome  # pure; uses module-level thresholds

class Outcome(BaseModel):
    verdict: Literal["pass", "flag", "act"]   # act = soft-refuse (preflight) / bounce-eligible (delivery)
    reasons: list[Reason]                      # (question_id, probability, short label) sorted desc
    render: str                                # compact advisory text for the Manager (≤ ~12 lines)
```

`run_battery(battery, record, subject)` does the following, in order:
1. Check the flags.
2. Return the cached decision for `(battery, version, subject_id)` if one exists. This makes managed
   (A82) replays deterministic.
3. `build_state`, then redact and trim.
4. `evaluate`.
5. `gate`.
6. Append a decision row off the event loop.
7. Return the Outcome, or `None` on fail-open.

Thresholds are **module constants inside the battery** (versioned and code-reviewed), not env
variables. A62 numeric flags can expose them later.

### 5.4 Decision log (the only new table)

```sql
CREATE TABLE IF NOT EXISTS system_one_decisions (
    decision_id     TEXT PRIMARY KEY,          -- uuid4
    battery         TEXT NOT NULL,             -- 'delivery' | 'preflight' | ...
    battery_version TEXT NOT NULL,
    subject_kind    TEXT NOT NULL,             -- 'task' | 'dispatch' | ...
    subject_id      TEXT NOT NULL,             -- mesh task id / dispatch idempotency id
    case_id         TEXT,                      -- flow_run id when Case-scoped
    arm             TEXT NOT NULL,             -- 'shadow' | 'annotate' | 'act' (A/B arm actually applied)
    model           TEXT,                      -- as reported by the API, e.g. 'jev-1.13.0'
    state_sha256    TEXT NOT NULL,             -- hash of the redacted, trimmed state (no text stored)
    trims_json      TEXT,                      -- what was trimmed (field → kept/total chars)
    answers_json    TEXT NOT NULL,             -- raw typed answers (probabilities only; no state text)
    verdict         TEXT NOT NULL,             -- pass | flag | act | error
    reasons_json    TEXT,
    input_tokens    INTEGER,
    latency_ms      INTEGER,
    error           TEXT,                      -- fail-open cause when verdict='error'
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_s1_subject ON system_one_decisions(battery, battery_version, subject_kind, subject_id);
CREATE INDEX IF NOT EXISTS ix_s1_case ON system_one_decisions(case_id, created_at);
```

- **Migration number:** the next free number at merge time. `main` is at **42**; the held A82 Stage 8a
  PR #185 introduces **43**. Coordinate, never collide.
- **Storage:** about 1 KB per row. Retention is not needed at expected volume (well under 10k
  rows/month). If needed, add age-based pruning later on the A93 pattern.
- Cost and latency come from this table. **No new metrics infrastructure.**

### 5.5 Flags and configuration

| Flag (registry) | Default | Meaning |
|---|---|---|
| `S1_DELIVERY_SHADOW` | `0` | Evaluate the delivery battery for presented tasks and log only |
| `S1_DELIVERY_ANNOTATE` | `0` | Also render the scorecard into the wake text, for the A/B "annotate" arm (implies evaluate) |
| `S1_PREFLIGHT_SHADOW` | `0` | Evaluate envelopes at dispatch and log only |
| `S1_PREFLIGHT_ACTIVE` | `0` | Soft-refuse high-risk envelopes (Manager can override) |
| `S1_DELIVERY_BOUNCE` | `0` | Move 3 bounce; refuses to enable unless the A96 precondition artefact exists (§6.3) |

All entries are `effect_scope: live` and `registry_writable: 1` in `RUNTIME_FLAG_DEFINITIONS`
(`src/control/db.py:246`), documented in `docs/ENV_FEATURE_FLAGS.md`.

Environment: `TYPESAFE_API_KEY` (secret, controller only) and optional `TYPESAFE_BASE_URL`. Add both
to `_MANAGED_ENV_KEYS` (`config/settings.py:13`) and to the controller env passthrough in
`compose.yaml`. **No key means S1 is off, silently** (P6).

**A/B assignment:** `arm = "annotate" if int(sha256(case_id)[:8], 16) % 2 == 0 else "shadow"` while both
flags are on. This is stable per Case. Both arms evaluate and log; only the annotate arm renders. That
gives counterfactual scores in both arms.

### 5.6 Replay harness (the "before" measurement and the promotion gate)

`scripts/system_one/replay.py --db <COPY of mesh.db> --battery delivery [--limit N] [--out results/s1/<battery>_<version>_<date>.json]`

**Read-only.** It opens the DB with `mode=ro` URI and refuses a path that is the live DB.

1. **Build records** exactly as the live path does. Reuse the battery's `build_state`; the record
   loader is the only replay-specific code.
2. **Labels for `delivery`:** `flow_events` with `event_type IN ('review.accepted','review.rework_requested','review.waived')`
   and `entity_type='task'`, joined on `entity_id` to the worker task.
   - Positive (problem) = `rework_requested`; negative = `accepted`. `waived` is excluded.
   - **First step: verify the join key** (`flow_events.entity_id` ↔ `mesh_tasks.id`) and report the
     label count. If there are fewer than 100 tagged verdicts, report that and switch to the fallback:
     hand-label 150 stratified deliveries in a CSV the script ingests. No promotion on fewer than 100
     labels.
3. **Code baseline,** computed alongside: flag if `files_changed == 0` or the error class is non-empty.
   This is the "plain code rule" S1 must beat.
4. **Report:** AUROC per question and for the max-gate; precision/recall at candidate thresholds; the
   threshold achieving precision ≥ 0.90; cost (input tokens × $0.042/M); latency p50/p95; and
   option-order agreement for Choice questions.
5. Live API calls require `AI_TEAM_ALLOW_JEV_LIVE=1`. Results are cached per `(battery, version,
   subject)` in a local sqlite next to the output, so re-runs are free.

Expected cost of one full replay: about 2,000 deliveries × about 7k tokens ≈ 14M tokens ≈ **$0.60**.

---

## 6. The three moves: implementation specs

### 6.1 Move 1: scorecard in the wake (A94)

**Seam.** Both wake producers call `_render_wake_turn(case_id, presented)`:
- the legacy producer at `src/orchestrator.py:2124`;
- the managed A82 producer at `src/orchestrator.py:2194`.

Add `async def _s1_wake_scorecard(case_id, presented) -> str | None` and call it *before* each render.
Extend `_render_wake_turn(case_id, presented, scorecard: str | None = None)`; `None` keeps the output
byte-identical.

- **Legacy path:** call after the claim succeeds (line 2121) and before rendering.
- **Managed path:** call before `_make_task`. Determinism on replay comes from the decision cache (§5.3).

**Total added latency** is bounded by P7. Evaluate presented tasks concurrently with `asyncio.gather`
and cap at ≤ 6 tasks per wake (more → "+N more not scored").

**Record → state** (`build_state`, all from existing state):

```text
objective            flow_runs.objective_lock (get_flow_run, db.py:5970) — verbatim
completion_criteria  flow_runs.completion_criteria — verbatim list
dispatch             mesh_tasks.prompt of the worker task, parsed by a tolerant section parser:
                     TASK, ACCEPTANCE (bullets), SCOPE OUT, RESERVED DECISIONS (manager.md envelope template).
                     Parse failure → dispatch.raw (trimmed) and per-acceptance questions are skipped.
worker_report        mesh_tasks.reply_text — if a structured block (G1 claim/report block) exists, send it whole;
                     else the last 6,000 chars (trim recorded)
facts (code)         files_changed (len file_changes_json), changed_paths (≤30), outcome/success, error_class,
                     tool-call counts by category for the turn if telemetry has them (optional; omit if absent)
```

**Questions** (all nouls, problem = `true`; drafts to be tuned only via replay):

| id | Instruction (literal) | Maps to |
|---|---|---|
| `off_target` | "Does `worker_report` deliver something other than the outcome requested in `dispatch.task`, so that it does not move `objective` forward?" | manager.md Gate 0 / critical "off-target delivery"; GOVERNOR §13.3 relevance |
| `unproven_completion` | "Does `worker_report` claim the task is complete without citing observable proof such as test output, command results, or commit ids?" | critical "claiming completion without observable proof" |
| `acc_<i>_unmet` (per bullet) | structured instructions: `criterion` = the bullet text; question = "Does &lt;worker_report&gt; fail to show evidence that &lt;criterion&gt; is satisfied?" (field names in backticks in the real request) | critical "omitting a named deliverable"; GOVERNOR §13.3 rigor |
| `answerable_question` | "Does `worker_report` end by asking a question that `dispatch` or `objective` already answers?" | critical "asking a question answerable from available context" |
| `stopped_at_diagnosis` | "Does `worker_report` stop at a diagnosis when `dispatch.task` required a fix or an implementation?" | critical "stopping at diagnosis" |
| `ignored_contradiction` | "Does `worker_report` mention a result that contradicts its own conclusion without resolving it?" | critical "ignoring contradicting evidence"; logic link finding↔conclusion |
| `unpursued_anomaly` | "Does `worker_report` mention an unexpected result that it did not investigate?" | rubric dim 3 "anomaly pursuit" |
| `scope_exceeded` (if SCOPE OUT present) | "Does `worker_report` describe work that `dispatch.scope_out` excludes?" | critical "exceeding explicit scope" |
| `reserved_taken` (if RESERVED present) | "Does `worker_report` describe taking an action listed in `dispatch.reserved_decisions`?" | critical "exceeding authority" |
| `claims_file_changes` | "Does `worker_report` claim that it created or changed files?" | Combined **in code** with `facts.files_changed == 0` → `phantom_changes` |

One informational Choice, `outcome`: {`completed`, `partial`, `blocked_needs_input`, `diagnosis_only`,
`no_work_done`}. It isn't gated; it's logged for the ledger and for G1/G5.

**Gate.** `critical = max(off_target, unproven_completion, acc_*_unmet, answerable_question,
stopped_at_diagnosis, ignored_contradiction, scope_exceeded, reserved_taken, phantom_changes)`.
- `flag` if `critical ≥ T_FLAG`. The initial value is 0.70; it is replaced by the replay-fitted value
  before annotate goes live.
- `act` (bounce-eligible, used only by Move 3) if `critical ≥ T_ACT`, the replay threshold reaching
  precision ≥ 0.90.

**Render** (appended to the wake text, advisory wording fixed):

```text
[S1 pre-review — advisory signals from a fast classifier, NOT a verdict; verify in git as usual]
task_ab12: ⚠ acceptance #2 not evidenced (0.84) · ⚠ completion claimed without proof (0.77) · ok: relevance, scope
task_cd34: no flags
```

**Annotate does not change the Manager's authority** and does not change `compute_continuation_tick`.

**Acceptance (A94):**
- the replay report exists and passes the §7 bars;
- flags OFF yields a byte-identical wake;
- fail-open is tested (timeout, 429, malformed response, missing key);
- decision rows are written for both arms;
- targeted pytest is green.

### 6.2 Move 2: soft pre-flight on dispatch (A95)

**Seam.** `scripts/mcp_manager.py::_dispatch_worker` (line 401) runs on the **worker node**, from that
node's checkout (`src/mcp_launchers.py`). It talks to the gateway over HTTP via `_api_request`. So:
- the battery and the Jev key live **gateway-side** behind a new authenticated endpoint,
  `POST /api/system-one/preflight` in `src/control/control_api.py`;
- `mcp_manager` calls it **before** `POST /api/sessions` opens a worker (line 490).

**Request:** `{case_id, envelope_text, session_reuse: bool}`. **Response:** `{verdict, reasons,
render, decision_id}`, or `{verdict: "pass"}` on fail-open.

**State:**
- `objective` and `completion_criteria`;
- the parsed envelope;
- `case_tasks`: the last ≤ 10 dispatched worker tasks in this Case, as their TASK line plus status,
  read with one batched query (no N+1).

**Code-only checks first,** with no Jev: missing ACCEPTANCE section, empty SCOPE OUT. These are
returned as notes.

**Questions:**

| id | Instruction |
|---|---|
| `not_serving_objective` | "Does `envelope.task` fail to move `objective` forward?" |
| `activity_not_outcome` | "Is `envelope.task` phrased as an activity to perform rather than an outcome to achieve?" |
| `acc_<i>_uncheckable` | structured instructions: `criterion` = the bullet; question = "Is &lt;criterion&gt; impossible to verify by inspecting an artifact, a command output, or data?" |
| `duplicate_work` | "Does `envelope.task` request an outcome already requested by a task in `case_tasks`?" (+ a Choice over `case_tasks` ids + `none`, for the pointer) |
| `unlisted_fork` | "Does `envelope.task` involve a paid, destructive, merge, deployment or strategic choice that `envelope.reserved_decisions` does not list?" |

**Gate.** `act` (soft refusal) if any value ≥ `T_ACT_PREFLIGHT`. Start conservative at 0.85 and tune
from shadow data. `flag` gives notes only.

**Manager experience.** On `act`, `_dispatch_worker` returns the findings **without dispatching**:

```text
Dispatch held by S1 pre-flight (advisory). Findings: … Revise the envelope, or re-call dispatch_worker
with preflight_ack=true to dispatch unchanged.
```

`preflight_ack=true` skips the hold and is logged as an override.

**Rollout facts:**
- The endpoint is a gateway change: deploy by rebuilding the controller and restarting the gateway,
  which is delegated.
- The `mcp_manager` change reaches a node when that node's checkout is updated. New Manager sessions
  spawn the MCP server from disk. **No worker-daemon restart is required.** Verify at implementation.
- Until a node is updated, dispatch behaves exactly as today. That is fail-open by construction.

**Acceptance (A95):**
- shadow log shows flagged envelopes reworked at least twice as often as unflagged ones, over at least
  60 dispatches;
- override rate under 50% once active;
- added dispatch latency p95 under 1.5 s;
- flags OFF gives byte-identical dispatch.

### 6.3 Move 3: bounce instead of wake (A96; blocked on A94 data)

**Precondition (mechanical).**
- `results/s1/delivery_*.json` from A94 shows **precision ≥ 0.90 at `T_ACT` on ≥ 100 labeled
  verdicts**, plus ≥ 2 weeks of live shadow/annotate rows agreeing within 0.05 precision.
- The flag refuses to turn on while the artefact is missing (checked at flag read).

**Mechanics.** In the Wake-Dispatcher, for a presented task whose cached delivery outcome is `act`:
1. **Bounce at most once per original task.** The marker is a new flow event, `s1.delivery_bounced`
   `{from_task, to_task, decision_id, reasons}`, with `entity_type='task'`. Add it to `FLOW_EVENT_TYPES`.
   It is not a review event, so it does not drain anything by itself.
2. **Submit one bounded rework instruction** into the *same worker session* via the existing
   `submit_instruction` with `join_case_id`. The text is fixed: the findings plus "address these, then
   report again". Principal: `automation`.
3. **Swap membership.** Re-emit `worker.wait_pending` for the same `wait_group_id` with `from_task`
   replaced by `to_task`. `compute_continuation_tick` keeps the **last** pending event per group
   (`src/control/db.py:7211-7216`), so this needs no new reader logic. Verify with a unit test over the
   real function.
4. **Do not deliver a wake** for that task this tick. Other presented tasks wake normally.
5. **The next wake for `to_task`** shows the Manager the bounce history: "S1 bounced task_X once for
   …; this is the rework".

**Hard limits:**
- one bounce per task;
- no bounce if the worker session is not live/resumable;
- no bounce on the last round before `round_cap`;
- no bounce when the Case is quota/transient-paused;
- kill switch is the flag.

**Acceptance (A96):**
- **M1** (Manager wake turns per accepted delivery) down ≥ 25% in the act arm;
- **wrong-bounce rate ≤ 5%** (bounced, and the rework changed nothing material, then the Manager
  accepted it);
- zero duplicate or lost wakes in recovery tests (crash between bounce event and membership swap must
  replay safely; idempotency key = `decision_id`).

---

## 7. Measurement: before, after, and stop rules

**Baseline ("before").** The replay over a read-only copy of the controller DB (§5.6) produces each
battery's baseline before anything ships. This is why A94 builds replay first.

**Scoreboard.** Computed from existing tables plus `system_one_decisions`, never from new telemetry.

| # | Metric | Definition / source | Target | Stop rule |
|---|---|---|---|---|
| M1 | Manager wake turns per accepted delivery | Delivered continuation tokens (`case_continuation_delivered` / `cont:*` rows) ÷ `review.accepted` count, per arm | −25% after Move 3 | Move 3 back to shadow if not met in 4 weeks |
| M2 | Rework rounds before acceptance | Worker tasks between dispatch and `review.accepted` per worker objective | −20% (annotate + pre-flight) | — |
| M3 | Delivery gate quality | AUROC of `critical` vs Manager verdict; precision/recall at thresholds | AUROC ≥ 0.75 and ≥ 0.10 above the code baseline | Two failed question-rewrite rounds → stop the delivery line |
| M4 | Manager $ per closed Case | `/api/cases/{id}/usage` manager share (cost read-model) | −15% | — |
| M5 | Post-acceptance defect proxy | Accepted task followed within the Case by a fix-type dispatch touching ≥ 1 of the same files (`file_changes_json` overlap) | −30% in the annotate arm | — |
| M6 | Pre-flight separation | Rework rate of flagged vs unflagged envelopes | ≥ 2× | Not met after 60 dispatches → stop Move 2 |
| M7 | Wrong-bounce rate | §6.3 | ≤ 5% | Exceeded → Move 3 back to shadow automatically (flag off + CONTEXT note) |
| M8 | Cost / latency / fail-open | `system_one_decisions` sums, p95, `verdict='error'` share | < $5/month; p95 < 1.5 s; errors < 5% | Errors > 20% for 24 h → investigate (fail-open keeps harness safe) |

**Statistics:**
- Report with bootstrap 95% confidence intervals.
- No arm verdict below 100 reviewed deliveries per arm. If volume is low, extend the window rather
  than lowering the bar.
- Label drift: re-fit thresholds monthly. A shift greater than 0.05 in optimal `T_ACT` is a battery
  version bump.

---

## 8. Governor programme integration (S1 slots)

GOVERNOR_OUTER_LOOP_V1 leaves exactly the holes S1 fills. Each slot below becomes a battery on the
same core when its G-job is built. **No slot adds infrastructure.**

| G-job | Slot (battery) | Spec anchor | Labels / proof |
|---|---|---|---|
| **G1** | `delivery` (Move 1/3) = relevance / rigor / integration | §13.3 | Manager verdicts (§5.6) |
| G1 | `integration`: per prior Case fact, "does this result supersede or contradict fact F?" | §13.3 integration | Later `review.*` + adversary outcomes |
| G1 | `claim_class_check`: S1 re-classifies each claim (MECHANICAL_FACT / SCIENTIFIC_FINDING / CAUSAL_INTERPRETATION / DECISION / UNRESOLVED); disagreement with the Manager's label = "claim may exceed evidence" flag | §13.4, §7.2 | Adversary DOWNGRADE/INVALID outcomes |
| G1 | `scope_drift`: per Manager turn, "does this action broaden or redefine `objective`?" If above threshold and no Manager escalation exists, the harness synthesizes `manager_case.escalated` (a Governor wake source, §22) | §13.5 | Owner corrections labelled "strategy" |
| G1 | `closure`: per DoD item, "is it covered?"; "uncertainty mentioned but missing from UNRESOLVED?" | §13.6, §14 | Reopened Cases |
| G1 | **Structured worker report block** (OUTCOME / CLAIMS with class and evidence ref / ROOT CAUSE / CHANGES / ANOMALIES / OPEN QUESTIONS) in `worker.md`. This lets the logic-link questions point at named fields. | §13.4, §14 | — |
| G2 | **none**. Structural. | §6, §11 | — |
| **G3** | S1 arms in the benchmark: semantic known-failure guards (one pinned battery per historical lesson) and the trigger policy below; line-id Choice for **localization accuracy**; S1-assisted scorer "is finding F the same defect as lesson S?" (calibrated against hand labels) | §16 | Benchmark ground truth |
| **G4** | `adversary_trigger`: one noul per §15.3 trigger (surprising result, semantic-object substitution, affects multiple branches, major architecture choice, closure with load-bearing claims, …); invoke the Adversary at max ≥ threshold fitted for recall at a fixed invocation rate. **This is the proposed answer to §34 "Exact risk classifier that triggers Adversary".** | §15.3, §34 | G3 benchmark (target: ≥ 90% critical-defect recall at ≤ 30% invocation rate) |
| G4 | `attack_routing`: two-stage shortlist over the 16 attack classes (§15.5); the Adversary attacks the top 3 first | §15.5 | Adversary noise/redundancy and localization, with vs without |
| G4 | `claim_packet_screen`: "does OBJECT_ACTUALLY_MEASURED name the same object as CLAIM?" | §15.4 | Benchmark substitution cases |
| **G5** | `wake_materiality`: runs **after** the deterministic router steps; "is this a meaningful change for campaign C?" | §10.3, inv. 8 | **Stage-0 self-labelling** (below) |
| G5 | `information_gain`: "does this result contain a finding absent from prior results?"; code counts zero-information streaks → PARK/ESCALATE | §7.5 #7, §18, §24 | Governor decisions at Stage 0 |
| G5 | `activation_tier`: high- vs low-leverage wake → Governor model/thinking tier | §19 | Decisions changed by tier (Stage-0 ablation) |
| G5 | `context_rank`: rank frontier items and refs for the dynamic context | §9.2 | Governor retrieval misses |
| G6 | **none**. Fail-closed code. | §20 | — |
| X1–X3 | Only after the owner rules on egress for project content (§10 R-1) | §21 | — |

**The Stage-0 self-labelling trick (G5).** At autonomy Stage 0 (GOVERNOR §27, "observe") the Governor
only recommends. Run it on **every** routed wake. Each activation labels itself: a decision other than
NOOP/WAIT means useful. That yields materiality-filter precision (target ≥ 0.95 on skipped wakes) and
activations avoided per resolved objective, for free, before any filtering ships.

**Governor vs Manager evaluation (§26, invariant 23)** is attribution over the decision log plus the
ledger:
- an Adversary INVALID/DOWNGRADE on a claim the Manager accepted is a *Manager* miss;
- a Governor that continued a branch whose premise was invalidated is a *Governor* miss;
- owner corrections are classified (by an S1 battery) as execution, logic or strategy, and attributed
  to the layer that should have caught them.

**Design conflicts raised for the owner** (also recorded in GOVERNOR §37):
- **DC-1 (invariant 9 / §6.2 / §10.3).** An S1 materiality check *is* inference, about 1/5000th the
  cost of a Governor activation. *Proposed clarification:* "inference" in invariant 9 means System-Two
  activations. S1 checks are allowed only on events that passed deterministic routing, are metered as a
  separate line in the G5 activation budget, and never run on parked/waiting branches without a new
  event.
- **DC-2 (invariant 13 "deterministic guards").** S1 semantic guards are calibrated, not
  deterministic. *Proposed:* a distinct trust-stack tier ("semantic guards") between deterministic
  guards and the evaluator/Adversary, with pinned model and threshold recorded per decision. Never the
  sole authority for a BLOCKER.
- **DC-3 (§4 ownership, X1).** Project research content to a third-party API. *Default:* forbidden
  until ruled.

---

## 9. Fit with the currently operating state (as of 2026-10-05)

| Live fact | Consequence for S1 |
|---|---|
| A82 Stage 8a (managed turns) is **held** (PR #185); legacy turns are live; no sessions enrolled | Move 1 hooks **both** producers; the decision cache makes the managed path deterministic on replay. No dependency on the cutover. |
| A84 completion-effects consumer exists for managed completions only | Not used by Moves 1–3. A future per-turn battery (G1 `scope_drift`) may use it once Stage 8 is live, or the legacy completion path before that. |
| Controller runs in Docker; gateway restart is delegated; **worker restarts are operator-gated** | Moves 1 and 3 are gateway-only (rebuild + restart). Move 2's `mcp_manager` change rides node checkouts, with no daemon restart. Nothing in S1 requires a worker restart. |
| Pi host with a slow USB disk; history of SQLite write-lock stalls (PRs #135–#147) | One tiny insert per evaluated subject, off the loop. No background scans. Reads are per-Case point queries. |
| Migration 43 is reserved by held PR #185 | S1 takes the next free number at merge; rebase if #185 lands first. |
| `CASE_CONTINUATION_ENABLED` and related autonomy flags are operator decisions | S1 flags are separate and default OFF. Annotate changes no autonomy; act (A95/A96) is Level 3 and needs operator approval per `level_rubric.md`. |
| Telegram is notification-only; push is info pings | S1 adds no approval surfaces. An optional push on Move 2 holds is out of scope (§10). |

**Level:** A94, A95 and A96 are all **Level 3** (`docs/harness/level_rubric.md`): a DB migration, a
secret, and an agent-behaviour change. They need an adversarial plan review and operator approval
before execution.

---

## 10. Non-goals and reserved decisions

**Non-goals (do not build under S1 without a new decision):**
- Jev in error classification (A97 fixes the real bug in code).
- Model or backend selection by classifier.
- Session compaction or session hygiene.
- Telegram intent parsing.
- Tool-call permission gates.
- Best-of-N.
- A continuous S1 process or any S1 background scanning.
- Training classical models (CatBoost etc.) before ≥ 300 labels.
- Storing state text in the decision log.
- Any S1 authority to accept, close, merge, deploy or decide direction.

**Reserved for the owner:**
- **R-1.** Egress of project-domain content (`tokens_ingest`) to TypeSafe. Default: **no.**
- **R-2.** Moving to a TypeSafe enterprise/ZDR plan if volume or sensitivity grows. Default: no.
- **R-3.** Ruling on DC-1/DC-2 in GOVERNOR §37. Default: the proposals in §8 apply provisionally,
  flagged.
- **R-4.** Promotion of each move from shadow → annotate → act. The operator approves Level-3 act
  steps; the measured bars in §7 are a necessary condition.

---

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Thresholds don't transfer from replay to live (distribution shift) | Shadow period with agreement check (§6.3); monthly re-fit; battery version bumps |
| A confident wrong scorecard persuades the Manager (the skill-suggestion cookbook observed this) | Fixed advisory wording; Gate-0/diff verification remains mandatory in `manager.md`; M5 monitors post-acceptance defects per arm |
| The model alias moves and answers change | Pin `jev-1.13.0`; record the reported model; a new version is a re-replay |
| Vendor availability or rate-limit changes | Fail-open (P6); M8 error monitoring |
| Worker reports written to game the scorecard (agents learn the questions) | Questions are not shown to workers; the scorecard is shown to the Manager only; review the questions quarterly |
| Too few labels | §5.6 fallback to hand labels; no promotion under 100 |
| Egress of sensitive snippets | P14 redaction; harness-generic content only; R-1 |

---

## Appendix A: question-writing guide (with our own examples)

- **Good:** "Does `worker_report` claim the task is complete without citing observable proof such as
  test output, command results, or commit ids?" It is literal, one hop and names a field, and *true*
  means problem.
- **Bad:** "Is this delivery good?" It is vague and gives mushy, uncalibrated scores. This is the SDE
  cookbook's warning.
- **Bad:** "Did the worker change more than 5 files?" That's counting, so do it in code.
- **Bad:** "Is the report not missing evidence?" A double negative is a documented weak spot.
- **Bad:** "Was the test run after the last edit?" That's ordering in time, so do it in code.
- **Per-item beats holistic.** One noul per acceptance bullet, aggregated with `max`, localizes the
  failure and stays sparse.
- When a replay shows a wrong answer, write down why it's wrong. That sentence goes into `criteria`.

## Appendix B: glossary

- **S1 / System One:** fast, typed, calibrated judgment, the Jev layer.
- **System Two:** LLM roles (Manager, Governor, Adversary) that reason and decide.
- **Battery:** a named, versioned set of pure functions plus questions, evaluated in one Jev request.
- **Noul:** P(statement is true).
- **Max-gate:** flag if any problem-noul exceeds its threshold.
- **Arm:** the A/B treatment actually applied to a Case: shadow, annotate or act.
- **Replay:** running a battery over a read-only DB copy to measure it against historical verdicts.
