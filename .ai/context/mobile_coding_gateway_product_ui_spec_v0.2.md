# Mobile Coding Gateway — Product and UI Specification v0.2

**Status:** Approved design direction; implementation blueprint  
**Primary interface:** Smartphone, portrait-first  
**Secondary interface:** Desktop browser, functional but not a design constraint  
**Visual reference:** `dark_ui_app_interface_mockup.png`  
**Product type:** Private mobile gateway for controlling persistent coding sessions, asynchronous tasks, agents, and workers on remote target machines

---

## 0. Revision summary

Version 0.2 formalizes the following decisions:

- mobile is the invariant and source layout;
- the interface is a remote coding gateway, not a mobile IDE;
- sessions and tasks are separate first-class concepts;
- tasks remain visible inside their parent session and are also aggregated globally;
- open and closed sessions are explicitly separated;
- the session is a chronological event stream, not only a chat transcript;
- the approved dark mock establishes the initial visual direction;
- files, approvals, logs, and terminal access use progressive disclosure;
- persistence, reconnection, idempotency, and notification behavior are part of the product model.

---

# 1. Product definition

The Mobile Coding Gateway is a phone-first control surface for coding agents and workers executing on stationary computers, laptops, servers, or other target machines.

The phone does not perform the primary coding workload. It provides:

- persistent conversational control;
- task dispatch and supervision;
- remote target and workspace selection;
- progress and state visibility;
- approvals and intervention;
- file upload and artifact review;
- error and log inspection;
- optional terminal access;
- continuity across application closure and network interruption.

The interface is conversation-first but operationally structured. Chat is the primary command channel. It is not the entire product.

---

# 2. Product invariants

These constraints override later design preferences.

## 2.1 Mobile-first means mobile-source

All essential workflows must work at a narrow portrait viewport, beginning at 360 CSS pixels.

No essential workflow may require:

- side-by-side panes;
- hover;
- right-click;
- a hardware keyboard;
- precise pointer input;
- wide tables;
- permanently visible inspectors;
- desktop-scale density reduced mechanically onto a phone.

Desktop rendering may add space but must not define the information architecture.

## 2.2 Remote work is persistent

Closing the application must not imply stopping work.

Sessions and tasks may continue while:

- the browser is closed;
- the phone is locked;
- the application is backgrounded;
- the network changes;
- connectivity is lost;
- the operator opens another session.

The frontend must restore and reconcile authoritative state after returning.

## 2.3 A session is not a task

A **session** is a persistent conversational and execution context.

A **task** is a bounded unit of work created inside a session.

A session may contain many tasks. A task belongs to one parent session.

## 2.4 The session is a chronological event stream

The session timeline contains more than messages.

It may include:

- user instructions;
- assistant responses;
- task creation;
- task state changes;
- tool executions;
- approval requests;
- files;
- artifacts;
- warnings;
- errors;
- reconnect events;
- system notices.

Operational events must be structured interface objects rather than raw text dumps.

## 2.5 Tasks are globally observable

Tasks appear:

1. in chronological context inside their parent session; and
2. in the global Tasks tab as a cross-session operational projection.

The Tasks tab does not own separate task data. It is a filtered aggregate of task objects already associated with sessions.

## 2.6 Remote actions must be attributable

Consequential actions must identify:

- target machine;
- workspace or repository;
- session;
- task;
- requested operation;
- reversibility or risk;
- current connection confidence.

Destructive actions require deliberate confirmation.

## 2.7 Complex detail uses progressive disclosure

The mobile timeline shows concise summaries.

Details open through:

- expandable cards for small amounts of information;
- bottom sheets for quick decisions and selections;
- full-screen routes for diffs, files, logs, terminal sessions, and consequential approvals.

---

# 3. Core domain model

## 3.1 Target

A machine or execution environment capable of running workers.

Examples:

- workstation;
- laptop;
- server;
- container host;
- remote development environment.

Core target state:

```text
online
degraded
offline
unknown
```

## 3.2 Workspace

A repository, directory, project, or execution scope on a target.

A session should normally be scoped to one target and one workspace. Scope changes must be explicit and recorded in the timeline.

## 3.3 Session

A persistent operational conversation tied to coding context.

A session contains:

- messages;
- tasks;
- tool executions;
- approvals;
- artifacts;
- files;
- execution history;
- session metadata;
- target and workspace context.

### Session lifecycle

```text
open
closed
```

> **Reconciled with backend (`docs/FRONTEND_BACKEND_GAP.md`):** `archived` was
> dropped. `closed` already means "ended, out of the working list, still readable
> and resumable"; the session list hides bulk-closed sessions. A second hiding tier
> earns nothing at this scale.

### Open-session operational states

```text
idle
running
waiting_for_input
waiting_for_approval
failed_attention
```

`open` describes lifecycle. `running` or `waiting_for_approval` describes current operational state. These must not be conflated.

> **Reconciled with backend:** per-session `connection_unknown` was dropped —
> connection liveness is a property of the **target/node** (heartbeat), surfaced on
> the System screen, not an independent session state. `waiting_for_approval` is
> gated on the backend approval consumer (gap-doc Move H).

### Closed sessions

A closed session has ended as an active operational context but remains readable and resumable only through an explicit reopen or branch operation.

An archived session is retained but removed from ordinary working lists.

## 3.4 Task

A bounded asynchronous unit of work initiated in a session.

Examples:

- inspect repository structure;
- refactor event adapter;
- run tests;
- generate a patch;
- review build failure;
- upload an artifact;
- deploy to a target;
- produce a report.

### Task lifecycle

```text
queued
dispatching
running
waiting_for_input
waiting_for_approval
succeeded
failed
cancelled
connection_unknown
```

### Session-task relationship

```text
Session: Mobile gateway implementation

├── Task: inspect existing frontend contract
├── Task: create mobile event adapter
├── Task: update call sites
├── Task: run test suite
└── Task: generate diff artifact
```

Tasks should be created only for work that benefits from an independent lifecycle, progress, attention state, or result.

Small tool actions may remain tool executions within a larger task.

## 3.5 Tool execution — DROPPED (reconciled with backend)

> **Removed from the model.** A backend turn is a black box to this gateway: it
> dispatches a whole turn to a CLI agent (Claude Code / Codex / OpenCode) and gets
> back a result, not a stream of instrumented tool calls. The agent's own UI owns
> tool-level granularity. Instrumenting every internal tool call would fight the
> backend boundary for little operator value.
>
> **What replaces it:** turn-level *operational* events the backend already emits
> ("jobs": `task_received`, `mesh_dispatch`, `validated`, `summarized`, `retry`,
> `artifacts_written`…) provide the "where is the agent, what is it doing" signal,
> rendered as `SystemNotice` cards in the timeline. See `docs/FRONTEND_BACKEND_GAP.md` §6.

## 3.6 Approval

A deliberate operator decision required before a consequential operation.

An approval references:

- parent session;
- parent task;
- target;
- workspace;
- proposed action;
- affected resources;
- risk;
- reversibility;
- expiration or staleness status.

## 3.7 Artifact

A durable output created by work.

Examples:

- patch;
- diff;
- report;
- generated file;
- test result;
- archive;
- screenshot;
- structured plan.

---

# 4. Primary operating loops

## 4.1 Session control loop

```text
Open application
→ select or resume an open session
→ inspect recent events
→ send instruction, command, or file
→ task is created or execution begins
→ observe progress
→ approve, reject, stop, retry, or redirect
→ inspect output
→ leave
→ return to reconciled state
```

## 4.2 Cross-session supervision loop

```text
Open Tasks
→ see all work needing attention
→ select blocked, failed, or running task
→ jump to exact event in parent session
→ act
→ return to aggregate view
```

## 4.3 File review loop

```text
Receive artifact or file event
→ open full-screen preview
→ inspect file or unified diff
→ approve, reject, download, share, or return to session
```

---

# 5. Information architecture

The primary bottom navigation contains four destinations:

```text
Sessions | Tasks | Files | System
```

## 5.1 Sessions

Purpose: persistent context and continuity.

Contains:

- open sessions;
- sessions needing attention;
- recently active sessions;
- closed sessions;
- archived sessions;
- pinned sessions;
- session search;
- new-session creation.

## 5.2 Tasks

Purpose: cross-session operational awareness.

Contains:

- Needs attention;
- Running;
- Queued;
- Failed;
- Recently completed.

The Tasks tab answers:

> What is running, blocked, failed, or waiting for me anywhere in the system?

## 5.3 Files

Purpose: remote file and artifact access.

Contains:

- recent artifacts;
- recent generated files;
- phone uploads;
- remote workspace browser;
- file preview;
- diff review;
- download and share actions.

## 5.4 System

Purpose: operational configuration and health.

Contains:

- target machines;
- workers;
- connection state;
- active capabilities;
- notification settings;
- approval policies;
- security settings;
- diagnostics;
- application settings.

---

# 6. Navigation behavior

## 6.1 Bottom navigation

Bottom navigation is visible on root screens.

Inside a session, the persistent composer may occupy the lower region. Back navigation must remain obvious.

## 6.2 Deep links

Notifications and task cards must deep-link to:

- exact session;
- exact task;
- exact approval;
- exact artifact;
- exact failure or timeline event.

## 6.3 Back behavior

Back should follow a predictable stack:

```text
full-screen detail
→ parent session or root tab
→ previous root tab state
```

Back must not silently discard drafts, uploads, or unsubmitted approval decisions.

---

# 7. Screen specifications

# 7.1 Sessions screen

## Purpose

Reach relevant work within one or two taps.

## Structure

```text
Header
Target selector
Needs attention
Open sessions
Closed sessions
Bottom navigation
```

## Session grouping

### Needs attention

Open sessions containing:

- approval requests;
- user questions;
- failures;
- stale unknown state;
- disconnected execution requiring review.

This group appears first only when non-empty.

### Open sessions

Contains all active operational contexts.

Each row shows:

- session title;
- target;
- workspace or repository;
- operational status;
- last meaningful activity;
- unread or attention count;
- optional active-task summary.

Open sessions use full contrast and live status indicators.

### Closed sessions

Displayed in a separate, collapsed-by-default section.

Closed session rows use reduced visual emphasis and show:

- title;
- target and workspace;
- closed date;
- terminal status;
- reopen or branch action.

### Archived sessions

Reachable by filter or secondary route, not mixed into the default list.

## Session row states

Recommended labels:

```text
RUNNING
WAITING
NEEDS APPROVAL
FAILED
IDLE
STATE UNKNOWN
CLOSED
ARCHIVED
```

Avoid using `COMPLETED` as the normal lifecycle label for a session. Completion normally belongs to tasks. A session may instead be explicitly closed after its work ends.

## Cold start behavior

The default screen should surface:

1. attention-required sessions;
2. running sessions;
3. recently active open sessions;
4. closed-session access.

---

# 7.2 Active session screen

## Purpose

Act as the primary command and observation surface.

## Structure

```text
Compact sticky header
Chronological event stream
Persistent composer
```

## Header

Displays:

- back navigation;
- session title;
- target;
- workspace;
- current session operational state;
- connection state when degraded;
- overflow menu.

Tapping the secondary line opens Session Details.

## Timeline

The timeline is an ordered sequence of typed events.

Example:

```text
User instruction
Assistant response
Task started
Tool execution
Tool execution completed
Approval required
Approval resolved
Artifact generated
Task completed
```

Each task card may expand to show its child tool executions or open a dedicated task detail route.

## Event card rules

Every operational card must answer:

- what happened;
- current state;
- when;
- where;
- what action is available.

Cards should remain compact by default.

## Scroll behavior

- opening a session lands at the latest unread or latest meaningful event;
- new events append without unexpectedly moving the user when reviewing older content;
- a “Jump to latest” control appears when appropriate;
- scroll position is restored per session;
- streaming updates must not cause layout instability.

---

# 7.3 Composer

## Required controls

- attachment/action button;
- multiline instruction field;
- command access;
- send control;
- stop control during cancellable execution.

## Action sheet

The plus button opens:

- Upload file;
- Take photo;
- Select remote file;
- Insert command;
- Create explicit task;
- Change target or workspace;
- Open logs;
- Open terminal, when enabled.

## Draft behavior

- draft persists per session;
- closing or changing session does not destroy it;
- send is disabled when command delivery confidence is unsafe;
- unsent and queued states must be visually distinct.

## Commands

Slash commands may exist as accelerators, but commands must also be discoverable through a searchable sheet.

---

# 7.4 Tasks screen

## Purpose

Provide a global operational inbox across all sessions and targets.

## Sections

### Needs attention

Includes:

- waiting for approval;
- waiting for user input;
- failed tasks;
- stale connection-unknown tasks;
- blocked tasks.

### Running

Includes current progress, elapsed time, target, session, and latest meaningful action.

### Queued

Includes dispatch position or reason for waiting when available.

### Recently completed

Shows recent succeeded, failed, and cancelled tasks.

## Task card

Shows:

- objective;
- lifecycle state;
- target;
- parent session;
- elapsed or completed time;
- latest meaningful event;
- progress where reliable;
- primary action.

## Task actions

Examples:

- Review;
- Answer;
- Stop;
- Retry;
- View logs;
- Open artifact;
- Open session.

## Aggregation rule

The Tasks screen is a projection over the canonical task store.

Selecting a task opens its exact context in the parent session or a task-detail route linked back to that session.

---

# 7.5 Task detail

A task detail route may show:

- objective;
- current state;
- parent session;
- target and workspace;
- created, started, and updated times;
- task event history;
- child tool executions;
- approvals;
- logs;
- outputs and artifacts;
- stop, retry, or redirect controls.

This route is useful for long-running tasks but is not required to replace timeline cards.

---

# 7.6 Files screen

## Default views

- Recent artifacts;
- Recent files;
- Uploads;
- Browse workspace.

## Scope

The active target and workspace must be visible.

Changing scope requires an explicit selector.

## File preview

Supports:

- source text;
- plain text;
- markdown;
- images;
- structured result documents;
- binary metadata;
- download and share.

## Diff review

Mobile defaults to unified diff.

Capabilities:

- file-by-file navigation;
- changed-file summary;
- changed-line anchors;
- approve;
- reject;
- comment or redirect;
- open parent task or session.

Side-by-side diff is optional on wider screens.

---

# 7.7 Approval review

Low-risk approvals may use a bottom sheet.

Consequential approvals use a full-screen route.

Required content:

- action summary;
- target;
- workspace;
- session;
- task;
- affected files or resources;
- patch or command summary;
- risk;
- reversibility;
- stale-state warning;
- Approve;
- Reject;
- Modify instruction.

Approval actions must not be available through swipe.

---

# 7.8 Logs

The first release should include a full-screen log viewer.

Capabilities:

- live stream;
- follow or pause;
- jump to latest;
- search;
- severity filtering;
- copy;
- line wrapping toggle;
- reconnect status;
- target and task context.

---

# 7.9 Terminal

Interactive terminal is a specialist capability, not the default control model.

When enabled, it requires:

- explicit target and working-directory banner;
- full-screen route;
- landscape support;
- mobile modifier row;
- Ctrl, Alt, Esc, Tab, arrows, and interrupt controls;
- connection state;
- deliberate entry;
- destructive-command policy;
- session or audit association where possible.

Terminal support may be deferred after the log viewer.

---

# 7.10 System screen

Contains:

- target list and health;
- worker availability;
- active connections;
- notification settings;
- approval policy;
- local cache and storage;
- security;
- diagnostics;
- application version;
- disconnect and sign-out controls.

---

# 8. Visual system

The approved mock is the initial visual reference.

## 8.1 Direction

Use:

- near-black base;
- charcoal elevated surfaces;
- subtle borders;
- restrained blue-cyan accent;
- compact spacing;
- moderate corner radii;
- white and muted-gray typography;
- semantic green, amber, and red only for state;
- stable bottom navigation;
- compact Telegram-like timeline density.

Avoid:

- glassmorphism;
- ornamental gradients;
- large dashboard cards;
- desktop sidebars;
- oversized typography;
- permanently visible message action bars;
- excessive bubbles;
- ambiguous floating buttons;
- color-only meaning.

## 8.2 Semantic colors

Suggested roles:

```text
Accent / selected       blue-cyan
Running                 blue-cyan
Succeeded               green
Waiting / approval      amber
Failed / destructive    red
Idle / closed           neutral gray
Unknown / degraded      amber-gray
```

These are semantic roles, not fixed color values.

## 8.3 Baseline tokens

```css
:root {
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;

  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 12px;

  --touch-min: 44px;

  --surface-0: ...;
  --surface-1: ...;
  --surface-2: ...;

  --text-primary: ...;
  --text-secondary: ...;
  --text-muted: ...;

  --border-default: ...;
  --accent: ...;
  --success: ...;
  --warning: ...;
  --danger: ...;
}
```

## 8.4 Typography

- readable sans-serif for interface and conversation;
- monospace for code, paths, commands, logs, and identifiers;
- ordinary body text must remain readable without zoom;
- metadata must not become illegible to increase density.

## 8.5 Touch and ergonomics

- minimum interactive target: 44 × 44 CSS pixels;
- primary actions reachable one-handed;
- no critical hover-only behavior;
- long press may reveal contextual actions;
- destructive swipe actions are prohibited;
- safe-area insets are mandatory;
- virtual keyboard behavior must be tested explicitly.

---

# 9. Connectivity and state integrity

Mobile connectivity is assumed unstable.

## 9.1 Connection states

```text
online
reconnecting
offline
state_unknown
```

`state_unknown` means the last known execution state may be stale. It must not be displayed as failure or success.

## 9.2 Command delivery

Every mutation or command must have a client-generated idempotency identifier.

Required command states:

```text
draft
sending
acknowledged
queued
rejected
delivery_unknown
```

The UI must not show a command as accepted until backend acknowledgement is received.

## 9.3 Recovery

After reconnect:

1. restore local session view;
2. fetch authoritative session and task state;
3. deduplicate events;
4. reconcile optimistic state;
5. mark stale events;
6. resume live transport.

## 9.4 Local persistence

Persist locally where safe:

- session list cache;
- recent timeline cache;
- per-session drafts;
- upload state;
- scroll positions;
- pending idempotency keys;
- UI preferences.

Sensitive cache policy remains a security decision.

---

# 10. Notifications

Notifications are part of the core mobile model.

Priority events:

- approval required;
- user input required;
- task failed;
- task completed;
- worker disconnected during active work;
- target returned online;
- artifact ready.

Each notification must deep-link to exact context.

Preferences:

- all active work;
- attention only;
- failures only;
- mute session;
- mute target;
- quiet hours.

In-app notification support should precede or accompany push notifications.

---

# 11. Frontend architecture

## 11.1 Canonical event adapter

Backend payloads must pass through a translation layer.

```text
Backend protocol
→ frontend transport adapter
→ canonical gateway events
→ authoritative stores
→ presentation components
```

Backend-specific payloads must not leak directly into components.

## 11.2 Canonical events

```ts
type GatewayEvent =
  | { type: "target.connected"; targetId: string }
  | { type: "target.disconnected"; targetId: string }
  | { type: "session.created"; session: Session }
  | { type: "session.updated"; session: Session }
  | { type: "session.closed"; sessionId: string }
  | { type: "session.reopened"; sessionId: string }
  | { type: "message.created"; message: Message }
  | { type: "message.delta"; messageId: string; text: string }
  | { type: "message.completed"; messageId: string }
  | { type: "task.created"; task: Task }
  | { type: "task.state_changed"; taskId: string; state: TaskState }
  // task.progress and the tool.* family were DROPPED — a backend turn is atomic to
  // the gateway (black-box CLI). "Where is the agent / what is it doing" comes from
  // turn-level operational events (job events: task_received, mesh_dispatch,
  // validated, summarized, retry…) surfaced as SystemNotice, not per-tool streaming.
  | { type: "approval.required"; approval: ApprovalRequest }
  | { type: "approval.resolved"; approvalId: string; decision: string }
  | { type: "artifact.created"; artifact: Artifact }
  | { type: "file.changed"; file: RemoteFile }
  | { type: "run.cancelled"; runId: string }
  | { type: "connection.state_changed"; state: ConnectionState };
```

## 11.3 State separation

Keep separate:

- authoritative server state;
- live transport state;
- local presentation state;
- optimistic mutation state;
- drafts;
- uploads;
- cached previews.

## 11.4 Recommended implementation stack

Starting recommendation:

- React with TypeScript;
- Vite;
- React Router or TanStack Router;
- Tailwind CSS;
- shadcn/ui with accessible primitives;
- TanStack Query for server state;
- Zustand for local UI state;
- WebSocket or SSE transport adapter;
- IndexedDB through Dexie or equivalent;
- Storybook for isolated component states;
- Vitest for unit and component tests;
- Playwright with mobile device profiles;
- installable PWA after the core transport and recovery flow is stable.

A generic chat framework must not own:

- canonical session state;
- task semantics;
- persistence;
- transport recovery;
- approvals;
- artifact routing.

---

# 12. Component inventory

## Shell

- `MobileAppShell`
- `BottomNavigation`
- `CompactTopBar`
- `ConnectionBanner`
- `TargetSelector`
- `WorkspaceSelector`

## Sessions

- `SessionList`
- `SessionSection`
- `SessionRow`
- `SessionStatusChip`
- `SessionHeader`
- `SessionDetailsSheet`
- `NewSessionSheet`
- `CloseSessionDialog`
- `ReopenSessionAction`

## Timeline

- `SessionTimeline`
- `UserMessage`
- `AssistantMessage`
- `SystemNotice`
- `TaskEventCard`
- `ApprovalCard`
- `ArtifactCard`
- `FileEventCard`
- `ErrorCard`
- `RecoveryNotice`
- `JumpToLatest`

## Composer

- `SessionComposer`
- `AttachmentActionButton`
- `CommandSheet`
- `UploadQueue`
- `SendButton`
- `StopRunButton`
- `DraftIndicator`

## Tasks

- `TaskInbox`
- `TaskSection`
- `TaskCard`
- `TaskDetailRoute`
- `TaskProgress`
- `AttentionBadge`

## Files

- `FilesHome`
- `RemoteFileBrowser`
- `FilePreview`
- `DiffViewer`
- `ArtifactViewer`
- `FileActionSheet`

## Operations

- `ApprovalReview`
- `LogViewer`
- `TerminalRoute`
- `WorkerHealth`
- `TargetHealth`

---

# 13. First implementation slice

The first complete vertical slice must prove the real operating model:

```text
Launch application
→ restore authenticated target state
→ show open and closed sessions distinctly
→ open persistent session
→ render cached event stream
→ reconcile with server
→ send instruction
→ create or observe task
→ stream assistant and tool events
→ show task in parent session and global Tasks tab
→ require one approval
→ resolve approval
→ complete or fail task
→ display artifact or logs
→ close application
→ reopen
→ restore authoritative session and task state
```

Do not begin broad feature expansion until this path is reliable.

---

# 14. Delivery phases

> **Backend dependencies (`docs/FRONTEND_BACKEND_GAP.md`).** Each phase below now
> carries the backend move(s) it needs. Phases 0–1 have **no backend blocker** and
> can start immediately against the existing read API (`/api/sessions`, `/api/nodes`,
> `/api/events`). Phases 2+ are gated on backend moves F, I, H, G′ — build those
> first or the frontend is writing against fiction.

## Phase 0 — Domain and contract

**Backend dependency:** none. Tag every canonical type with its gap-doc status mark
(✅ PRESENT / 🟡 PARTIAL / ❌ MISSING). Do not emit types for ⛔-dropped concepts
(`tool.*`, `task.progress`, `archived`, per-session `connection_unknown`).

Produce:

- canonical TypeScript domain types;
- canonical event types;
- transport adapter contract;
- state transition tables;
- session fixtures;
- task fixtures;
- approval fixtures;
- failure and reconnect fixtures.

## Phase 1 — Mobile shell and mocked workflow

**Backend dependency:** none. Sessions bind to live `/api/sessions`; System target
list binds to live `/api/nodes`. Timeline + Tasks render from **fixtures** (🔵 MOCK-OK).

Build:

- bottom navigation;
- Sessions screen;
- open/closed grouping;
- active session timeline;
- composer;
- Tasks screen;
- mocked timeline (whole-message; no token streaming);
- approved dark visual system.

## Phase 2 — Real session and task state

**Backend dependency: Move F (write + WS/SSE surface) + Move I (canonical event
adapter).** Whole-message only — `message.delta` token streaming is post-v1, not
here. Task "creation and aggregation" uses the Move G′ task lifecycle when ready;
until then aggregate over `/api/tasks` flat rows.

Add:

- backend event adapter (snake→dotted translation, not pass-through);
- live transport (WS/SSE; poll is the safety net);
- task creation and aggregation;
- reconnect;
- idempotent commands;
- session restoration;
- stop and retry.

## Phase 3 — Attention workflows

**Backend dependency: Move H (approval object + queue + consumer + resolve
endpoint) + Move G′ (task lifecycle states).** Today approval events are emitted but
inert — nothing waits on them. This phase is the most backend-gated.

Add:

- approvals;
- user-input requests;
- failures;
- task deep links;
- notification center;
- in-app notifications.

## Phase 4 — Files and artifacts

**Backend dependency: artifact listing API** (artifacts exist on disk as
`results/<task_id>.json` + `last_artifact_path`; no listing/preview endpoint yet).

Add:

- uploads;
- artifact cards;
- previews;
- workspace browsing;
- unified diff review;
- download and share.

## Phase 5 — Operational depth

**Backend dependency:** target/worker health is ✅ PRESENT (`/api/nodes`). Log
**streaming** needs Move F transport; interactive terminal is a separate auth/transport build.

Add:

- target health;
- worker health;
- log viewer;
- diagnostics;
- optional interactive terminal.

## Phase 6 — Hardening and polish

Complete:

- accessibility;
- virtual keyboard behavior;
- screen-reader labels;
- reduced motion;
- offline and reconnect testing;
- visual regression tests;
- performance profiling;
- security review;
- PWA installability;
- push notifications where supported.

---

# 15. Acceptance criteria

The initial production-capable version must satisfy:

1. All primary workflows work at 360 CSS pixels.
2. Relevant open work is reachable within two taps after launch.
3. Open, closed, and archived sessions are visibly distinct.
4. Session lifecycle and operational state are not conflated.
5. Tasks appear inside their parent session and in the global Tasks view.
6. Selecting a global task returns to exact session context.
7. Closing the phone interface does not affect remote execution.
8. Reopening restores the authoritative state.
9. Network loss does not duplicate commands.
10. Delivery-unknown state is distinguishable from accepted or failed.
11. The target, workspace, session, task, and connection state are recoverable from visible context.
12. Approvals show target, consequences, and reversibility.
13. No essential action depends on hover, right-click, or hardware keyboard.
14. The composer remains usable with the virtual keyboard open.
15. Ordinary screens do not require horizontal scrolling.
16. Logs, code, diffs, and terminal content may scroll horizontally when unavoidable.
17. Notifications deep-link to exact operational context.
18. Cached data is clearly distinguished from confirmed current state.
19. Task failure and connection uncertainty are not conflated.
20. Desktop rendering remains functional without influencing mobile information architecture.

---

# 16. Initial non-goals

Exclude from the first release:

- full mobile IDE;
- rich collaborative editing;
- desktop dashboard shell;
- multi-pane code review;
- plugin marketplace;
- visual workflow builder;
- advanced analytics;
- consumer onboarding;
- social features;
- elaborate theming;
- unrestricted terminal-first operation;
- autonomous interface rearrangement.

---

# 17. Open decisions

These remain unresolved but do not block initial frontend work:

- authentication and trusted-device model;
- encryption and local-cache policy;
- whether a session may change target after creation;
- whether one task may span several targets;
- task creation rules: explicit, inferred, or both;
- session close versus archive semantics;
- branch and reopen behavior;
- default approval policy by operation type;
- notification transport;
- terminal authorization policy;
- log and artifact retention;
- PWA-only versus later native wrapper;
- offline command queue policy;
- conflict resolution for simultaneous control devices.

---

# 18. Reference rules

Use the approved mock as the visual baseline for:

- dark surfaces;
- compact cards;
- bottom navigation;
- status chips;
- session list density;
- active session timeline;
- task aggregation;
- semantic state colors.

Use Telegram selectively for:

- mobile density;
- timeline continuity;
- attachments;
- long-press actions;
- compact navigation;
- reliable one-handed interaction.

Use ChatGPT selectively for:

- persistent conversational sessions;
- streaming;
- stop and retry;
- artifact interaction.

Use operational monitoring tools conceptually for:

- attention queues;
- task lifecycle;
- failure visibility;
- progress;
- cross-session supervision.

Do not reproduce a desktop IDE inside the phone. The product is a mobile operational gateway into remote coding systems.
