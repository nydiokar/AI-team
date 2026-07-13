# Role of cases, tasks, sessions, and milestones

Use these as separate concepts:

Case
  Durable coordination container around an objective or problem.

Task/dispatch
  One bounded unit of work inside the Session/case.

Session
  One runtime conversation with a Manager or worker.

Event
  Something that happened in a case, task, or session.

Artifact
  Dispatch packet, commit, report, spec, review, decision.

Milestone
  A planning objective or project phase.

# A practical relationship:

Project
  └── Milestone
       └── Case
            ├── Manager sessions
            ├── Tasks/dispatches
            │    └── Worker sessions
            ├── Events
            ├── Decisions
            └── Artifacts


# Hierchy of the modules 

**Gateway** owns progression and durable state.
**Manager** owns judgment and decisions.
**Skills** define repeatable management procedures.
**Tools** let the Manager act and retrieve evidence.
**System instructions** define the Manager’s stable role.
**The invocation** identifies the current case and operation.


# Exact separation

**Layer** | **What it defines**  |	**Example**

Role/system instructions |	Who the Manager is and what authority and obligations it always has	| “Own the case, ground intent, verify evidence, do not accept worker claims blindly”
Skill |	How to perform one repeatable management procedure |	review-worker-delivery, frame-next-dispatch
Tools |	What concrete actions the Manager can perform |	Query case, read task timeline, inspect commit, spawn worker
Project context |	What project rules, architecture, plans, and constraints currently apply |	operating_model.md, specs, DOC_MAP.md
Assignment/invocation |	What the Manager must handle now |	“Review Task T-42 in Case C-7 after worker completion”
Policy	| Rules that must be mechanically checked |	Level-3 approval, allowed transitions, closure requirements
Workflow/state machine	When each operation happens and what follows |	Worker completed → invoke review skill → iterate/close/derive
State |	What has actually happened	| Case, tasks, sessions, events, decisions, artifacts