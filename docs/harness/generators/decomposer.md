# DECOMPOSE — approved objective → task-DAG in ONE Case (M4)

**Role:** the decomposer front-half for a feature-sized Case (spec §M4 / A56),
resuming the intent of the stalled `AGENT_24_DECOMPOSER_GENERATOR.md` now that the
durable Case substrate (M2.5) is live. Where `draft_packet.md` turns one intent into
one packet, this turns one **approved** feature objective into **N dependency-linked
tasks** — a task-DAG — expressed as DATA on the existing Case. It salvages MAX's one
reusable pattern (`TaskExpertAgent`'s `intent → dependency-aware task list`) onto the
real substrate.

**Input:** a Case whose spec has a **passing** scored review (see `spec_authoring.md`),
plus the spec's `<tasks>` section.
**Output:** `decompose_case(case_id, spec_id, tasks)` → **N `task_attached`
flow_links on the SAME Case**, each carrying its objective + dependency edges on
`metadata_json`, plus one `case.decomposed` event. The Manager then dispatches
workers from the DAG in topological order.

---

## The one hard rule: ONE Case, ZERO orphan flow_runs

A decomposed feature is **N `task_attached` links on the ONE Case** — NOT N child
Cases and NOT N standalone `flow_runs`. Scattering orphan `flow_runs` is the exact
failure mode the entire M2.5 Case correction exists to prevent (§0.2 anti-goal). The
DAG is **data**: each task is a `flow_link` (`entity_type='task'`,
`role='task_attached'`, `entity_id=<task_key>`) whose `metadata_json` holds
`{objective, depends_on: [task_key,...], ...planning hints}`. `decompose_case` creates
**zero** new `flow_runs` — verify this holds if you ever touch the write path.

## Prompt (decomposition)

> You are decomposing an APPROVED feature objective into a task-DAG on this Case.
> The spec already passed its scored review — if it did not, STOP (the gateway will
> refuse with `spec_not_approved`; go back and fix + re-score the spec).
>
> From the spec's `<tasks>`, emit an ordered list of task nodes, each:
>
> ```
> {
>   "task_key": "t1",              # stable, unique within this Case
>   "objective": "<single checkable outcome>",
>   "depends_on": ["t0"],          # task_keys that MUST finish first (its prereqs)
>   "estimated_hours": 2,          # optional, non-authoritative planning hint
>   "human_task": false            # optional: needs a person, not an agent
> }
> ```
>
> Rules:
> - **Smallest set that covers the feature** — under-decompose over over-decompose;
>   each task has ONE `definition_of_done`.
> - **`depends_on` must be acyclic** and every edge must name a task_key in this same
>   list. A cycle is unschedulable — the gateway refuses `cyclic_dependencies`; an
>   edge to an unknown key refuses `unknown_dependency`; a duplicate/empty key refuses
>   `invalid_task_keys`. All refusals write nothing (all-or-nothing).
> - Call `decompose_case(case_id, spec_id, tasks)`. It returns the topological
>   `order` — dispatch a worker for each task as its dependencies complete, in that
>   order.

## Dispatching from the DAG

`decompose_case` delivers the DAG **as data + a valid dispatch order**; it does NOT
run the tasks. The Manager dispatches each `task_attached` node as a normal worker
(`dispatch_worker`) once that node's `depends_on` prerequisites have finished, walking
the returned topological `order`. **How tasks run in PARALLEL is out of scope here** —
that is the A57 intra-task-executor spike. A56 stops at the DAG-as-data + ordered
sequential dispatch.

## Provenance & guardrails

- Salvages the **decomposition** tier of the retired-MAX audit (its one reusable
  pattern) onto the M2.5 Case substrate; resumes `AGENT_24_DECOMPOSER_GENERATOR.md`.
- **No new schema.** The DAG rides existing `flow_links` + `metadata_json`; no new
  table, column, or lane.
- **No bespoke executor.** Deliver the DAG as data + order; the parallel executor is
  the A57 spike (scope-out).
- **Flag-gated.** Gated by `SPEC_AUTHORING_ENABLED` (OFF ⇒ the tool returns a disabled
  marker, the route 404s ⇒ byte-identical to pre-M4).
- **No paid CLI.** Decomposition is text over the already-approved spec.
