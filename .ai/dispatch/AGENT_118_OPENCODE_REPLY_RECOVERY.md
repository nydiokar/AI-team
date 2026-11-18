```yaml
job_id: AGENT_118_OPENCODE_REPLY_RECOVERY
created_at: "2026-11-18T10:34:10.981364+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A118             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-18T10:45:01.322497+00:00"
```

# DISPATCH — A118 · OpenCode managed-turn reply recovery and correlation audit

**Level:** 3 (managed-turn correctness and cross-layer recovery) · **Type:** investigation + code + regression proof
**Authored:** 2026-11-18 · **Packet state:** ready; implementation not started
**Issue:** [#298](https://github.com/nydiokar/AI-team/issues/298)
**Depends on:** none.
**Branch:** fresh `feat/a118-opencode-reply-recovery` from current `origin/main`, isolated worktree; PR, review, push and self-merge under project policy.
**Authorization:** The operator explicitly requested a complete agent handoff to re-check assumptions, correct the implementation plan, and then execute it. This job is authorized through implementation and verification once its P0 evidence review establishes a sound plan. It does not authorize worker restarts or production history mutation.

## Goal

Determine why OpenCode managed task `task_f0cd17ef` in gateway session `a5940c108ba8` ended with “OpenCode finished this turn without a reply” after roughly 33 minutes, despite a prior inspection reporting a completed native assistant response. Reconstruct the event and result path from source and original runtime evidence, identify whether the failure is a local edge case or a structural mismatch in the managed-turn protocol, then repair the actual ownership/correlation/recovery contract and prove the exact user-visible response is preserved without accepting unrelated native-session messages.

## Situation and evidence (treat confidence honestly)

The prior investigation reported:

- The managed task ran for 2,016.55 seconds and failed with the generic no-reply banner.
- The OpenCode native session reportedly contained a completed assistant message (`finish=stop`) with a final answer at 13:07:36 UTC, but that assistant message was parented to a synthetic “Continue if you have next steps…” prompt inserted around compaction, not directly to the gateway prompt ID.
- No provider/quota error was reportedly present. The task activity continued through “Writing response…” and “OpenCode finished.” The reported final answer described completed work and PR #288.
- The earlier diagnosis therefore proposed a compaction-continuation correlation gap, not a timeout or quota failure.

These are inherited investigation notes, **not independently re-established evidence in this checkout**. Local `state/mesh.db` and `logs/carrier_state/opencode-native-sessions.sqlite3` do not contain the supplied session/task IDs. Preserve this distinction in all conclusions; recover the original runtime evidence from its authoritative source if available, and record exactly what cannot be recovered.

Current source observations on `main` (`30f1f53`):

- `src/backends/opencode.py::_send_message` submits a deterministic `messageID` from the managed `turn_uuid` and documents strict `parentID` attribution.
- `_await_reply` polls `/session/{id}/message?limit=100` and accepts only `_find_correlated_response(history, managed_message_id)`. The helper searches the returned tail and matches assistant `parentID`/`parentId` exactly; it does not traverse ancestry or recognize a continuation chain.
- A complete direct child can be returned; after an idle grace, a nonterminal direct child may also be returned. If there is no direct correlated assistant message, `_managed_outcome` emits the generic banner.
- The same backend has explicit managed compaction operations, but their existence alone does not prove how OpenCode-native automatic compaction/continuation is represented during an ordinary turn.
- Existing tests are in `tests/test_opencode_managed_turns.py`, including deterministic fake native histories and managed compaction coverage. Add the regression at the managed-turn boundary, not as an isolated helper-only assertion.

The 33-minute duration is not evidence of a timeout by itself. The agent must trace `TurnControl` expiry/progress, native status/history polling, event stream, carrier result persistence and UI projection before assigning cause. Consider whether provider limit errors, transport/database loss, a late reply, wrong task/session mapping, a response beyond the 100-message history window, compaction continuation ancestry, or multiple terminal assistant messages could explain this particular incident. Rule alternatives in/out with evidence.

## Task

### P0 — Re-establish the facts and correct the plan before coding

1. Read `.ai/CONTEXT.md`, dispatch index/protocol and current Git state. Inspect issue and packet, then re-check symbols in current `origin/main` and the exact deployed source/build from the incident.
2. Recover the original task result/event ledger, native OpenCode message history (including full parent IDs, timestamps, finish/error fields, compaction messages), backend version and relevant gateway/worker build identity. Use read-only access. If original data is unavailable, mark each claim unverified and build a faithful offline fixture from documented OpenCode protocol behavior; do not invent missing evidence.
3. Trace source-to-observation: gateway prompt identity → native prompt submission → native message/session lifecycle through compaction → response selection → `ExecutionResult` → durable carrier/task result → conversation/API/UI rendering. Include managed turn expiration, progress signals, idle grace, history pagination/limits, and late-result recovery.
4. State a root cause only after this trace. Explicitly decide whether the defect reveals a structural contract problem: does the gateway assume one native user message has only direct assistant children while OpenCode can continue the same logical turn under synthetic prompts? Is direct parent ID the right ownership key across compaction/retry? Can a continuation be attributed safely without reading unrelated user activity? Identify any general failure mode beyond this incident.
5. Write a short P0 findings and implementation plan in this packet before touching production code. Include rejected alternatives and why. If the evidence contradicts the inherited diagnosis, revise the scope and issue plan accordingly. Do not preserve the earlier proposal by inertia.
6. Obtain an independent review of the evidence, root-cause reasoning and plan. The reviewer must challenge at least the timeout/quota alternatives, correlation ownership across compaction, unrelated same-session traffic, and the proposed regression boundary. Resolve blockers in the packet. Do not begin code implementation until this review accepts the plan; this is an engineering review gate, not a request for renewed operator approval.

### P1 — Fix the owning contract, not just the observed banner

Implement the smallest coherent change at the established OpenCode managed-turn ownership boundary. The solution must recover the actual terminal response and its text/usage/metadata when it belongs to this managed turn, while refusing unrelated messages from concurrent/manual use of the same native session.

Depending on P0 evidence, consider a durable logical-turn correlation lineage that records known synthetic continuation IDs, an OpenCode-native turn/parent relation, or a safer history reconciliation protocol. Do not merely select “latest assistant after prompt,” increase the history limit, suppress the error banner, or special-case the word “Continue.” Prove how ownership survives compaction and late recovery, how it behaves when native history is truncated or malformed, and how ambiguity fails visibly without losing an already-generated response.

Keep timeout policy unchanged unless P0 demonstrates it caused this incident and the owning timeout contract itself is wrong. Likewise, do not change quota/limit behavior without provider error evidence. Any necessary protocol/API/result shape change must be traced through carrier persistence and the user-facing conversation projection.

### P2 — Regression and adversarial verification

Add deterministic tests at the backend managed-turn seam for at least:

- ordinary direct terminal reply remains unchanged;
- compaction continuation containing the reported synthetic user/continuation message, with terminal assistant response recoverable;
- continuation chains and multiple assistant messages/tool steps, selecting the correct terminal answer;
- unrelated later user/manual turn in the same OpenCode session is never attached to this task;
- no terminal response, native error, malformed/truncated history, history-window/pagination boundary, and ambiguous ancestry each produce a truthful, actionable result;
- recovered response keeps text, usage and relevant metadata through durable task result and visible conversation projection;
- late result / recovery path does not duplicate or misattribute the answer.

Use only focused offline tests for touched modules, per repo test-cost guidance. No full/e2e suite or paid model call. Do not run tests if the project guard would launch a paid CLI. Record exact commands and results.

## Done when

- P0 evidence and plan are in this packet and correct the inherited assumptions explicitly.
- Independent reviewer accepts the P0 root-cause finding and implementation plan before code changes begin; review objections and resolutions are recorded.
- The actual defect is repaired at the ownership/correlation boundary, with regression tests that fail on the pre-fix implementation and exercise the relevant downstream result path.
- A final source-to-observation trace explains how a native terminal response becomes the user-visible answer and which joins/contracts were verified.
- Focused verification, independent review, branch/PR/merge and relevant gateway deployment/read-only verification are recorded. Gateway restart is allowed by policy; **do not restart any worker** without surfacing that decision to the operator.
- Remaining limitations are explicit (including unavailable incident evidence or any native history/API bound). The generic no-reply banner is used only when evidence supports that no owned terminal answer was recoverable.
- Issue and dispatch registry are closed only after proof is linked; this packet remains the lifecycle record.

## Service-boundary / architecture integration

This work changes a backend entry point consuming external/native OpenCode protocol data. At closure, document: serialization/concurrent-turn ownership (existing repo lock/capacity), memory at 100 simultaneous requests (bounded by existing admission or explain observed bound), native response/history payload limits, poll/socket/turn deadlines, malformed history handling, native server loss behavior, and whether late replies retain or release ownership. Do not claim hard native IO deadlines unless measured/enforced. Preserve existing queue/carrier ownership; do not introduce a parallel session store or generic retry system.

## Trail / evidence

- Incident identifiers from the user-provided prior investigation: session `a5940c108ba8`, task `task_f0cd17ef`; reported 2,016.55s runtime.
- Review/implementation issue: [#298](https://github.com/nydiokar/AI-team/issues/298).
- Relevant current code: `src/backends/opencode.py` (`_send_message`, `_await_reply`, `_managed_outcome`, `_find_correlated_response`); focused coverage: `tests/test_opencode_managed_turns.py`.
- Existing related historical fix: GitHub issue/PR #197, which established terminal-only response selection; inspect as context, not as proof that the current incident has the same cause.
- Original incident DB rows are absent from this checkout's `state/mesh.db` and `logs/carrier_state/opencode-native-sessions.sqlite3` as checked on 2026-11-18.
- Add issue link and proof paths here after authoring/implementation. Do not create a sibling report unless evidence genuinely needs a separate artifact under `.ai/dispatch/evidence/`.

### P0 investigation — 2026-11-18

**Incident evidence remains unavailable.** The local ledger, carrier-state database and
session logs do not contain the reported IDs; the running gateway's session endpoint
returned 404. The gateway health reports build `3f0f27f`; that identifies the current
gateway, not the incident worker build. The locally installed OpenCode package is
1.18.32, but the incident worker version is unverified. Consequently the reported
native answer, provider status, duration, mapping and deployed source cannot be
independently confirmed. No production records were changed.

**Confirmed structural defect, not a proven incident cause.** In OpenCode
[v1.18.32 compaction source](https://github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/session/compaction.ts),
automatic continuation creates a fresh user message without a parent ID and marks
its text part synthetic with `compaction_continue`; the source explicitly says the
marker is not a stable plugin contract. The assistant is parented to that new user
message, whereas this gateway only accepts an assistant directly parented to its
deterministic managed prompt. Thus a real continuation can be missed. The same
ownership assumption makes cancellation fail closed after such a continuation.
This evidence does not establish that the particular incident followed that path.

**Trace and alternatives.** The carrier supplies a turn UUID; `_send_message`
submits its derived message ID. While busy, `_await_reply` waits; when idle it reads
at most 100 messages and checks direct parent IDs. After three idle polls with the
prompt present it returns the latest direct step or an empty object; the latter
becomes the no-reply banner and releases ownership. A stall or hard-cap expiry
instead produces `recovery_required` and can start the late watcher; elapsed time
alone cannot establish either. An uncorrelated provider error is also missed, so
quota/provider failure cannot be excluded without incident records. History window
truncation, transport loss and wrong mappings likewise remain unverified. A selected
response is parsed into `ExecutionResult`, then the carrier sends bounded output and
usage in its result envelope; late capture uses turn UUID and the result spool.
The full persisted-result-to-conversation projection has not yet been verified.

**Independent review and plan gate.** A separate reviewer checked the code and
versioned upstream sources, challenged timeout/quota, manual concurrent traffic,
history truncation and regression scope, and rejected attribution by proximity or
the synthetic marker alone. OpenCode's
[prompt implementation](https://github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/session/prompt.ts)
persists a new user message before entering the session loop; the carrier's local
lock does not fence other native clients. No reviewed implementation plan yet
establishes safe ownership across those clients, restarts and incomplete history.
Do not start production code on the assumption that the marker proves ownership.

Next: locate the authoritative incident worker and read-only native/ledger records;
investigate a native-supported provenance or exclusive-writer/fencing contract,
including compatibility and restart behavior. If neither is available, design a
failure-honest recovery state that preserves potentially generated work without
attributing foreign content, and independently review its downstream and lifecycle
semantics before implementation. Test at the managed-turn and late-result seams,
including direct replies, continuation, intervening manual prompts, multiple
continuations, window loss, provider errors and durable visible projection. Do not
change timeout or quota policy absent causal evidence. No tests, paid turns, worker
restarts or deployment were performed during this investigation.

## Milestone

- [ ] P0 original evidence recovered or explicitly unavailable; current runtime/source reconciled; root cause and reviewed plan recorded before code changes.
- [ ] P1 structural ownership/correlation defect fixed at the responsible boundary with no unrelated-message attribution.
- [ ] P2 focused regressions prove compaction continuation, normal response, ambiguity, late recovery, result persistence and user-visible projection.
- [ ] Independent review, merge, runtime verification and dispatch/issue closure evidence recorded.

## Closure

Open. This dispatch creates a reviewable handoff; the inherited diagnosis is not yet independently proven, and no implementation or runtime fix is claimed.
