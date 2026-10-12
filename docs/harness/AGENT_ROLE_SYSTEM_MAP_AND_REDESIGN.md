# Agent / Role System — Current Map & Redesign

**Status:** design doctrine (frozen snapshot + target). **Created:** 2026-10-10.
**Owner surface:** `docs/harness/` (per `.ai/DOC_MAP.md` — durable reference, outlives any one job).
**Why this exists:** the Governor programme, an advisory board, and per-programme governors are
imminent. Adding each of them the way agent types are added *today* means editing the boot path and
redeploying the gateway for every persona. This document freezes how the role system actually works
right now (grounded in git, with file:line), names the single design flaw that forces that cost, and
specifies the minimal-change path to make "add an agent / iterate its prompt" a no-restart file drop —
the `.claude/agents/*.md` experience — without re-implementing Claude Code.

> **Grounding note (honesty).** As of 2026-10-10, `git grep` + `git log --all` find **no**
> `scientific_governor` / `load_scientific_governor_role()` anywhere in the tree or on any branch.
> `src/core/roles.py` defines exactly two loaders: `load_manager_role` and `load_worker_role`. A prior
> agent reported "merged inert governor code"; that code does not exist. What actually ran was a plain
> `role='worker'` dispatch with governor behavior pasted into the first-turn envelope. The redesign
> below is written against the real tree, not that report.

---

## 1. The core diagnosis (read this first)

The harness uses **one word — "role" — for two independent concerns that change at different rates and
for different reasons:**

| Concern | What it governs | Cardinality | Should changing it cost a restart? |
|---|---|---|---|
| **Capability (RBAC)** | tool grant (`tool_profile` → MCP tool allowlist) + authority/membership semantics (who may dispatch, review, close a case, message whom) | **small, closed** (today: manager, worker; soon: reviewer/governor) | **Yes, acceptably** — it touches tool grants and auth branches |
| **Behavior (persona / agent-type / profile)** | system-prompt prose, declared skills, model, display identity ("scientific reviewer", "advisory board", "governor of program X") | **large, open, grows weekly** | **No — must be a file drop, no restart** |

Today these are welded together: a "role" is *simultaneously* a capability profile **and** a persona,
identified by a single hardcoded string. So a new persona (which should be free) drags in a new
capability wiring (which is expensive) — every new agent type pays the RBAC cost even when its
capability is identical to an existing role. That is the whole problem in one sentence.

**The fix is to split the two layers:** a closed set of **Roles** (capability, in code, restart-gated)
and an open set of **Profiles** (behavior, declarative files, hot-loaded). A Profile *names* a Role.
"Scientific reviewer", "advisory board", "governor of program X" become **Profiles over a `reviewer`/
`governor` Role**, added by dropping a file — never by editing the boot path.

---

## 2. Current system map (frozen snapshot)

### 2.1 The data model

- `AgentRoleDefinition` — `src/core/roles.py:80-91`. Fields: `role_id`, `system_instructions`,
  `declared_skills`, `tool_profile`, `output_contract`. Provider-neutral.
- Identity constants — `src/core/roles.py:21,61`: `MANAGER_ROLE_ID="manager"`,
  `WORKER_ROLE_ID="worker"`. **No registry, no enum, no `Literal`** — just module constants.
- Tool-profile constants — `src/core/roles.py:22,62`: `MANAGER_TOOL_PROFILE="manager_v1"`,
  `WORKER_TOOL_PROFILE="worker_v1"`.
- Prose files — `docs/harness/roles/manager.md`, `docs/harness/roles/worker.md`. **Pure prose, no
  frontmatter.** Loaded verbatim with `.read_text().strip()` (`roles.py:120-128`, `145-153`).

### 2.2 How a role becomes a running agent (the boot chain)

```
Manager dispatch (MCP)                     Session create                 Driver boot (per turn)
──────────────────────                     ──────────────                 ─────────────────────
dispatch_worker(role="worker")   ──────▶   POST /api/sessions    ──────▶  claude_driver._role_boot(session)
scripts/mcp_manager.py:304,364             {role_boot:"worker"}           src/backends/claude_driver.py:1813
  only "worker" sets role_boot             CreateSessionBody.role_boot      │
                                           control_api.py:510               ├─ case_role=="manager" → load_manager_role()
invoke_manager() for managers              sessions row: case_role,         │     roles.py:114  → manager_v1 tools
orchestrator.py:6851                       role_boot (INSERT-only)          ├─ role_boot=="worker" → load_worker_role()
  open_case(role="manager")                db.py:2020-2032                  │     roles.py:138  → worker_v1 tools (empty)
  stamps case_role="manager"                                               └─ else → (None, None)  # tier-0, legacy
```

- **Boot seam (the bottleneck):** `_role_boot` — `claude_driver.py:1813-1846`. It is a **hardcoded
  two-branch `if`**: `case_role == MANAGER_ROLE_ID` (1827) and `role_boot == WORKER_ROLE_ID` (1837),
  else `(None, None)`. **There is no generic `load_role(role_id)`.** Adding a third role means adding
  a branch here.
- **Tool grant:** `_session_allowed_tools(role=...)` — `claude_driver.py:549-576` — scopes tools by
  role when `MANAGER_ROLE_ENABLED` is ON. Profile→tools map `_PROFILE_TOOLS` —
  `src/backends/claude_role_adapter.py:26-45` (`manager_v1` → 15 `mcp__manager__*` tools; `worker_v1`
  → `[]`). System prompt assembly `claude_system_prompt()` — `claude_role_adapter.py:48-55` (appends
  prose to the `claude_code` preset).
- **Dispatch opt-in:** `_dispatch_worker` — `scripts/mcp_manager.py:289-385`. `role` arg accepted with
  **no enum/`Literal` validation** (304), but only `role == "worker"` threads `role_boot` (364,368).
  Any other value silently degrades to tier-0 (legacy, role-less) — exactly how the "governor" ran.
- **Enable gate:** `manager_role_enabled()` — `db.py:1170-1177`, runtime flag `MANAGER_ROLE_ENABLED`
  (default OFF). This flag *can* flip without restart, but only affects **new** session boots.

### 2.3 Everywhere "role" is checked (the dependency surface)

Two distinct kinds of check are tangled together under the same string literals:

**(A) Boot / capability resolution** — the legitimate "which prompt + tools" seam:
- `claude_driver.py:567,569` — profile selection by role in `_session_allowed_tools`.
- `claude_driver.py:1827,1837` — the two boot branches.

**(B) Authority & membership semantics keyed on the literal string** — these encode *behavior rules*,
and they are the hidden tax on adding a role:
- `claude_driver.py:1861`, `orchestrator.py:4610,6610` — `case_role != "worker"` / `== "manager"`
  gating (reconcile, close-on-case-close, etc.).
- `db.py:3267,3276,4576,4657,4668,6606,11205,11217` — "manager ≠ manager" messaging rules, sender
  authority, case-manager stamping.
- `db.py:11192` `_SENDER_ROLES=("manager","worker")`, `db.py:2352` `WHERE s.case_role='worker'`.
- `control/work_read_model.py:191` `_SESSION_ROLES=("manager","worker","reviewer","evidence")` —
  note "reviewer" is *already* a half-citizen here but nowhere in the boot path.
- `control/cost_read_model.py:243,245`, `interfaces.py:216` (comment-documented vocab only).

**Taxonomy matters:** group (A) is capability resolution and should become a single generic lookup.
Group (B) is RBAC/authority and must migrate from *literal-string equality* to *capability-class
predicates* (e.g. `role.can_dispatch`, `role.can_review`) so new roles slot into the auth model
declaratively instead of needing a new `== "..."` branch in a dozen files.

### 2.4 What forces a gateway restart today — and what already does **not**

- **Already hot (no restart):** editing `manager.md` / `worker.md` prose. The prose is `read_text()`
  **at every boot**, not cached in memory — a *new session* picks up edited prompt text immediately.
  This is latent leverage the redesign builds on.
- **Already hot:** `MANAGER_ROLE_ENABLED` and similar runtime flags (registry-writable, re-read per
  boot).
- **Requires rebuild + restart today:** adding *any* new role, because it means (1) a new
  `load_X_role()` function + constants in `roles.py` (Python import), (2) a new entry in `_PROFILE_TOOLS`,
  (3) a new branch in `_role_boot`, (4) a new `role=="X"` branch in `_dispatch_worker`, (5) `docker
  compose build` + recreate. That is the heavyweight path to eliminate.

### 2.5 Current recipe to add a role (the cost you are trying to kill)

1. Write `docs/harness/roles/<role>.md` (prose).
2. Add `X_ROLE_ID`, `X_TOOL_PROFILE`, `_X_ROLE_DOC`, `load_X_role()` to `src/core/roles.py`.
3. Add `"x_v1": [...tools]` to `_PROFILE_TOOLS` in `claude_role_adapter.py`.
4. Add a branch to `_role_boot` in `claude_driver.py`.
5. Add a `role=="x"` branch to `_dispatch_worker` in `scripts/mcp_manager.py`.
6. (If it has authority/membership semantics) add `== "x"` branches across `db.py` / `orchestrator.py`.
7. `docker compose build` + recreate gateway (+ worker if worker-side). Flip `MANAGER_ROLE_ENABLED`.

Seven steps across five files + a rebuild, for what should be **one file**.

---

## 3. Target design

### 3.1 Two layers

```
                         ┌──────────────────────────────────────────────┐
   OPEN SET (hot)        │  PROFILE  (declarative file, hot-loaded)       │
   add by file drop ───▶ │  docs/harness/agents/<name>.md                 │
   no restart            │  ---                                           │
                         │  name: scientific-reviewer                     │
                         │  role: reviewer          ◀── names a Role      │
                         │  model: sonnet                                 │
                         │  skills:                                       │
                         │    - git+https://…/sci-review@<full-sha>       │
                         │  description: when to use me                   │
                         │  ---                                           │
                         │  <markdown body = system prompt / persona>     │
                         └───────────────────────┬──────────────────────┘
                                                 │ role: <id>
                         ┌───────────────────────▼──────────────────────┐
   CLOSED SET (code)     │  ROLE  (capability, in code, restart-gated)    │
   add rarely,           │  reviewer | worker | manager | governor        │
   restart acceptable    │  ├─ tool_profile  → MCP tool allowlist         │
                         │  └─ capability flags: can_dispatch,            │
                         │        can_review, can_close_case, can_message │
                         └────────────────────────────────────────────────┘
```

- A **Role** is a capability: a `tool_profile` plus a set of **capability predicates** that replace the
  scattered `== "manager"` string checks. Closed set, lives in code, changing it is a reviewed,
  restart-gated event. This is where a gateway restart remains legitimate.
- A **Profile** (a.k.a. agent-type) is a persona: a hot-loaded file (Claude-Code `.claude/agents`
  shape — YAML frontmatter + Markdown body) that **names** a Role and supplies prose, model, and pinned
  skills. Open set, lives in `docs/harness/agents/`, added/edited with **no restart**.
- "Governor of program X" and "governor of program Y" = two **Profiles** over one `governor` **Role**.
  A new programme next week = a new file, nothing else.

### 3.2 Profile file shape (adopt the Claude Code `.claude/agents` convention)

Research confirms Claude Code's sub-agent file is the gold standard and is *ahead* of AutoGen/CrewAI/
LangGraph on this exact split (frontmatter = capability, body = persona, hot-watched, no restart).
We adopt its shape so operators already know it and cloud-authored agents port over cleanly:

```yaml
---
name: scientific-reviewer          # identity (not the filename)
role: reviewer                      # REQUIRED — names a closed-set Role (capability)
description: Invoke to adjudicate whether a delivery is sound scientific work.
model: sonnet                       # optional; inherit if omitted
skills:                             # optional; pinned behavior, local or frozen SHA
  - sci-review                      #   local skill name, OR
  - git+https://github.com/org/repo@<full-40-char-sha>#subdirectory=skills/x
tools:                              # optional ALLOWLIST override within the Role's ceiling
  - mcp__manager__record_review     #   (may only narrow, never exceed, the Role's grant)
---
You are a scientific reviewer embedded in the AI-Team. <persona / method prose…>
```

- `role` is the only hard-required link to the capability layer. `tools` may **narrow** the Role's
  allowlist but never exceed it (enforced at resolution — a Profile cannot privilege-escalate).
- Validate the frontmatter with a **pydantic model** (`AgentProfile`) that also emits the JSON Schema
  (honors project pydantic-first rule). A malformed profile is **rejected at load with a structured
  error**, never silently degraded (the opposite of today's `role=="worker"` silent fallthrough).
- **Skill pinning:** no cross-ecosystem standard exists, so we fix the convention here — a local skill
  **name**, or a `git+URL@<full-sha>` (immutable full SHA, never a branch/tag), recorded so a reinstall
  reproduces the exact behavior. Frozen behavior = frozen SHA.

### 3.3 Hot-load mechanism (how "no restart" is achieved)

- **Lazy per-dispatch read + mtime cache.** On resolve, `stat()` the profile file; re-parse only if the
  mtime changed. This is the pattern Claude Code uses and it **sidesteps every `importlib.reload`
  stale-object trap** precisely because a Profile is *data* (YAML/Markdown), not imported Python.
- Prose already loads per-boot today (§2.4) — we are extending that proven property from the two prose
  files to a scanned directory of profiles. No new runtime, no daemon, no file-watcher required for v1
  (watchdog is an optional later optimization).

---

## 4. Minimal-change migration path (each phase independently shippable)

Ordered so that value lands early and nothing is a big-bang rewrite. Phases 0–2 already deliver the
"drop a file, no restart" goal for any persona that reuses an existing capability.

- **Phase 0 — Generalize the boot seam (small, high-leverage).**
  Replace the two hardcoded branches in `_role_boot` (`claude_driver.py:1827,1837`) and the twin loaders
  in `roles.py` with **one** `load_role(role_id)` backed by a registry/scan. Replace the
  `role=="worker"` opt-in in `_dispatch_worker` with "thread `role_boot` for any registered role id."
  Validate the id against the registry (reject unknown → structured error, kill the silent-fallthrough
  that let the "governor" run as a bare worker). *Still needs a restart to add a role, but collapses
  five edit sites to one and makes the registry the single source of truth.*

- **Phase 1 — Frontmatter + Profile layer (delivers the no-restart goal).**
  Add `AgentProfile` pydantic model + a loader that scans `docs/harness/agents/*.md`, parses
  frontmatter, validates, and caches by mtime. `load_role` consults profiles first (profile → role +
  prose + model + skills), falling back to the legacy two prose files. **Now a new persona over an
  existing Role (`reviewer`, `worker`) is a pure file drop — no restart.** This is the core win and
  the point where "scientific reviewer" and "advisory board" (as worker/reviewer-class personas)
  become trivial.

- **Phase 2 — Add the `reviewer`/`governor` Role capability (one restart, then open forever).**
  Define the closed-set capability entries (tool_profile + capability predicates) **once**. After this
  single restart, every future governor/reviewer persona is Phase-1 file drops. This is the only place
  a restart stays legitimate, and it is paid once per *capability class*, not per *persona*.

- **Phase 3 — Migrate authority checks from literals to predicates (deeper, decoupled).**
  Convert the group-(B) `== "manager"` / `_SENDER_ROLES` checks (§2.3) to capability-predicate queries
  on the Role (`role.can_dispatch`, `role.can_review`, `role.can_message(peer)`). This removes the last
  reason a new capability needs scattered code edits. Can be done incrementally, check-by-check, behind
  behavior-preserving tests; not required for Phases 0–2 to deliver.

**What still (correctly) needs a restart after all phases:** adding a brand-new *capability class* or
changing a Role's tool ceiling (Phase 2-type change). Everything persona-shaped — new agent types,
prompt iteration, new programme governors, model swaps, skill re-pins — is hot.

---

## 5. How this enables what's coming next

- **Governor programme / per-programme governors.** One `governor` Role (Phase 2). Each programme is a
  Profile file (`docs/harness/agents/governor-<programme>.md`) with its own prose + pinned skill +
  model. A new programme = a new file, no restart. Directly unblocks `.ai/context/
  GOVERNOR_OUTER_LOOP_V1.md`.
- **Advisory board.** Either a Profile over `worker`/`reviewer` (read-only advisory) or over a new
  `advisor` capability if it needs distinct tools — the layering makes that a one-line `role:` decision
  per file, not a code fork.
- **Workflow UI + hooks (the Confi-UI vision).** The MCP tools are already the orchestration seam.
  The "before `close_case`, ask the Manager *was this scientific work, did you invoke the reviewer?*"
  gate is a **`PreToolUse` hook on `close_case`** — it needs *no role at all*, and it can chain a
  dispatch of the `scientific-reviewer` Profile. Likewise "after the Manager finishes → dispatch the
  governor/advisory board" is a post-stage hook. A drag-and-drop workflow builder then composes
  {Profiles} × {tools} × {hooks} — and because Profiles are declarative files addressable by `name`,
  the UI writes a file instead of patching code. The redesign is the precondition for that UI being
  buildable at all.

---

## 6. Open decisions for the operator (surface, don't guess)

1. **Where do Profiles live?** Proposed `docs/harness/agents/` (harness-owned, versioned with the
   repo). Alternative: a writable runtime dir (so the future UI writes without a commit). Pick based on
   whether Profiles are doctrine (commit-reviewed) or operator data (hot, uncommitted). Recommendation:
   repo dir for v1 (reviewable, diffable), add a runtime overlay dir when the UI lands.
2. **Capability classes for the first cut.** Proposed closed set: `manager`, `worker`, `reviewer`,
   `governor`. Confirm whether "advisory board" is its own class or a `reviewer` Profile.
3. **Profile `tools` narrowing — allow it?** Recommended yes, narrow-only (never exceed the Role
   ceiling). Confirm.
4. **Skill-pin enforcement.** Do we hard-require a full SHA for `git+` skills (reject branches/tags) at
   load? Recommended yes (frozen = frozen).

---

## 7. One-paragraph summary for the next agent

The harness conflates **capability** (tools + authority, a small closed set that may cost a restart)
with **behavior** (persona/prompt/skills, an open set that must be free), under one hardcoded "role"
string resolved by a two-branch `if` in `claude_driver._role_boot` and gated by a `role=="worker"`
check in `dispatch_worker`. Prose already hot-reloads per boot; only the *wiring* is baked. Split the
two: a closed set of **Roles** (capability, in code) and an open set of **Profiles** (Claude-Code-style
`.claude/agents` frontmatter files, hot-loaded by lazy mtime-cached scan) where a Profile names a Role.
Phase 0 collapses the boot seam to one `load_role`; Phase 1 adds the Profile layer and delivers
no-restart persona drops; Phase 2 adds the `reviewer`/`governor` capability once; Phase 3 migrates the
scattered `== "manager"` authority checks to capability predicates. After that, every new agent type —
scientific reviewer, advisory board, per-programme governor — is one validated file, no restart, and
the future workflow UI composes Profiles × tools × hooks by writing files instead of editing code.
```
