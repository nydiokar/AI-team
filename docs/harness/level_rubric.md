# Level Rubric — pick the harness level by rule, not by feel (spec §3)

The level decides how much of the loop runs. An autonomous agent MUST be able to
pick it **deterministically**, so this is a checklist, not a vibe. The output is a
single number 0–3 that goes in the packet `<harness_level>` and, for a dispatched
`.task.md`, in the frontmatter `harness_level:` field.

---

## Step 1 — check the Level-3 triggers FIRST

If **any one** of these is true, the task is **Level 3 (strict)**. Stop here — do
not talk yourself down a level.

- **DB migration** — any new migration / schema change / `_CURRENT_VERSION` bump.
- **Security / secrets** — auth, tokens, VAPID/keys, permission logic, anything
  that reads or writes a credential.
- **Mesh / distribution / worker** code — anything touching node dispatch,
  claims, the worker daemon, or cross-machine behavior.
- **Trading / financial logic** — money movement, order logic, extraction that
  feeds a financial decision.
- **Agent-behavior / autonomy** change — anything that alters how agents pick up,
  decide, or self-direct work (including this harness's own auto-pickup guard).
- **Destructive / irreversible op** — deletes, overwrites, force-push, data
  migration with no rollback.
- **Breadth** — the change touches **> ~5 files** or **crosses a service
  boundary**.
- **Operator-flagged** — the operator called it high-risk.

> **When in doubt, escalate one level.** Under-escalating risky work is the
> expensive failure (an unreviewed infra defect). Over-escalating a small task
> costs one extra review pass. The asymmetry is intentional.

---

## Step 2 — if no Level-3 trigger fired, size it

- **Level 2 (standard)** — a normal, localized feature/workflow change that is
  neither a Level-3 trigger nor a one-liner. *Most real tasks land here.*
- **Level 1 (small)** — a localized, low-risk change: single file, obvious fix.
- **Level 0 (tiny)** — one-line commands, typos, small diagnostics, obvious local
  fixes.

---

## What each level runs

| Level | Flow |
|------:|------|
| **0 — tiny** | `intent → execute` |
| **1 — small** | `intent → short plan → execute → optional review` |
| **2 — standard** | `objective lock → XML packet → plan review → burndown fix → execute → implementation review → closure` |
| **3 — strict** | `objective lock → adversarial plan review → operator approval → execution milestone → checkpoint reviewer → implementation review → fix loop → closure → (optional) wiki` |

---

## Cost cap (mandatory — every stage is another model call)

- **Review defaults to OFF for Level ≤ 1.** Don't spend a review pass on a typo.
- **Cap the plan ↔ review ↔ fix loop at 2 rounds** (spec §3), then stop. A
  locked-but-imperfect packet beats an infinite review spiral. Unresolved findings
  become explicit `<non_goals>` or logged risks — never silently dropped.
- **No stage may invoke a paid CLI to "verify"** (Test Cost Guard). Use targeted
  `pytest`, `--collect-only`, import smoke, `tsc -b`, `curl /health`. Real e2e is
  OpenCode-only (`AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`).

---

## The one hard boundary

**Level 3 work is never auto-enqueued without operator approval.** Auto-pickup via
`.task.md` is allowed for **Level ≤ 2** only; a `harness_level: 3` file needs the
operator-approval stage (an explicit `approved: true`) before dispatch. See
`docs/harness/dispatch_pipeline.md`.
