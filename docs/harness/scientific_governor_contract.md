# Scientific Governor — dispatch contract

This document specifies how a **manager** engages the **scientific governor** role
(`docs/harness/roles/scientific_governor.md`) to review a research-programme milestone before
closing it. It defines the CONTEXT PACKET the governor must receive and the completion lifecycle.

## How the governor is dispatched (harness reality)

The harness `_role_boot` path (`src/backends/claude_driver.py`) promotes only two live tiers:
`manager` (by `case_role`) and `worker` (by `role_boot='worker'`, threaded from
`dispatch_worker(role='worker')`). There is **no** generic role-string → prompt mapping. The
governor is therefore dispatched as a **worker** whose stable stance is the governor role doc,
delivered in the manager-composed **dispatch envelope** (the worker's first assignment turn) — the
same convention `docs/harness/roles/manager.md` describes for all worker tasks.

Concretely, the manager composes the envelope so its CONTEXT section carries (or references) the
governor role doc and the full context packet below, then calls `dispatch_worker(...)` for a
review-only task. `src.core.roles.load_scientific_governor_role()` exists to load that doc
verbatim and keep the role first-class and testable; it is not auto-loaded by `_role_boot`.

## The CONTEXT PACKET (independently assembled — NOT the manager's prose)

The governor reviews **evidence**, not the manager's narration. The manager must assemble and pass
these fields independently; a packet missing a field means the governor cannot judge the questions
that field feeds, and must say so rather than infer it.

| # | Field | Feeds question | Why it is required |
|---|-------|----------------|--------------------|
| 1 | **Programme objective + roadmap** | Alignment, Continuity | Fixes the far horizon and where this milestone sits; without it the governor cannot tell drift from progress. |
| 2 | **Accepted evidence / RESUME** | Validity, Continuity | What prior knowledge must survive; lets the governor detect a result that silently contradicts accepted findings. |
| 3 | **Current milestone contract** | Alignment, Future simulation | The exact target + acceptance criteria this result claims to meet. |
| 4 | **Manager's proposed conclusion** | Evidence | The claim under review, stated as the manager intends to close it. |
| 5 | **Actual evidence references** | Validity, Evidence | Pointers to the real artifacts (commits, run outputs, datasets, figures) — NOT a summary. The governor grounds in these, never in prose. |
| 6 | **Protected-dataset + resource constraints** | Validity, Decision | Which data is the protected holdout vs. development data, and the compute/time budget — so the governor can price the cheapest-useful next action and catch development-evidence-as-confirmation. |

If any field is absent, the governor names it and states what it could not judge (see the role
doc's "Operating inside the project" clause).

## Completion lifecycle

```
manager proposes completion
      │
      ▼
governor reviews  ── one pass ──▶ structured review:
      │                           six answers · findings (BLOCKER/MATERIAL/RESIDUAL/OPPORTUNITY)
      │                           · decision · cheapest-useful next action
      ▼
manager adjudicates
      ├─ accept / accept-with-qualifications ─────────────▶ manager closes
      ├─ justified rework (BLOCKER or decision-relevant MATERIAL only)
      │        │
      │        ▼
      │   governor verifies resolved material defects  ── one pass ──▶ manager closes
      │
      └─ evidence-based rejection ───────────────────────▶ manager redirects / reframes milestone
```

### Caps and rules

- **Normally ONE review pass + ONE verification pass.** The verification pass checks only that the
  BLOCKER / decision-relevant MATERIAL findings from the first pass are resolved — it is not a
  fresh full review.
- **Escalate** to the operator only for a **genuine unresolved scientific BLOCKER** — not for
  RESIDUAL uncertainty or an OPPORTUNITY.
- **No reviewer-of-reviewer recursion.** The governor never reviews another governor's review, and
  the manager does not dispatch a second governor to adjudicate the first.
- **Only BLOCKER and decision-relevant MATERIAL justify rework.** RESIDUAL findings are recorded;
  OPPORTUNITY findings are advisory. The manager, not the governor, decides closure.

### Metrics

Each review + verification appends a record to the governor metrics ledger
(`scripts/oracle/governor_metrics.py`, schema in `scripts/oracle/governor_metrics_schema.json`):
reviews performed, blocker/material counts, findings accepted/rebutted/deferred, rework completed
pre-closure, review latency + model consumption, and any escaped defect found after closure. This
makes the governor's own value — did it catch real defects without false blockers — auditable over
time.
