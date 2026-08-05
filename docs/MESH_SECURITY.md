# MESH_SECURITY.md — Mesh/relay security model

> Describes the **designed** security model of the AI-Team mesh and its honest limits,
> in the spirit of hcom's relay-security writeup. This is a threat model, not a finding
> dump: it states what the operator is trusted with, what a leaked token means, and how
> to respond. It contains no instructions for breaking an unpatched path.

## Trust domain & membership

The mesh is **one trust domain for one operator's machines**. It has exactly two classes
of peer, and membership is all-or-nothing:

- **The gateway** (hosts the orchestrator, the control API on `:9003`, and — when
  `MESH_ENABLED=true` — the embedded mesh task server on `:9002`).
- **Nodes** (the gateway itself when embedded, plus worker daemons on other machines,
  e.g. a worker node on the tailnet). Nodes register, heartbeat, claim tasks, execute
  them, and report results back through the task server.

The intended trust boundary is the **Tailscale tailnet**: the task server binds the
gateway's `MESH_TAILSCALE_IP` (fallback `127.0.0.1`), and the control API binds
`127.0.0.1` **plus** the Tailscale IP — never the LAN/public interface by default.
Setting `CONTROL_API_HOST` is an explicit operator override; a value like `0.0.0.0`
there is a deliberate LAN-exposure choice and exposes the UI (and its in-page
credential) to anything on that network.

Be honest, like hcom: **the dispatch surface is code execution on a node.** A
Manager (or anyone who can post an instruction) causes arbitrary Claude/Codex sessions
to run inside a repo on a worker, and those sessions run with
`CLAUDE_SKIP_PERMISSIONS=true` / `GUARDED_WRITE=false`. Only enroll machines you would
give shell access to.

## What the tokens mean

Two bearer credentials, one effective authority:

| Token | Authenticates | Fallback | What a leak grants |
|---|---|---|---|
| `WORKER_TOKEN` | Every mesh task-server surface (`:9002`): nodes/register, heartbeat, claim, result, `/files` staging, jobs, telemetry. **Also accepted as a control-API credential** (`:9003`). | n/a | Full read + write on the mesh task server **and** the control API — including `/api/instructions`, `/api/manager`, `/api/cases`. That is arbitrary instruction dispatch = shell-equivalent on every enrolled node. |
| `DASHBOARD_TOKEN` | The web UI / control API (`:9003`). | Falls back to `WORKER_TOKEN` when unset. | Full control-API read + write (sessions, instructions, cases, cost, nodes). Does **not** by itself authenticate the `:9002` task server. |

Both are static, single-value secrets stored in `.env` (mode `0600` on the gateway, as of
2026-08-05). There is **no per-node key**: every node carries the same `WORKER_TOKEN`, and
node identity (`node_id` in register/claim/result payloads) is **self-reported**, not
verified. There is no token expiry, no scope, and no revocation list — a leaked token is
valid until it is rotated by hand.

Concretely, a leaked `WORKER_TOKEN` (the analog of hcom's join token on a public broker):

- can read all sessions, turns, cases, flows, node inventory, and cost data,
- can dispatch instructions → run sessions → execute code on any node,
- can claim and forge task results (identity is self-reported),
- can poison the node registry and knock a live node's in-flight claims loose.

Treat `WORKER_TOKEN` and `DASHBOARD_TOKEN` **exactly like an SSH key or API key**.

## Limits by design

The honest list, adapted from hcom's relay section:

- **No forward secrecy / static credentials.** Tokens are static strings in `.env`. A
  leak is a leak of everything until manually rotated. There is no automatic rotation.
- **No per-device attribution.** Sender `node_id` is routing metadata, not an
  authorization identity. Every device holding the shared token speaks with full
  authority; a task's "claiming node" is what the caller says it is.
- **Prompt injection from an authenticated peer is by design.** A Manager peer can
  dispatch arbitrary work — that is the product. Only admit peers you would let run code
  in your repos.
- **Local OS compromise is out of scope.** Anyone who can read the `.env` or the mesh DB
  (`state/mesh.db`) on the gateway or a node is fully trusted. The mesh does not defend
  against another local user or filesystem-level malware. This is why the secrets and DB
  are chmod `0600` and why `.env` must never be world-readable on any node.
- **Token comparisons are not constant-time.** Bearer checks are plain string equality;
  a LAN-side timing attacker is not part of the threat model.
- **The dashboard token is served in-page.** The web UI injects the bearer token into the
  served `index.html` so the browser can authenticate; anyone who can fetch `/` on a bound
  interface (loopback/tailnet) can read it. This is the reason the control API refuses to
  bind the LAN interface by default.
- **No rate limits.** Neither surface throttles authentication attempts or dispatch
  volume; a holder of a valid token can flood the queue.
- **No enforced spend ceiling in the live default.** The SDK governor knobs
  (`CLAUDE_SDK_MAX_TURNS`, `CLAUDE_SDK_MAX_BUDGET_USD`) exist but are not configured in
  the live environment (`/health` reports `sdk_max_budget_usd: null`). Cost bounding is
  operator-configured, not on by default.

## Bounded execution — as it actually is

- **Local cwd/repo scope.** `PathResolver` resolves a session's `repo_path` against the
  configured allowed root (`CLAUDE_ALLOWED_ROOT`). Local sessions are validated at create
  time (`invalid_repo_path` → 400). Remote (node-pinned) sessions skip that check because
  the gateway cannot stat a path that lives on another machine — the node is trusted with
  its own paths.
- **Tool / role bounds.** The `claude_driver` grants Manager tools only when
  `MANAGER_TOOLS_ENABLED` **and** a configured `manager` MCP server both pass, and role
  boot is gated by `MANAGER_ROLE_ENABLED`. These are flag-gated; in the live env they are
  ON. There is no per-turn or per-session tool allowlist beyond the role gates.
- **Input caps.** The Manager MCP client bounds its own arguments (`objective` ≤ 8000
  chars, path/id/files caps, `wait_for_worker` timeout ≤ 600 s). The HTTP API bounds
  `continue_inline` (48 000), `continuation_plan` (8 000), and — since 2026-08-05 (PR #73) —
  the whole write surface (`description`, Manager/Case `objective`,
  `completion_criteria` ≤ 256 KB; oversized ⇒ 422). No request rate limit is imposed: the
  Manager can legitimately dispatch several workers in parallel. Uploads are capped only
  when `GATEWAY_UPLOAD_MAX_MB` (control API) or `telegram.upload_max_mb` is set, and the
  `:9002` `/files` staging endpoint has no size cap of its own.
- **Staged uploads.** Uploaded filenames are sanitized to a safe charset and
  containment-checked against the staging root (hardened 2026-08-05, PR #72) on both the
  control-API and task-server paths, so a hostile filename cannot escape the upload
  directory.

## Incident response

Best-effort damage control, like hcom — no single switch is a guarantee, but the
sequence below is the runbook:

1. **Assume total compromise.** A leaked `WORKER_TOKEN`/`DASHBOARD_TOKEN` means code
   execution on every node. Stop trusting the mesh until rotated.
2. **Stop new instruction flow.** Kill any runaway Case with the `interrupt_case` kill
   path (PR #50). Suspend the harness admission gate (Level-3) so unapproved tasks queue
   instead of executing. If you need an absolute stop, set `MESH_ENABLED=false` and
   restart the gateway — the `:9002`/`:9003` surfaces are gone until re-enabled.
3. **Rotate both tokens.** Generate new `WORKER_TOKEN` and `DASHBOARD_TOKEN`, write them
   into `.env` on the gateway **and on every node**, restart the gateway, and redeploy the
   node workers. The old tokens remain valid on any machine you miss — rotation is only
   complete when every enrolled machine carries the new value. There is no server to
   notify and no denylist.
4. **Disconnect a node.** Revoke the node's tailnet access (Tailscale admin) so it can no
   longer reach the gateway; the registry will mark it offline after its heartbeat
   timeout and pinned tasks wait / requeue rather than migrate to another host.
5. **Know what survives.** Sessions and task/result rows live in `state/mesh.db`
   (canonical) with per-session JSON fallbacks that are never deleted; `results/` and
   `tasks/` are droppable. Rotation does not invalidate stored history.

## Storage & file modes

- `WORKER_TOKEN` / `DASHBOARD_TOKEN` live in `.env` — mode `0600` on the gateway
  (hardened 2026-08-05). Verify the same on every node.
- **Node deploy guard (2026-08-05, PR #74).** `scripts/safe_worker_deploy.py` refuses to
  load a `.env` that is group/other-readable, so the next surfaced worker deploy enforces
  `0600` on every node automatically; `AI_TEAM_ALLOW_LOOSE_ENV=1` is the explicit
  acknowledged override. Backups must preserve modes (`tar --preserve-permissions`).
- `state/mesh.db` — mode `0600` (hardened 2026-08-05). Holds session turns, task
  payloads, node state. Anyone who can read it sees the mesh's history.
- Tokens are **not** logged by application code and are **not** stored in the DB or in
  task payloads. Exceptions to watch: the SSE event stream accepts the token as a query
  parameter (a browser `EventSource` limitation) — it can land in access logs or browser
  history.
- `state/uploads/` is the temporary staging root for `/files`; entries are cleaned up by
  the worker after fetch.

## Review & history

- 2026-08-05 — Adversarial security review of the mesh surfaces (private findings in
  `.security/`, git-ignored; see the A67 dispatch packet). P0 fixed in PR #72
  (staged-upload filename hardening). Secrets and mesh DB chmod to `0600`.
- Open, escalated design items: per-node credentials (replaces the shared token and
  self-reported node identity), server-side dispatch bounds + rate limits, and moving the
  dashboard token out of the served HTML. See `.security/mesh_findings_2026-08.md`.
