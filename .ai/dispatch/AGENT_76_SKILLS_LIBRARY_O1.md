```yaml
job_id: AGENT_76_SKILLS_LIBRARY_O1
created_at: "2026-08-08T23:19:01.822222+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: worker:e3dba8b45092
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-08T23:21:27.026650+00:00"
```

# DISPATCH — AGENT_76_SKILLS_LIBRARY_O1

**Level:** 3 (harness quality lever — touches the Manager MCP surface + worker boot) ·
**Type:** design + minimal flag-gated build.
**Status of this packet:** ready (authored, not executed)
**Depends on:** — (M3 open question **O1**, `docs/M3_MANAGER_INVOCATION_SPEC.md` §7; operator
green-lit the O1/O4 token-lane on Case `67b0ec8c…`).

> **Why this packet exists (O1).** The operator's model of the harness: the Manager is *invoked
> with inherited qualities* and *imbues each worker with the skills the task needs*. Today those
> "qualities" are re-authored as attitude prose in every dispatch envelope (e.g. "no false
> success", "reuse before build", "verify claims in git"). That is (a) repeated tokens on every
> dispatch — a direct token sink the operator wants cut — and (b) inconsistent, because the prose
> is retyped from memory each time. O1 asks: build a **skills library** — named, reusable
> professional attitudes/procedures the Manager attaches to a dispatch **by reference**, so the
> worker is imbued with a *stable, versioned* skill instead of ad-hoc prose.

## The token-lever question you MUST answer explicitly (do not skip)

There are two mechanisms, and they differ on whether O1 actually saves tokens:

- **(A) Reference-only-in-authoring, expanded on the wire.** The Manager writes `skills: [no-false-success]`;
  `dispatch_worker` expands the referenced skill text into the worker's objective/boot before it
  is sent. Saves the *Manager's* authoring tokens + guarantees consistency, but the worker still
  receives the full skill text every dispatch (no wire saving).
- **(B) Reference-carried, loaded worker-side on demand.** The dispatch payload carries only the
  skill *ids*; the worker resolves them against a local `skills/` library at boot. Saves wire +
  prompt tokens across dispatches **and** stabilizes the SDK prompt cache (ADR-0001) because the
  skill text is a static, cache-warm prefix rather than per-dispatch-varying prose.

**Decide which mechanism to build, with the token math stated.** (B) is the real token lever and
aligns with the persistent-SDK-session cache thesis, but only works when the worker's filesystem
has the library. Given workers can run on a **remote node** (Horse) whose filesystem is separate,
state honestly whether (B) is achievable now or whether the first shippable slice is (A) with (B)
as a follow-up once the library is provisioned node-side. **Do not hand-wave this — it is the
whole point of O1 as a token lever.**

## Task

1. **Ground first.** Read `docs/M3_MANAGER_INVOCATION_SPEC.md` §4/§7 (O1), `docs/harness/roles/manager.md`
   and `worker.md` (where the dispatch envelope + worker boot are defined), `scripts/mcp_manager.py`
   (`dispatch_worker` signature + how the objective reaches `POST /api/instructions`), and the
   worker boot path in `src/worker/agent.py` / the role-boot seam. Establish exactly where skill
   text could be injected on each mechanism.
2. **Design note** `docs/SKILLS_LIBRARY_O1.md` (concise, house prose — NOT a frenzy doc): the
   chosen mechanism (A or B) with the token math, the `skills/` storage shape (one file per named
   skill: id, one-line intent, the attitude/procedure prose), the reference syntax the Manager
   uses, the injection seam, the flag that gates it (default OFF ⇒ byte-identical), and the
   remote-node caveat. Seed **3** skills that already exist as repeated prose today:
   `no-false-success`, `reuse-before-build`, `verify-claims-in-git` (mine the real wording from
   `manager.md`/`worker.md` so this is extraction, not invention).
3. **Minimal flag-gated slice.** Implement the smallest reversible increment that proves a skill
   can be attached by reference and reaches the worker: an optional `skills: list[str]` param on
   `dispatch_worker` that resolves ids against `skills/` and injects per the chosen mechanism,
   behind a flag (e.g. `SKILLS_LIBRARY_ENABLED`, default OFF). Unknown skill id ⇒ structured
   error, never a silent drop. With the flag OFF the dispatch path is byte-identical to today.
4. **Apply the §7 service-boundary lens** to the resolver: bounded id list, bounded skill-file
   size, malformed/oversized skill file rejected early, missing file ⇒ structured error.

## Constraints / hard rules

- Branch policy: one `feat/<slug>` branch + PR + self-merge; minimal diff, no drive-by refactors.
- `pytest` on touched modules only — **NEVER** the full/e2e suite (paid CLI cost guard).
- Flag default OFF ⇒ byte-identical; prove it (a test asserting the OFF path is unchanged).
- Do not overreach into O2 (manager memory) or M4 (spec authoring) — skills-by-reference only.
- Reserved for the Manager/operator: whether to flip the flag ON live, and any gateway restart.

## Done when

- `docs/SKILLS_LIBRARY_O1.md` exists with the mechanism decision + token math + remote-node caveat.
- `skills/` holds the 3 seed skills, extracted from real role-prompt wording.
- `dispatch_worker` accepts `skills` by reference behind a default-OFF flag; unknown id ⇒ structured
  error; flag-OFF path proven byte-identical; targeted `pytest` green (report the exact command +
  counts).
- PR opened **and merged to `main`** by you; branch not left dangling. Do NOT restart the gateway
  to "activate" — the flag stays OFF; activation is a Manager/operator decision.
- Set `evidence:` to the test report + design doc + PR ref, `results_ref:` to the DISPATCH_LOG row,
  and `status: done`.
