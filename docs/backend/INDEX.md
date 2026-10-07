# Backend Docs Index

Docs that describe the **gateway backend as it is built** — the processes, the HTTP
surfaces, the contracts other code must respect, where data lives, and the flags that
gate it. The Web UI's counterpart is [`docs/frontend/`](../frontend/INDEX.md). For the
project as a whole, start at [`.ai/CONTEXT.md`](../../.ai/CONTEXT.md); for the full
`docs/` catalog see [`docs/INDEX.md`](../INDEX.md).

| Doc | Read for |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | **Start here.** Process/deployment topology (PM2 and Docker), the Control API (`:9003`) and mesh task-server (`:9002`) route maps. |
| [`CONTROL_CONTRACT.md`](CONTROL_CONTRACT.md) | The M1 inbound/outbound contract — event envelope, entry points, backend registry, read model. Read before adding a surface or a backend. |
| [`CONVERSATION_DATA_FLOW.md`](CONVERSATION_DATA_FLOW.md) | Where conversation/artifact data lives and how it flows; §0 is the DB-canonical migration. |
| [`DATABASE_AUTHORITY.md`](DATABASE_AUTHORITY.md) | Controller vs. worker DB authority (A88) — the controller owns `mesh.db`; workers read controller state over the task-server API. |
| [`ENV_FEATURE_FLAGS.md`](ENV_FEATURE_FLAGS.md) | Inventory of default-OFF feature flags. Check here before assuming a built feature is live. |
| [`MESH_SECURITY.md`](MESH_SECURITY.md) | Mesh trust domain, threat model, token-leak response. |
| [`CODEX_APP_SERVER_ADAPTER_CONVERGENCE.md`](CODEX_APP_SERVER_ADAPTER_CONVERGENCE.md) | The single Codex backend (`codex_native` over the app-server RPC) and its runtime-recycle boundary. |
| [`QUOTA_WINDOW_COORDINATOR_PHASE1.md`](QUOTA_WINDOW_COORDINATOR_PHASE1.md) | Architecture of the observe-only quota window coordinator (`src/services/quota_window_coordinator.py`). |

## Related, outside this folder

| Doc | Why |
|---|---|
| [`docs/adr/`](../adr/0001-canonical-sdk-driver-for-agent-spawn.md) | Architecture decision records (ADR-0001: agents always spawn on the canonical SDK driver). |
| [`docs/RUNBOOKS/`](../RUNBOOKS/OPERATIONS_PM2.md) | Operating and deploying these processes (PM2, Docker, control-surface deploy). |
| [`docs/harness/`](../harness/README.md) | The Manager/worker loop that drives the Case surface in `ARCHITECTURE.md` §2b. |
| [`docs/schema/results.schema.json`](../schema/results.schema.json) | JSON schema for task result artifacts. |

## Maintenance

This folder holds docs that describe the system **as built**. Specs and designs for
work not yet (fully) built stay in `docs/` and are catalogued in
[`docs/INDEX.md`](../INDEX.md); when one ships and becomes the reference for its
subsystem, move it here and update its inbound links. When a route or a process
changes, update `ARCHITECTURE.md` in the same PR.
