# DISPATCH — <N> · <one-line theme>

Free-prose packet in the **current house style** — this is the shape every dispatch doc
(`.ai/dispatch/AGENT_N_*.md`) has used since `AGENT_31` (the XML shape used through
`AGENT_29`; see `docs/harness/README.md`). The sections below are
the **common core**, grounded in real recent packets (`AGENT_52/55/56/60/61/62/63/64`).
NOTHING parses this file — it is model-facing prose in a stable shape. Do not build a
validator for it. Fill the fields, keep the section order, and hand the result to the
executor. Copy this into the ONE dispatch doc `.ai/dispatch/AGENT_N_*.md` (ONE-FILE RULE)
with the DRAFT generator (`docs/harness/generators/draft_packet.md`).

```yaml
job_id: AGENT_N_<THEME>              # = the dispatch doc filename, minus .md
created_at: "<ISO timestamp>"        # CANONICAL — set once at dispatch, never change
status: ready                        # ready | active | blocked | done | dead
owner: ""
depends_on: []                       # AGENT_N ids this job waits on
results_ref: null                    # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                         # artifact paths that PROVE it ran (checked to exist)
updated_at: "<ISO timestamp>"
```

---

**Level:** <0–3 from `docs/harness/level_rubric.md`, with the trigger in parens> · **Type:** <docs | code | audit | test-only>
**Authored:** <date> · **Status of this packet:** ready (authored, not executed)
**Depends on:** <— or the AGENT_N ids + their status>
**Branch:** <`main` for docs-only; `feat/<slug>` + PR + self-merge for any `src/`/config change>

> **Read this first — why this packet exists.** The real-world trigger in 2–4 sentences:
> the scar, operator request, or pending gate that makes this job necessary, and what it
> will let a reader/executor trust afterwards. Recent precedent: `AGENT_63_*.md`,
> `AGENT_64_*.md`.

## Why (intent)
The OUTCOME in the world this job produces — a result, not a code change. If the literal
request and the real goal could diverge, state both here.

## TASK
1. Concrete, independently checkable steps. Prefer "edit file X to do Y" over "improve X".
2. … Each step names its non-paid verification (targeted `pytest`, `--collect-only`,
   import smoke, `tsc -b`, `curl http://127.0.0.1:9003/health` — never the paid CLI).

## TYPE
<docs | code | audit — repeat the branch rule; docs commit straight to `main`, code cuts a
`feat/<slug>` branch + PR + self-merge at close>

## CONTEXT (reuse verbatim)
The shared grounding, copied from the sources it cites (prior packets, `docs/` specs,
code seams with `file:line`). This is what the executor reuses so the packet doesn't have
to restate history. Never put reference doctrine here — that belongs in `docs/`.

## ACCEPTANCE (proof, not vibes)
1. The proof that closes this job — an artifact, a command's output, a reproducible check.
   Not "should work". If a proof is impossible, say what evidence replaces it.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — <the choice>. <What is being decided, who decides it, and the default if nobody
  does.>** Recent precedent: `AGENT_62_*.md`, `AGENT_64_*.md`.

## SCOPE OUT
What this task explicitly does NOT do. Unresolved review findings that won't be fixed land
here as explicit non-goals.

## TRAIL / EVIDENCE (fill at close)
- The artifact paths and verdicts this job will record at close.

---
## Milestone (burndown)
The `definition_of_done` as a checkable checkbox list. Tick as you go; on resume this +
`orchestrator.load_compact_context()` is ground truth, NOT model memory.

- [ ] <checkable item>
- [ ] …

## Closure (fill on completion)
Honest summary at close, per `docs/harness/generators/closure_summary.md`: what changed
(per file), verification commands + results, F-tag outcomes, what follows.
