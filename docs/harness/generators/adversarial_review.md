# REVIEW — adversarial pass over the packet → F-tagged findings

**Role:** Supervisor / Reviewer (spec §4 / §14 step 2). Runs on the **drafted
packet** (before execution) and, at checkpoints, on the **committed diff** (spec
§5). It is an adversary: it assumes the packet is wrong and tries to prove it.

**Input:** a filled `packet_template.xml` (plan review) OR a committed diff
(checkpoint review).
**Output:** F-tagged findings in the house style, feeding an inline FIX loop
**capped at 2 rounds**.

---

## Prompt

> You are running an adversarial review. Challenge the assumptions; find the ways
> this fails. Report **only P0/P1** — do not nitpick style.
>
> ```
> P0: correctness / security / data-loss / blocking failure
> P1: serious regression, broken validation, bad architecture drift,
>     scope violation (e.g. adds gateway state the spec forbids)
> ```
>
> For each finding emit an F-tag in the **house style** (from
> `.ai/dispatch/AGENT_8_OPERATOR_SIGNAL.md`):
>
> ```
> ### F<n> (<SEVERITY> — <one-line category>) — <one-line defect statement>
> **Failure scenario:** <concrete inputs/state → wrong output/crash>.
> **Resolution:** <the inline fix, or "logged risk" / "explicit non-goal">.
> ```
>
> Rules:
> - Each `[Fn]` is **stable** — keep its number across rounds so the fix log can
>   reference it.
> - Every finding needs a **concrete failure scenario**, not a worry.
> - Prefer findings that catch the recorded scars: overbatch + hallucinated
>   success; adding a migration/stage machine when the spec forbids it; a paid-CLI
>   "verify"; an unbounded review spiral; drift from `<real_objective>`.
> - If the packet is sound, return **zero findings** — an empty review is a valid
>   result, not a failure.

---

## The FIX loop (step 3) — capped at 2 rounds

1. Revise the packet **inline** against each `[Fn]` at the exact field/step it
   guards.
2. Re-review only what changed. **Stop after 2 rounds** (spec §3 cost cap) — a
   locked-but-imperfect packet beats an infinite spiral.
3. Any finding still unresolved after the cap becomes an **explicit `<non_goal>`**
   or a **logged risk** in the packet — it is **never silently dropped**.
4. The implementation log records each tag's outcome: `fixed` / `accepted` /
   `no change needed`.

## Guardrails

- **No model smoke here (F3).** The provider/model identity smoke (spec §9) is
  **provider-onboarding only** and cost-guarded — it does NOT belong in a per-task
  review. Putting it here risks a paid-CLI call on every task.
- **Implementation review = existing skills (F3).** At a checkpoint, review the
  **committed diff** with `/code-review` + `/security-review` — they cost nothing
  extra. Never live-tail a mutating working tree (F4): the executor commits, *then*
  the reviewer runs.
- **No parser (F6).** The packet is model-facing prose — do not write a validator
  for it.
