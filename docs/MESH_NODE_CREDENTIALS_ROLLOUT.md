# Mesh per-node credentials — rollout playbook

Operates **after PR #77 (`feat/security-per-node-credentials`, A71) is reviewed and merged**
into `main`. This is the hands-on runbook: exact commands, verification at every step, and
the rollback for each step. The design rationale lives in
`.ai/dispatch/AGENT_71_MESH_PER_NODE_CREDENTIALS.md` and the public model in
`docs/MESH_SECURITY.md`.

## What you are doing

Every node today authenticates to the mesh task server (`:9002`) with one shared
`WORKER_TOKEN`; node identity is self-reported. This rollout mints a **unique credential per
node** (`NODE_CRED`) and makes the server verify it against the claimed `node_id` — including
pinned tasks, where the credential must belong to the pinned machine. The shared token stays
usable during the rollout (`MESH_NODE_CREDENTIALS_ALLOW_SHARED_FALLBACK=true`) and is
retired at the end.

Flag defaults (`false`/`true`) mean **nothing changes** until you flip the first flag.

## Flag table

| Env var | Default | Meaning |
|---|---|---|
| `MESH_NODE_CREDENTIALS_ENABLED` | `false` | ON ⇒ server verifies per-node credentials on node endpoints. OFF ⇒ byte-identical old behaviour. |
| `MESH_NODE_CREDENTIALS_ALLOW_SHARED_FALLBACK` | `true` | While `true`, `WORKER_TOKEN` still authenticates as any node (rollout window). `false` ⇒ only enrolled node credentials work. |

## Preflight (before touching anything)

1. Confirm the PR is merged and the gateway is running that code:
   ```bash
   git checkout main && git pull --ff-only
   git log --oneline -1          # expect the A71 merge commit
   curl -s http://127.0.0.1:9003/health
   ```
2. List the nodes you will enroll:
   ```bash
   curl -s http://127.0.0.1:9003/api/nodes
   ```
   Enroll **every** machine that speaks to the task server (every node that registers /
   heartbeats / claims tasks). Missing one will 403 that node once fallback is off.
3. Make sure you can edit each node's repo-root `.env` (the worker loads it via
   `load_dotenv(..., override=False)` — `NODE_CRED` must be a new line there, not already
   exported in the process environment).

## Step 1 — Enroll each node (gateway side)

On the gateway host, for each node:

```bash
.venv/bin/python scripts/enroll_node.py <node_id>
```

Output is the one-time plaintext secret:

```
Minted credential for node horse (SHA-256 stored in mesh.db).
Put this EXACT value in the node's .env as NODE_CRED:

<32-byte-hex-token>
```

Only the SHA-256 is persisted (`state/mesh.db`, `node_credentials` table). **Copy the
plaintext straight into the node's `.env`; it is never shown again.** A re-run of the same
command rotates the credential (old one stops working).

## Step 2 — Provision each node

On each node, append to its repo-root `.env`:

```bash
NODE_CRED=<the printed hex token>
```

Keep `.env` mode `0600`. The gateway never reads `NODE_CRED`; the worker sends it as
`Authorization: Bearer <NODE_CRED>` (falls back to `WORKER_TOKEN` when absent — this code
ships in PR #77 and only takes effect after the worker is redeployed).

## Step 3 — Redeploy workers (surface this)

This is the one step you coordinate with the operator's "no silent worker restart" rule —
do it **with** the operator, node by node, outside active sessions:

```bash
pm2 restart ai-team-worker   # or however that node's worker runs
```

After restart, the worker picks up `NODE_CRED` from its `.env`.

## Step 4 — Enable enforcement (gateway)

On the gateway, add to `.env` and restart the gateway:

```bash
MESH_NODE_CREDENTIALS_ENABLED=true
# MESH_NODE_CREDENTIALS_ALLOW_SHARED_FALLBACK stays true for now
pm2 restart ai-team-gateway
curl -s http://127.0.0.1:9003/health
```

With fallback still `true`, the shared token still works as a safety net, but enrolled nodes
now bind their identity to their credential.

## Step 5 — Verify

```bash
# every enrolled node is still heartbeating (fresh updated_at)
curl -s http://127.0.0.1:9003/api/nodes

# claim flow still works — watch task_server logs for clean claims on each node
# a wrong-node credential must be refused:
curl -s -X POST http://127.0.0.1:9002/nodes/register \
  -H "Authorization: Bearer <cred-of-other-node>" \
  -H "Content-Type: application/json" \
  -d '{"node_id":"horse"}'     # expect 403 while enabled
```

Check the task-server log for the *last successful* heartbeat/claim from each enrolled node
**after** Step 4 — that proves the node is presenting its own credential.

## Step 6 — Drop the shared fallback (gateway)

When every node has been enrolled, provisioned, and redeployed (Step 1–3), and Step 5 is
clean for **all** nodes:

```bash
# .env on gateway:
MESH_NODE_CREDENTIALS_ALLOW_SHARED_FALLBACK=false
pm2 restart ai-team-gateway
```

From here only enrolled node credentials authenticate node endpoints. The shared token is
**no longer accepted on `:9002`** (it still works on the control API `:9003`).

## Step 7 — Retire `WORKER_TOKEN`

Rotate/remove `WORKER_TOKEN` from every node's `.env` when convenient (it is now unused for
mesh traffic). Keep `DASHBOARD_TOKEN` for the control API.

## Rollback (valid at every step)

| If... | Do |
|---|---|
| A node 403s or stops heartbeating after Step 4 | Fix its `NODE_CRED` (re-run `enroll_node.py`, update `.env`, redeploy) — or set `MESH_NODE_CREDENTIALS_ENABLED=false` + restart gateway to go back to the old model. |
| Anything misbehaves after Step 6 | Flip `MESH_NODE_CREDENTIALS_ALLOW_SHARED_FALLBACK=true` + restart gateway (shared token works again immediately). |
| Worst case | Both flags back to defaults + gateway restart = byte-identical to today. Nothing is ever permanently changed until `WORKER_TOKEN` is retired. |
| Wrong credential minted | Re-run `scripts/enroll_node.py <node_id>` (rotates), update `.env`, redeploy that node. |

## Troubleshooting

- **401 on a node endpoint** = token not valid at all (flag ON, wrong/absent credential, and
  fallback already off). Check `NODE_CRED` in that node's `.env` and that the worker was
  restarted after Step 2.
- **403 = "credential not bound to node X"** = the token is enrolled but for a different
  node, or a pinned task was claimed by a non-pinned machine.
- **Flag flip didn't take effect** = config is read at process start; restart the gateway
  after editing `.env`.
- **`NODE_CRED` ignored on a node** = `override=False` in the worker's `.env` load: if the
  variable is already exported in the worker's environment, the `.env` line won't win.
