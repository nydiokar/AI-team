```yaml
job_id: AGENT_67_MESH_SECURITY_REVIEW_THREAT_MODEL
created_at: "2026-08-03T18:17:00.272353+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T18:17:00.272377+00:00"
```

# DISPATCH — A67 · Mesh/relay/worker security review + threat-model doc

**Level:** 2/3 (security review + docs; code only if a P0/P1 forces a fix) · **Type:** adversarial security audit → private findings → public threat-model doc
**Authored:** 2026-08-03 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (independent). Read A58/A61/A63 (quota coordinator) only to avoid overlap; do not block on them.

> **Read this first — why this packet exists.** Reviewing **hcom** (`github.com/aannoo/hcom`)
> surfaced that its README carries an unusually **honest, well-structured relay security
> threat-model** (trust domain / what the token grants / limits-by-design / incident response /
> storage). AI-Team's mesh security is real but **thinly documented**: a shared `WORKER_TOKEN`
> over Tailscale (`MESH_TAILSCALE_IP`), the embedded task server (`MESH_TASK_SERVER_PORT` 9002),
> the node registry, the `DASHBOARD_TOKEN` on the control API, and — critically — **a Manager
> MCP surface that can `dispatch_worker` and drive real code execution across nodes.** This is,
> in the operator's words, *"becoming a dangerous autonomous loop that could create a lot of
> damage if someone breaches into it."* This job does two things: (1) an **adversarial security
> review** of the mesh/relay/control surfaces for P0/P1 exposure, and (2) writes a **public
> threat-model doc** modeled on hcom's structure.
>
> **CRITICAL DISCLOSURE RULE (non-negotiable).** If we are broken, we do NOT advertise it
> publicly. **Live, exploitable P0/P1 findings go to a PRIVATE, GIT-IGNORED artifact
> (`.security/mesh_findings_2026-08.md`), never to a committed file, never to the public
> threat-model doc, never to DISPATCH_LOG prose in exploit detail.** The public doc describes the
> *designed* security model and its *documented* limits (like hcom does) — it must NOT contain a
> working exploit recipe for an unpatched hole. Confirm `.security/` is in `.gitignore` (add it
> if not) as step 0. If a P0 is found, it is fixed on a private branch FIRST, then the public doc
> is written against the patched state. When in doubt about whether a detail is safe to publish,
> STOP and ask the operator.

## PART A — Adversarial security review (private output)
Apply the §7 Service Boundary Checklist to every external-input surface and enumerate exposure.
Work from OUR code, not assumptions. Surfaces to audit (at minimum):

1. **`WORKER_TOKEN` / mesh transport.** How is it minted, stored, compared (constant-time?),
   scoped? What does a leaked `WORKER_TOKEN` grant — is it all-or-nothing like hcom's PSK, or
   scoped? Is the task server reachable only on the Tailscale IP, or does it bind `0.0.0.0`?
   (`src/control/{task_server,embedded_server,node_registry}.py`, `config/settings.py::MeshConfig`.)
2. **`DASHBOARD_TOKEN` / control API.** The Manager pulls it from `config.mesh.dashboard_token`
   (MEMORY: manager invocation). What does it authorize? Any endpoint that mutates state
   (`/api/instructions`, `/api/manager`, `/api/sessions`) reachable without it, or with it over a
   non-loopback bind? Rate limits? Payload size caps (§7 request-size)?
3. **The Manager→worker execution loop as an attack surface.** This is the highest-stakes item:
   an attacker who can post to the dispatch/instruction surface can cause **arbitrary code
   execution in a repo on a node**. Trace exactly what authenticates a dispatch, what bounds the
   cwd/repo scope (production_vision §4 "bounded cwd/repo scope"), and whether allowlists/tool
   bounds are actually enforced at the driver (`claude_driver` allowed-tools) vs merely intended.
4. **Node registration / registry poisoning.** Can a rogue host register as a node and receive
   pinned/unpinned tasks? What identity check gates `node_registry`? (Incarnation-ID reaper is in
   MEMORY — confirm it's an authZ boundary, not just liveness.)
5. **Malformed / adversarial input** (§7): garbage/truncated/oversized payloads to the task
   server and control API — structured error vs panic vs unbounded read.
6. **Secrets at rest / in logs.** Are `WORKER_TOKEN`/`DASHBOARD_TOKEN`/telegram tokens ever
   written to `logs/events.ndjson`, session logs, or `mesh.db`? File modes on state/secrets
   (hcom writes its PSK `0600` — do we?).

For each finding: severity (P0 breach / P1 serious / P2 hardening / P3 note), the exact code path,
a NON-public reproduction sketch, and a remediation. **Private artifact only.**

## PART B — Public threat-model doc (`docs/MESH_SECURITY.md`)
Written AFTER Part A (and after any P0/P1 is patched). Structure modeled on hcom's README security
section, describing the DESIGNED model — not live holes:
- **Trust domain & membership** — what a node/operator is trusted with (be honest, like hcom:
  a dispatch surface = code execution on a node).
- **What the tokens mean** — `WORKER_TOKEN` and `DASHBOARD_TOKEN`: scope, storage, what a leak
  grants, rotation story (or the honest absence of one).
- **Limits by design** — no forward secrecy / no per-node attribution / prompt-injection from an
  authenticated peer / local-OS-compromise assumptions (adapt hcom's honest list to us).
- **Bounded execution** — cwd/repo scoping, tool allowlists, rate limits (as they ACTUALLY are).
- **Incident response** — how to rotate tokens, disconnect a node, kill a runaway case
  (`interrupt_case`, PR #50), what canonical state survives.
- **Storage & file modes** — where secrets live and their permissions.
The public doc must contain **no exploit recipe for an unpatched finding.**

## TYPE
Part A = review, private (`.security/`, git-ignored). Part B = docs (`docs/MESH_SECURITY.md`) on
`main`. Any code fix for a confirmed P0/P1 = `feat/<slug>` branch + PR + self-merge, description
written to avoid disclosing the exploit (reference the private finding ID, not the recipe).

## CONTEXT (what exists — audit these)
- `config/settings.py::MeshConfig` — `WORKER_TOKEN`, `MESH_TAILSCALE_IP`, `MESH_TASK_SERVER_PORT`,
  `dashboard_token`, `MESH_ENABLED`.
- `src/control/{task_server,embedded_server,node_registry}.py` — HTTP API, registry, bind address.
- `src/control/control_api.py` — `/api/instructions`, `/api/manager`, `/api/sessions`,
  `/api/push/*`; auth checks.
- `scripts/mcp_manager.py` — the Manager tool surface (`dispatch_worker`, `open_case`, …) and how
  it authenticates to the control API.
- `src/worker/agent.py` — worker daemon; how it authenticates to the mesh.
- `src/backends/claude_driver.py` — allowed-tools enforcement (bounded execution reality).
- MEMORY: worker-restart claim reaper (incarnation ID), `release_worker` guard (PR #27),
  `interrupt_case` kill path (PR #50).
- **hcom reference:** README "Relay Security" section (structure to emulate for Part B).
  Clone: `https://github.com/aannoo/hcom`.

## ACCEPTANCE (proof)
1. `.security/` confirmed git-ignored; `.security/mesh_findings_2026-08.md` written with every
   surface in Part A audited (or explicitly marked "no finding") — private, uncommitted.
2. Every P0/P1 either patched (private-first branch → PR, non-disclosing description) or, if a fix
   is a larger decision, escalated to the operator with the private finding — NOT left silent.
3. `docs/MESH_SECURITY.md` written, hcom-structured, describing the designed model + honest
   limits, containing NO exploit recipe for an unpatched hole.
4. `pytest` on any touched module only (TEST COST GUARD).

## RESERVED DECISIONS (surface, do not guess)
- **R1 — disclosure line.** If unsure whether a specific detail is safe to publish, STOP and ask.
- **R2 — P0 remediation scope.** A P0 whose fix is architectural (e.g. token model redesign) is an
  operator decision, not a silent unilateral rewrite — escalate with the private finding.
- **R3 — is the repo even public?** Confirm whether the AI-Team repo is public before deciding how
  aggressively to sanitize DISPATCH_LOG/commit messages. Assume it could become public.

## SCOPE OUT
- Building new auth infrastructure (OAuth, mTLS) unless a P0 demands it — propose, don't build.
- Touching the quota-coordinator scope (A58/A61/A63).
- Publicly documenting any live vulnerability.
- Redesigning dispatch / CONTEXT structures.

## TRAIL / EVIDENCE (fill at close)
- `.security/mesh_findings_2026-08.md` (private, NOT committed — note its existence, not contents,
  in DISPATCH_LOG) · `docs/MESH_SECURITY.md` · any patch PR numbers · `results_ref` → DISPATCH_LOG.

---
## Milestone (burndown)
- [ ] Step 0: `.security/` git-ignored confirmed/added
- [ ] Part A: every surface audited; findings triaged P0–P3 (private)
- [ ] P0/P1 patched private-first OR escalated to operator
- [ ] Part B: `docs/MESH_SECURITY.md` written (no exploit recipes)

## Closure (fill on completion)
(fill when executed)
