# Skills library (O1) — attach professional attitudes by reference

**Status:** design + minimal flag-gated slice (this PR). Flag `SKILLS_LIBRARY_ENABLED`
default **OFF** — activation is a Manager/operator decision, not shipped live here.
**Answers:** `docs/M3_MANAGER_INVOCATION_SPEC.md` §7 open question **O1**.

## The problem

Today the Manager re-authors the same *attitude prose* in every dispatch envelope — "no false
success", "reuse before build", "verify claims in git" (see `docs/harness/roles/manager.md` and
`worker.md`). That is (a) repeated tokens on every dispatch, and (b) inconsistent, because the
prose is retyped from memory each time. O1 replaces the retyping with **named, versioned skills**
the Manager attaches *by reference*: `dispatch_worker(skills=["no-false-success", …])`.

## The token-lever decision (A vs B) — stated honestly

There are two mechanisms, and they differ on *what* they save:

| | (A) reference-in-authoring, expanded on the wire | (B) reference-carried, resolved worker-side |
|---|---|---|
| Manager writes | `skills:[no-false-success]` (~3–6 tok) | same |
| Where resolved | **Manager side**, against the Manager's repo-local `skills/` | **worker side**, at boot |
| Worker receives | full skill text (expanded into the objective) | just the ids (resolves locally) |
| Saves Manager authoring tokens | ✅ | ✅ |
| Saves **wire + worker-prompt** tokens | ❌ (worker still gets full text) | ✅ |
| Stabilises the SDK prompt cache (ADR-0001) | ❌ | ✅ (static, cache-warm prefix) |
| Works when the worker is on a **remote node** (Horse, separate FS) | ✅ (text is *carried*, not resolved node-side) | ❌ **until** `skills/` is provisioned on that node |

### Token math

A single re-authored attitude paragraph is ~80–120 tokens; the Manager typically attaches 2–3 per
dispatch, so ~200–360 tokens of *Manager authoring* per dispatch today. A reference is ~3–6 tokens.

- **(A)** removes the Manager's ~200–360 authoring tokens/dispatch and guarantees the wording is
  the versioned library text, not a from-memory paraphrase. **Wire/worker-received tokens are
  unchanged** — the worker still gets the full skill text, just consistently.
- **(B)** additionally removes those ~200–360 tokens *from the wire and from the worker's per-turn
  prompt on every dispatch*, and — more importantly for the persistent-SDK-session cache thesis —
  turns the skill text into a **static prefix** the worker loads once, so it becomes a cache-warm
  prefix (ADR-0001) rather than per-dispatch-varying prose that busts the cache.

### Decision: ship (A) now, (B) is the documented follow-up

**(B) is the real token lever**, but it only works when the worker's filesystem holds the `skills/`
library. Workers run on a **remote node (Horse)** whose filesystem is *separate* from the gateway
host — `skills/` is committed to *this* repo, present wherever the gateway/Manager repo is checked
out, but **not** on Horse unless it is deployed there. So (B) is **not achievable now** for remote
workers without a provisioning step.

(A) resolves on the **Manager side** (wherever `scripts/mcp_manager.py` runs — the gateway host or
the Manager's node, which always has this repo checkout) and injects the text into the objective,
which is *carried over the wire* to the worker's first turn regardless of node. So **(A) is the
honest first shippable slice**: it captures the authoring-token + consistency win today and works
for both local and remote workers. **(B) is the follow-up** once the library is provisioned
node-side (deploy `skills/` to worker nodes, or serve it via a small read endpoint the worker
fetches at boot and caches), at which point `dispatch_worker` would carry the ids and skip the
expansion for node-local workers.

## Storage shape — `skills/`

One markdown file per named skill, committed to the repo root: `skills/<id>.md`.

- **id** = the filename stem (`no-false-success`). Lowercase letters/digits with single hyphens
  only (`^[a-z0-9]+(?:-[a-z0-9]+)*$`) — this is also the traversal guard (an id can never carry a
  path separator).
- **body** = an `# <id>` header, a `**Intent:**` one-liner, then the attitude/procedure prose.
- **versioning** = git. The file *is* the version; a change to a skill is a normal reviewed commit,
  so every dispatch that references it gets the same, current wording.

### Seed skills (extracted, not invented)

Three skills that already exist as repeated prose in the role prompts today:

| id | extracted from |
|---|---|
| `no-false-success` | `manager.md` ("Reject premature completion"; "claiming completion without observable proof" is a critical failure) + `worker.md` ("Done means the requested outcome works in real conditions … committed and recorded"; "Treat anomalies … as signs that the work is unfinished") |
| `reuse-before-build` | `manager.md` (warm-worker reuse: "a cheap resume, not a cold boot"; "reuse the one you have") + the project rule "Minimal diff / least action … no drive-by refactors" |
| `verify-claims-in-git` | `manager.md` ("Verify the worker's committed diff in git … never accept a summary"; "never trust … over the repository") + `worker.md` ("The commit is your unit of evidence … the Manager reviews your committed diff in git, not your prose") |

## Reference syntax + injection seam

The Manager passes an optional `skills: list[str]` to `dispatch_worker`
(`scripts/mcp_manager.py`). When `SKILLS_LIBRARY_ENABLED` is ON, `_resolve_skills()` resolves each
id against `skills/`, concatenates the texts under a `SKILLS — …` header, and `_dispatch_worker`
**prepends that block to the objective** before it builds the `POST /api/instructions` body
(`description`). That is the whole mechanism-(A) injection seam — the expanded text rides the
existing objective field to the worker's first assignment turn, on any node.

## The flag — default OFF ⇒ byte-identical

`SKILLS_LIBRARY_ENABLED` (env, default OFF). When OFF, `_dispatch_worker` never reads `skills`, so
`description == objective` and the `POST /api/instructions` payload is **byte-identical to today**
— proven by `test_dispatch_worker_flag_off_is_byte_identical` (a dispatch WITH `skills` produces
the same body as one WITHOUT, when OFF). Activation (flip ON) is reserved for the Manager/operator.

## Service-boundary lens (§7) on the resolver

`_resolve_skills` reads model-authored ids that map to filesystem paths, so it is treated as an
external-input boundary:

- **bounded id list** — at most `_MAX_SKILLS` (16) ids per dispatch.
- **charset + containment** — each id must match the id regex AND resolve *inside* `skills/`
  (`Path.relative_to` check), so `../…` / absolute / separator-bearing ids are refused before any
  file is opened.
- **unknown id ⇒ structured error** — a missing `skills/<id>.md` raises a `ValueError` the tool
  layer surfaces as a clean MCP error; it is **never silently dropped**.
- **bounded / malformed file** — a skill file over `_MAX_SKILL_FILE_BYTES` (8 KiB), empty, or
  non-UTF-8 is rejected early.
- **fail-before-dispatch** — resolution runs before any network call, so an invalid id never opens
  a worker session first.

## What remains (not in this slice)

- **Live activation** — flipping `SKILLS_LIBRARY_ENABLED` ON (a Manager/operator decision) and,
  if desired, a gateway restart.
- **Mechanism (B)** — provision `skills/` node-side (deploy or a boot-time fetch endpoint), then
  carry ids for node-local workers to capture the wire/cache savings.
- **Manager-authoring guidance** — teaching `manager.md` to *prefer* `skills=[…]` over re-authored
  attitude prose once the flag is ON (kept out here so the flag-OFF world is unchanged; this is an
  activation-time doc edit, not a code change).
