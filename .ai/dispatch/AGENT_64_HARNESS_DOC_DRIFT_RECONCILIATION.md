# DISPATCH — A64 · Harness docs: reconcile the abandoned XML-packet ritual against actual practice

**Level:** 1 (docs-only, no code) · **Type:** docs
**Authored:** 2026-08-01 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (this session already did the zero-ambiguity part: deleted
`docs/harness/manager_invocation.md` and `docs/harness/promotion_ladder.md`, both self-marked
retired with zero code references; fixed the blockquote-leak-into-Manager/Worker-system-prompt bug;
updated `docs/INDEX.md`/`docs/harness/README.md`/`.ai/CONTEXT.md` pointers. This job is the
harder, still-open half.)

> **Read this first — why this packet exists.** While auditing `docs/harness/roles/manager.md` for
> content that doesn't belong in a Manager's live system prompt (2026-08-01), the operator asked for
> a full sweep of `docs/harness/` for anything that needs retiring. That sweep found real, evidenced
> drift that's bigger than a quick fix: `docs/harness/packet_template.xml` and the three
> `docs/harness/generators/*.md` files describe an XML-packet DRAFT/REVIEW ritual — fill
> `<task_packet>`, paste a generator prompt to review it, etc. — that was used through
> `.ai/dispatch/AGENT_16_*.md`–`AGENT_29_*.md` (mid-July) and then **abandoned without a trace**:
> zero dispatch doc from `AGENT_36` onward (`grep -L '<task_packet>' .ai/dispatch/AGENT_*.md`,
> verified 2026-08-01) uses the XML shape. Every recent packet (including this session's
> `AGENT_61`–`AGENT_63`) uses a free-prose house style instead (`Why` / `TASK` / `TYPE` / `CONTEXT`
> / `ACCEPTANCE` / `RESERVED DECISIONS` / `SCOPE OUT` / `TRAIL` sections + `## Milestone` + `##
> Closure`, one file per the ONE-FILE RULE). Nobody ever wrote down that this happened. This job is
> that write-down + reconciliation — a doc-hygiene job, not a process redesign.

## Why (intent)
`docs/harness/dispatch_pipeline.md` — the file `DISPATCH_LOG.md` itself still cites as authoritative
for the ONE-FILE RULE, and the file a fresh executor is told will let them "run a small task from
this file alone" — points its DRAFT and REVIEW steps at artifacts (`packet_template.xml`,
`generators/draft_packet.md`) nobody actually uses anymore. A fresh reader following it literally
would produce an XML packet that doesn't match any real recent example. That's a real onboarding
trap, not a cosmetic issue.

## TASK
1. **Confirm the drift is total, not partial.** Re-run
   `grep -l '<task_packet>' .ai/dispatch/AGENT_*.md` and confirm the cutoff point (this session
   found it's clean after `AGENT_29`; verify `AGENT_30`–`AGENT_35` too, they weren't checked). Read
   5–6 representative recent packets (`AGENT_50`, `AGENT_55`, `AGENT_60`, `AGENT_61`, `AGENT_62`,
   `AGENT_63`) and extract the ACTUAL current house style as a concrete template — section names,
   order, what's mandatory vs optional. This becomes the replacement for `packet_template.xml`.
2. **Decide, per artifact, retire vs rewrite-to-match-reality** (do not default to one answer):
   - `packet_template.xml` — likely rewrite as a markdown template matching the extracted house
     style (or retire the file and instead point directly at a recent real example — decide which
     is more useful to a fresh executor).
   - `generators/draft_packet.md` — the DRAFT generator prompt; update to produce the current shape,
     not the XML one.
   - `generators/adversarial_review.md` — the F-tag convention IS still followed in spirit (P0/P1,
     ≤2 rounds) even without the formal XML review step; check a couple of recent packets for
     evidence of it, keep what's true, fix what's not.
   - `generators/closure_summary.md` — spot-check against 2–3 real `## Closure` sections; likely
     still broadly accurate, confirm rather than assume.
   - `milestone_template.md` — compare its field shape (Objective/Current Status/Burndown/Live
     Log/Blockers/Next Action) against real `## Milestone` sections in recent packets (which use a
     simpler checkbox list). Reconcile to one true shape.
3. **Fix `dispatch_pipeline.md`'s DRAFT/REVIEW step pointers** (currently § "1. DRAFT" and "2.
   REVIEW", plus the worked example in § "Worked example") to reference whatever this job lands as
   the current artifacts — do not leave it pointing at retired/rewritten files under stale names.
4. **Check the remaining unaudited harness docs for the same kind of drift** (this session did not
   deep-read these): `docs/harness/operating_model.md`, `docs/harness/loop_config_map.md`,
   `docs/harness/FLOW_MAP.md`. Each was last touched 2026-07-03/07-09 (a month of silence while
   substantial M2/M3/M3.4 work happened) — confirm whether their content still matches how the
   Manager role loop actually runs today, or whether they too describe the pre-Manager-role
   paste-workflow. Flag/fix per the same retire-vs-rewrite judgment as above.
5. **Do not touch historical dispatch docs** (`.ai/dispatch/AGENT_*.md`, `.ai/dispatch/deferred/*`)
   — they are point-in-time records and stay as-is even where they reference now-retired files by
   name (that's accurate history, not a dangling pointer, per this session's own precedent).
6. **Update `docs/harness/README.md`'s "Which file to use when" table** — remove the ⚠️ drift-note
   caveats this session added once the underlying files are reconciled (or keep/adjust them if a
   file is intentionally retired rather than rewritten).
7. **Update `docs/INDEX.md`** rows for whichever files change status (🟡 → 🟢 if rewritten-and-
   current, or removed if retired).

## TYPE
docs (Level 1). Docs-only ⇒ commits straight to `main` per branch policy — no `feat/` branch needed
unless the audit surfaces something that turns out to need a code check too (unlikely; flag and
re-scope if so, don't silently expand into code changes).

## CONTEXT (reuse verbatim)
- What this session already fixed (don't redo): `docs/harness/roles/manager.md` +
  `docs/harness/roles/worker.md` blockquote-leak fix (meta-doc relocated to `src/core/roles.py`
  comments near `_MANAGER_ROLE_DOC`/`_WORKER_ROLE_DOC`); `manager_invocation.md` +
  `promotion_ladder.md` deleted; `docs/INDEX.md`, `docs/harness/README.md`, `.ai/CONTEXT.md`
  pointers fixed for those two deletions.
- The abandoned ritual: `docs/harness/packet_template.xml`, `docs/harness/generators/draft_packet.md`,
  `docs/harness/generators/adversarial_review.md`, `docs/harness/generators/closure_summary.md`,
  `docs/harness/milestone_template.md`.
- Still-confirmed-live doctrine (do not retire): `docs/harness/dispatch_pipeline.md`'s ONE-FILE
  RULE + 7-stage doctrine + Level-3 admission-guard section (real code:
  `orchestrator.py::_harness_level3_allows_autopickup`, `HARNESS_LEVEL3_GUARD` flag,
  `tests/test_harness_level3_guard.py`); `docs/harness/level_rubric.md`'s Level 0–3 ladder (every
  recent packet cites `**Level:** N`).
- Reference for the extraction task: any of `.ai/dispatch/AGENT_50_*.md` through
  `AGENT_63_*.md` as real, current-shape examples.
- `.ai/DOC_MAP.md` — "ONE-FILE RULE" ownership; consult before deciding where any
  rewritten template artifact should live (`docs/harness/` for the reference artifact itself, never
  `.ai/dispatch/`).

## ACCEPTANCE (proof, not vibes)
1. Written confirmation of the exact drift cutoff (which AGENT_N first stopped using XML).
2. Extracted current house-style template, grounded in ≥5 real recent packets, not invented.
3. Every file in the "abandoned ritual" list has an explicit disposition: retired-and-removed,
   or rewritten-to-match-reality — no file left silently stale.
4. `dispatch_pipeline.md` DRAFT/REVIEW steps + worked example match whatever this job lands as
   current — a fresh executor following it literally now produces something that looks like a real
   recent `AGENT_N` packet.
5. `operating_model.md`/`loop_config_map.md`/`FLOW_MAP.md` explicitly checked against current
   practice, not assumed fine because they weren't flagged this session.
6. `docs/harness/README.md` + `docs/INDEX.md` reconciled, no leftover ⚠️ drift caveats pointing at
   now-fixed files.
7. No historical dispatch doc touched.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — rewrite vs retire, per file.** Don't default to "rewrite everything" or "retire
  everything" — some of these (e.g. `generators/closure_summary.md`) may already be accurate and
  need zero change; say so explicitly rather than touching files that don't need it.
- **R2 — how prescriptive the new template should be.** The current house style emerged
  organically across ~25 packets without a written spec. Capture what's actually common, don't
  invent structure nobody follows.

## SCOPE OUT
Any code change. Redesigning the dispatch process itself (this is reconciliation of docs to match
established practice, not a process redesign — if the audit reveals the practice itself has a real
gap, surface it as a separate finding, don't silently fix it here).

## TRAIL / EVIDENCE (fill at close)
- Drift-cutoff confirmation · extracted template + its ≥5 source packets · per-file disposition
  table · `dispatch_pipeline.md` diff · `operating_model.md`/`loop_config_map.md`/`FLOW_MAP.md`
  audit verdicts · README/INDEX reconciliation diff.

---
## Milestone (burndown)
- [ ] Drift cutoff confirmed (AGENT_30–35 checked, not just 16–29)
- [ ] Current house-style template extracted from ≥5 real packets
- [ ] Per-file disposition decided for all 5 "abandoned ritual" files (retire or rewrite)
- [ ] `dispatch_pipeline.md` DRAFT/REVIEW steps + worked example updated
- [ ] `operating_model.md`/`loop_config_map.md`/`FLOW_MAP.md` audited against current practice
- [ ] `docs/harness/README.md` + `docs/INDEX.md` reconciled

## Closure (fill on completion)
(fill when executed)
