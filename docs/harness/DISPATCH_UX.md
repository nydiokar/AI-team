# Dispatching a worker — the manager's-eye view

*Last updated 2026-10-10.*

How a Manager gets a worker running on the right repo/node with the fewest steps,
and how the dispatch error now tells you how to fix a wrong node instead of
leaving you guessing. Owned surface: `scripts/mcp_manager.py` (the `manager` MCP
server — `dispatch_worker`, `list_nodes`).

Key fact that trips everyone up: **the OS hostname is NOT the carrier id.** This
host's hostname is `DESKTOP-3PGTBMF`, but its registered mesh carrier is `Horse`.
Passing the hostname as `node_id` gets you a `carrier_unavailable` 503. The three
changes below make that a non-issue.

## To dispatch on the LOCAL node (the node you are on)

Pass `node_id='local'` (aliases: `here`, `this`, `self`, `this-node`). It is
resolved to this node's registered carrier automatically — you never hand-type the
mesh id:

```
dispatch_worker(objective="…", cwd="/path/to/repo", model="sonnet", node_id="local")
```

Resolution matches your host against `GET /api/nodes`, in order:
1. the carrier daemon's own id env (`MESH_LOCAL_CARRIER_NODE_ID`, else
   `WORKER_NODE_ID`, else `AI_TEAM_NODE_ID`) — validated against the live online
   managed-capable set;
2. this host's tailscale IP matched to a node's `tailscale_ip`;
3. the OS hostname matched to a `node_id` (last resort; usually does not match).

If none resolves, or it is ambiguous, the call **fails with the list of online
carriers** — it never silently mis-routes. The reply echoes what it resolved to
(`Node: node_id='local' resolved to carrier 'Horse' via carrier-id env (Horse)`).

> Note: *omitting* `node_id` is NOT the same as `node_id='local'`. Omitting routes
> the worker to the **gateway host** (`__local__`), where the gateway checks `cwd`
> against its own allowed_root. Omit only when the repo lives on the gateway host;
> otherwise pass `node_id='local'` (this node) or an explicit carrier id.

## To target ANOTHER node

First discover the valid carriers — cheap and read-only:

```
list_nodes(backend="claude")
```

This lists each registered carrier with `[online]`/`[offline]`, its `tailscale_ip`,
and its `managed_backends`. Only an `[online]` carrier whose `managed_backends`
includes your backend can take a dispatch. Pass one of those ids as `node_id`:

```
dispatch_worker(objective="…", cwd="/repo/on/that/node", model="sonnet", node_id="kanebra")
```

An explicit non-sentinel `node_id` is passed through **unchanged** — no resolution
round-trip, same behaviour as before.

## If you get `carrier_unavailable`

You no longer have to reverse-engineer the mesh. The 503 is caught and rewritten to
append the actual online carriers for your backend, e.g.:

```
HTTP 503 … no registered managed-capable carrier 'DESKTOP-3PGTBMF' for backend 'claude'.
  'DESKTOP-3PGTBMF' is not an online managed-capable carrier for backend 'claude'.
  Online carriers for 'claude': Horse (100.112.245.29), kanebra (100.88.11.88).
  Pass one as node_id, or pass node_id='local' to auto-resolve the carrier for the
  node you are on. (See `list_nodes`.)
```

Re-dispatch with a listed id, or `node_id='local'`. (Modelled on how the repo's
symbol index answers a miss with near-matches rather than a bare "not found".)

## Is the gateway even up? (don't be fooled into thinking it's down)

The gateway usually runs **remotely** (on the tailnet, `CONTROLLER_URL`), not on
`127.0.0.1`. The live-state probe resolves the gateway URL the same way
`mcp_manager._base_url()` does (`DASHBOARD_URL`, else `CONTROLLER_URL` host +
`DASHBOARD_PORT`, else `127.0.0.1:DASHBOARD_PORT`) and prints the address it
actually probed:

```
.claude/skills/checking-live-state/scripts/live_status.sh
# == gateway /health (http://100.88.11.88:9003)
# {"status":"ok", …}
```

A healthy remote gateway now reads as up — it is no longer hardcoded to
`127.0.0.1`, which falsely reported "UNREACHABLE" and made a Manager wrongly
conclude the harness was dead.
