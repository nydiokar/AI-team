# Codex app-server adapter convergence

Status: implemented behind the existing ``CodexBackend`` entry point. Focused
offline parity is passing; live gateway deployment proof remains operator-gated.
Protocol authority: installed `codex-cli 0.153.2`, generated JSON Schema.

## Original abstraction assessment

`CodingBackend` is synchronous: create runs the first turn, resume runs a later
turn, oneoff runs an untracked turn, cancel requests best-effort interruption,
close releases backend resources, and each execution returns `ExecutionResult`.
Native identity is the existing `backend_session_id`; cwd, model, effort and
optional telemetry already cross this boundary. Callers persist results and IDs.

Codex satisfies the method signatures but not all intended behavioral guarantees:
exit-code success can disagree with native turn failure; a late cancel can replace
successful completion; default compaction sends text instead of invoking native
compaction. These are adapter defects, not reasons to enrich Case/Task APIs.

Create/resume/oneoff/cancel/close/compact are generic semantics. Raw stdout,
stderr and return code are process-shaped diagnostic fields, but optional and
usable for structured diagnostics without requiring callers to parse native RPC.
The existing optional `terminate_active_processes` shutdown hook is process-named;
retain its spelling for compatibility, implementing runtime shutdown underneath.
Existing `prepare_execution`/`cancel_execution` hooks identify a gateway task, not
a native turn; preserve these while mapping internally to exact native IDs.

Generic Session stores the native thread in its deliberately generic identity
slot, backend selection, workspace, model and effort. It has no Codex process or
native turn field. Existing driver fields are unrelated to this migration.
No generic interface change is justified by the native protocol.

## Ownership and composition

Gateway owns Session/Case/Task identity, backend selection, durable queues/leases,
orchestration retries, approvals, Case continuation and closure, audit/telemetry,
workspace affinity and cross-process exclusion. Existing ownership changes in the
working tree are prerequisites and must be preserved, not redesigned here.

`CodexBackend` remains the only production semantic adapter. It composes the bounded
`CodexAppServerClient`, adapter-local native thread/turn state and an event/result
translator. The client owns protocol validation, request correlation, transport,
readers and runtime lifecycle. Native IDs never become gateway RPC arguments.

The adapter owns Session-to-thread mapping, active turn identity, reattachment,
interruption and protocol compatibility. Its durable ownership journal preserves
the thread ID before first execution, including failures before result delivery.
Native active-turn exclusion is additional to gateway distributed ownership:
`turn/start` can steer an existing turn, so it must never be used on an already
active thread. Different session claims do not share an execution mutex.

The harness owns thread context, native tools/model loop, execution, native
lifecycle and context management. Neither transport reconnect nor adapter errors
authorize replaying a possibly executed gateway instruction.

## Desired lifecycle

One intended execution runtime per carrier process, shared by its Codex adapter
calls. Initialize with the pinned protocol before use; reject incompatible
versions and schemas. Test construction must remain free of paid execution.
Serialize runtime initialization only; unrelated threads execute concurrently.

New session: acquire durable session ownership, call `thread/start`, immediately
journal its exact returned ID, then `turn/start`. Later turn: use the loaded
thread directly. Runtime restart: acquire ownership, `thread/resume` by exact ID
with bounded history response, verify returned ID and cwd and idle status, then
start the new turn. Never use history reconstruction, `--last`, or fresh fallback.

Thread configuration must preserve tool SESSION_ID using thread-scoped shell/MCP
configuration, verified under concurrent sessions. Do not bake one session's
environment into a shared runtime. Model/effort remain per-session/per-turn.

Register native event delivery before turn start; bind all lifecycle events to
thread and turn IDs. Completion is authoritative only from terminal native state.
Cancel requests interrupt the captured turn; completed remains completed even if
cancel races. Follow-ups stay queued. Close unloads thread resources; gateway
shutdown interrupts active turns and shuts down/reaps its owned runtime/readers.

## Failure model

| Failure | Authority and action | Continuity / visible result |
|---|---|---|
| Idle runtime death | Replace dead runtime on next use; no prompt replay | Exact thread resumed |
| Runtime death during turn | Journal retains identity; fail current execution | Outcome uncertain; no adapter replay |
| Gateway death during turn | Durable claims remain held until old mutation capability is proven dead | Fail closed; operator recovery for unclean claims |
| Gateway restart | Existing gateway recovery plus exact native reattachment | Same ID, no heuristic lookup |
| Resume fails | Persisted ID authoritative; no new thread | Structured failure with original ID |
| Turn start RPC rejected | Native rejection authoritative | Failed result; gateway owns retry policy |
| Start response lost | Execution may have started; do not replay | Uncertain failure, retained claim until runtime stopped |
| Native turn fails | Terminal status/error authoritative | Failed result preserving output, ID and usage |
| Interrupt/completion race | Terminal status authoritative | Completed remains success; interrupted is cancelled |
| Same Session concurrently | Gateway FIFO plus durable claim and native idle check | No competing turn or implicit steering |
| Different Sessions concurrently | Independent claims/turn channels, bounded capacity | Concurrent execution |
| Two processes / thread alias | Shared durable thread claim | Contender fails closed |
| Malformed/out-of-order events | Validate schema and lifecycle correlation | Fail closed; never infer success |
| Version/schema mismatch | Pinned runtime and generated schema authority | Clear incompatibility before execution |

Transport/request failures must not accidentally acquire retry eligibility by
including retry-looking prose in their public error classification. Preserve
structured native diagnostics separately. Native model retries remain harness
owned; gateway retry policy is not replaced.

## Migration and verification gates

1. The bounded transport validates requests, responses and notifications against
   the generated version-matched schema snapshot. It has offline fake-server
   tests for correlation, death, malformed data, deadlines and limits.
2. The semantic adapter has offline tests for exact IDs, cwd, terminal status,
   interruption, concurrency, durable claims and telemetry projection.
3. The registry now selects only the app-server adapter. CLI-only tests and
   machinery are deleted; there is no fallback or second production backend name.
4. Targeted gateway integration tests and a natural live multi-turn Codex probe
   remain required before deployment. Do not restart worker carriers silently.

Delete CLI exec/resume construction, per-turn Popen registry, stdout prose/exit
completion inference, inactivity kills and rollout-file usage scanning. Retain
durable IDs/claims, gateway queues and retries, affinity and generic telemetry.

## Service boundary obligations

Bound admitted turns, pending RPCs, incoming/outgoing frame sizes and accumulated
turn output. Readers must not block on unbounded consumer queues. RPCs and native
interrupt acknowledgment have deadlines. Invalid JSON, wrong IDs, invalid status
and unsupported server requests fail explicitly; approvals are never granted by
the transport. SQLite unavailability refuses ownership acquisition. Runtime death
fails affected calls. Record measured bounds and any concrete deferrals here and
in CONTEXT before closure.
