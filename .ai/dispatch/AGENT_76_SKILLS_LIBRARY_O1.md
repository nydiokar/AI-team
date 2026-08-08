```yaml
job_id: AGENT_76_SKILLS_LIBRARY_O1
created_at: "2026-08-08T23:19:01.822222+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: worker:e3dba8b45092
depends_on: []
results_ref: DISPATCH_LOG.md → A76 row (done, PR #86)             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["docs/SKILLS_LIBRARY_O1.md", "skills/no-false-success.md", "skills/reuse-before-build.md", "skills/verify-claims-in-git.md", "scripts/mcp_manager.py", "tests/test_mcp_manager.py"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-08T23:32:56.356719+00:00"
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

## Milestone — delivered (2026-08-09, PR #86 merged to `main`)

**Mechanism decision: ship (A) reference-in-authoring, expanded on the wire; (B) is the follow-up.**
Grounded in the code: `dispatch_worker` (`scripts/mcp_manager.py`) sends the objective as the
`description` of `POST /api/instructions`, which is *carried over the wire* to the worker's first
turn on **any** node. Mechanism (A) resolves the referenced ids against the repo-local `skills/`
library **on the Manager side** (wherever `mcp_manager.py` runs — the gateway host / Manager's node,
which always has this repo checkout) and prepends the skill text to the objective. That reaches a
remote worker (Horse, separate filesystem) too, because the text travels rather than being resolved
node-side. Mechanism (B) — carry only ids, resolve worker-side for real wire/cache savings + prompt-
cache stabilisation (ADR-0001) — is the honest **follow-up**: it needs `skills/` provisioned on the
worker node, which is not true today. Token math + the A-vs-B table + the remote-node caveat are in
`docs/SKILLS_LIBRARY_O1.md`.

**What shipped**
- `skills/` — 3 seed skills (`no-false-success`, `reuse-before-build`, `verify-claims-in-git`)
  **extracted** from `docs/harness/roles/manager.md` + `worker.md` wording (mapping table in the doc).
- `dispatch_worker(skills=[…])` behind `SKILLS_LIBRARY_ENABLED` (default **OFF**). `_resolve_skills`
  applies the §7 boundary lens: bounded id list (≤16), charset+containment guard (no `../` traversal),
  **unknown id ⇒ structured error (never a silent drop)**, oversized (>8 KiB)/empty/non-UTF-8 file
  rejected early, resolution fails **before** any dispatch.
- Flag OFF ⇒ the `skills` param is ignored and the `POST /api/instructions` payload is
  **byte-identical** to today — proven by `test_dispatch_worker_flag_off_is_byte_identical`.
- `docs/SKILLS_LIBRARY_O1.md` (design + token math) and `docs/ENV_FEATURE_FLAGS.md` (flag registered).

## Closure

- **Verdict:** done. All acceptance criteria met against the merged tree.
- **Tests:** `.venv/bin/python -m pytest tests/test_mcp_manager.py` → **77 passed** (12 new O1 tests),
  run on the merged-`main` state. Targeted module only — paid-CLI cost guard respected.
- **PR:** #86 (`feat/skills-library-o1`) merged to `main`; `b4e4c80` is an ancestor of `origin/main`
  (verified — `skills/`, `_resolve_skills`, design doc, flag, and tests all present in `origin/main`).
- **Flag stays OFF.** Live activation (`SKILLS_LIBRARY_ENABLED=1`) and any gateway restart are a
  Manager/operator decision — not performed here.
- **What remains:** (1) live activation decision; (2) mechanism (B) once `skills/` is provisioned
  node-side (deploy or a boot-time fetch endpoint); (3) an activation-time edit to `manager.md`
  teaching the Manager to *prefer* `skills=[…]` over re-authored attitude prose (kept out so the
  flag-OFF world is unchanged).
