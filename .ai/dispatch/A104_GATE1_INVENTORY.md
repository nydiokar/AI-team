# A104 Gate 1 — full consumer inventory (Phase 1, read-only)

Linked from `AGENT_104_UNIFIED_AGENT_INBOX.md`. Swept 2026-10-09 17:30–17:50Z on `feat/unified-agent-inbox`
(= `main` @ `d6553fa` + docs). Line numbers are current at that commit (several packet line refs were stale;
the corrected ones are here). Live data read with `?mode=ro` only.

## Reproduce (non-paid)
```bash
rg -n "list_flow_events\(" src scripts | grep -v "def list_flow_events" | wc -l          # 18
rg -n "compute_continuation_tick|continuation_watermark|reviewed_task_ids|pending_case_outbox|record_continuation_consumed|reconcile_worker_waits|boot_reconcile_case|arm_wait_group|record_worker_wait" src scripts
rg -c "completion_outbox" src scripts                                                       # orchestrator 2, db 11, a84_outbox_e2e 4
rg -n "get_session_turns\(" src scripts                                                     # transcript.py:239, backfill_conversation_turns.py:173
rg -n "sender_session_id" src/control/db.py src/orchestrator.py src/control/control_api.py
sqlite3 "file:$HOME/ai-team-data/controller/state/mesh.db?mode=ro" "select continuation_mode,status,count(*) from flow_runs group by 1,2"
```

## Packet corrections (verified, evidence inline)
1. **`task.finished` is NOT in the terminal txn.** `complete_turn` (db.py:5581, one `_write()` :5624–5728) writes the
   terminal row + `task_events` + outbox row (:5712); `task.finished`, the `flow_links` and telemetry are written AFTER
   commit by the effects consumer `_apply_managed_turn_projections` (orchestrator.py:12741/:12794). The inbox row lives
   in the terminal txn (that is the atomic part that matters); `task.finished` stays audit-only.
2. **A second, independent I7 failure on Case 83d10aec.** `task_7b175284` was reviewed (`review.accepted` 16:31Z) but its
   review is event #629; `reviewed_task_ids()` (db.py:8266) reads `list_flow_events` oldest-500, so the outbox row was
   never cleared. Its `task.finished` is event #506 (also outside).
3. **`list_flow_links` is also an oldest-N window** (db.py:6642, `ASC LIMIT 200`) — feeds the brief worker list,
   `interrupt_case`, `sweep_orphaned_cases`. Added to I7 scope.
4. **The `waiting_workers` reason is role-keyed** (`session_reason._is_manager` :102 gates on `case_role=='manager'`).
   Repointing it to `pending_for` also removes that role read.
5. **D7 reaches further than "worker endpoints".** `mcp_manager.py` executes from the checkout of the node that hosts the
   Manager (src/mcp_launchers.py); 124 of 129 Manager links since 2026-09 are on **Horse** (un-redeployed). So until
   Horse is redeployed: no new `requester_session_id` field, old tool texts, and the old tool calls
   (`POST /api/cases/{id}/waits` on every case dispatch, `/wait-group`, `/waits/reconcile`, `/brief`,
   `/api/work/{id}/timeline` as `wait_for_worker`'s completion detector) must keep their shapes. The allowlist is
   worker code too: no tool can be added or renamed (D5 "keep names" is mandatory).
6. **Respawn does not record lineage.** `_respawn_manager_managed` (orchestrator.py:4141–4157) creates the new session
   without `continued_from`; the only successor record is the newest manager `flow_links` row (role-keyed) and the
   `case.manager_respawned{dead_session_id}` event. D4 implementation: set `continued_from=dead_session_id` at respawn
   (role-free lineage) + index `sessions(continued_from)`.
7. **The lost-carrier reaper (PR #203) is gated on the outbox flag** (orchestrator.py:2478) → inert under D1. Its gate
   moves to the inbox (any Case) in Gate 3.
8. **`notify_error` without `chat_id` is a no-op** apart from an observability event (notification_service.py:212); the
   round-cap and headless escalations therefore never reach the operator. The I5 alert reuses the push seam
   (`PushService.fanout` + `notification_chat_id`, as `notify_case_resume_proposal`/`_maybe_push_case_resume` do).
9. Incidental, recorded not fixed unless touched: `sweep_orphaned_cases` awaits the sync `close_case` (orchestrator.py:4648);
   the legacy non-enrolled wake branch (:2191–2241) and `_finalize_continuation` (:4245) are unreachable since A82 8b.

## Design resolutions forced by the inventory (inside D1–D9, not reopening them)
- **R1 Requester identity (I2).** `dispatch_worker` sends no identity today (mcp_manager.py:380–404, header
  `X-AI-Team-Principal: automation`; `sender_session_id` stamped only for `source=="agent_session"`, orchestrator.py:11237).
  Two role-free sources, explicit first:
  1. **explicit** `requester_session_id` in the `/api/instructions` body (new `mcp_manager` reads `SESSION_ID` /
     `AI_TEAM_SESSION_ID` from its env — the same channel `mcp_jobs.py:204` uses; 24/24 recent `jobs` rows carry a
     session id). `InstructionBody` has no `extra="forbid"`, so it is wire-compatible both ways.
  2. **derived** (un-redeployed `mcp_manager`, i.e. every Horse Manager): the requester is the Case member session that is
     **executing a turn right now** and whose executing turn is not itself a dispatched child. Only an executing agent can
     call a tool; this reads the session links + `mesh_tasks` status, never `case_role`. 0 or >1 candidates → no row +
     `inbox.requester_unresolved` audit event (never a guess).
  The stamp goes on `mesh_tasks.sender_session_id` (the existing column / identity path), without the agent-send
  capability check (that path stays for agent sends; its role-pair authZ `_sender_pair_allowed` is authZ, not addressing).
- **R2 Wait conditions (D2/I8)** are keyed by member task ids, not by recipient: an unreleased ALL filter holds every
  inbox row whose `about_task_id` is a member until all member tasks are terminal (a terminal member without a row —
  e.g. unresolved requester — still counts as in, so a filter can never deadlock).
- **R3 `pending_for` returns two things**, one function: inbox rows (`pending`/`delivered`) AND outstanding requests
  (children whose `sender_session_id = recipient` that are not terminal). `waiting_workers` needs the second.
- **R4 Attempts count admissions.** `delivered` increments `attempts`; a withdrawn/refused/failed wake returns its rows to
  `pending` with backoff 30 s·2^(attempts-1); a return at `attempts >= 5` → `dead(attempts_exhausted)` + alert (D3).
  Only `completed` acks (I4); `failed` returns to pending (the packet's "fails → pending").


## db.py inventory (src/control/db.py @ 64ebfa7)

| symbol | db.py line | store | role | fate | consumers |
|---|---|---|---|---|---|
| migration 44 `completion_outbox` + `idx_completion_outbox_pending` | 11397–11409 | DDL [C] | outbox, partial idx on case_id | evolve → I1 inbox (migration 45) | all outbox fns |
| `flow_runs.continuation_mode` | 11409 | DDL | per-Case routing marker | routing deleted; column inert audit | 2899, 5562, 8210 |
| `case_completion_outbox_enabled` + registry desc | 612, 275 | flag | birth flag | delete (Ph5) | 6790 |
| `open_case` birth stamp | 6755 (:6790) | W flow_runs | stamps continuation_mode | stop stamping (Ph5) | case birth paths |
| `_record_case_child_outbox` | 5529 | R flow_links(task); W outbox | writer in terminal txn; addressed by ANY task link (bug) | repoint → inbox writer addressed to child `sender_session_id`; none for NULL/self | 3000, 5712, 5910 |
| `complete_turn` / `resolve_recovery` / `synthesize_managed_terminal` | 5581 / 5832 / 2925 | terminal txns | keep; call inbox writer | task_server complete; recovery; reaper |
| `list_stale_managed_children` | 2858 (:2899) | R tasks⋈flow_runs outbox-only | lost-carrier reaper | keep; drop outbox-mode filter | orch 2485 |
| `case_continuation_mode` | 8200 | R | mode branch | delete | orch 2044 |
| `pending_case_outbox` | 8220 | R ASC 256 | producer read | → `pending_for` | orch 2341 |
| `mark_case_outbox_delivered` | 8235 | W | reviewed_in_turn | → `ack` transition | orch 2349 |
| `reviewed_task_ids` | 8266 | R list_flow_events(500) | review consumption | delete; record_review acks inbox | orch 2342 |
| `_mark_outbox_delivered_conn` | 8287 | W | ack at finalize | → state machine | 7908, 8071 |
| `list_continuation_rows` / `continuation_watermark` | 7720 / 7730 | R cont rows | consumed set/rounds | delete consumption | 7810; orch 2354 |
| `compute_continuation_tick` | 7762 | R events(500)+watermark | legacy satisfaction, retire_only 7830–55 | delete; ALL → I8 filter | orch 2054, 3772, 12155; db 9049 |
| `record_continuation_consumed` | 7867 | W | legacy ack | delete/shim | orch 4293 |
| `arm_wait_group` | 7654 | W worker.wait_pending | ledger writer | shim (D5) → D2 filter | orch 7020; routes/cases.py:230; db 9151 |
| `record_worker_wait` | 7432 | W | A46 per-task wait | shim | orch 6994; cases.py:187 |
| `reconcile_worker_waits` | 7473 | R/W events | reconcile | shim → pending_for | orch 7042; cases.py:201; db 9126 |
| `backfill_missing_task_finished` | 7540 | W task.finished | ledger repair | audit-only | orch 1364; db 7503 |
| `get_case_brief` | 8943 | R links(200)+events(500)+tick | brief | → pending_for + I7 | orch 3288,3296,3636,3999,4067,7054; cases.py:299 |
| `boot_reconcile_case` | 9100 | R events; re-arm | boot | shim, signature stable (D7) | claude_driver 1950; task_server 919; orch 4187, 7073; cases.py:314 |
| `close_case` | 6917 | R events(500) review gate | close gate | keep; dead(case_closed); I7 | orch 6791,7145,11993,4648; cases.py:122,434 |
| `_cancel_case_pending_dispatch_tokens` | 7018 | W tokens | close discharge | keep | 7016 |
| `list_flow_events` | 7084 | ASC LIMIT 500 | audit | audit-only; fix callers I7 | 18 callers |
| `max_flow_event_ids` | 7096 | R | dispatcher skip | repoint/delete | orch 1560 |
| `latest_spec_review` | 7283 | R events(500) | decompose gate | I7 | decompose |
| `reconcile_finalizers` / `_finalize_producer_token` | 7981 / 8030 | tokens; unbounded re-arm 8080–94 | finalizer | → state machine; bounded | orch 2522 |
| `token_to_turn`/`producer_token_attempt`/`continuation_token_for_turn` | 7926/7943/7967 | tokens | wake id plumbing | shrink | orch 2282,2430,12127,12178,12231 |
| `continuation_task_id`/`producer_turn_id`/`_token_attempt`/`_link_producer_token` | 1098/1123/11898/11906 | ids | cont:→cturn_ | shrink | orch 2194.. |
| `withdraw_turn` | 3987 | effects_state='telemetry'; lineage void only if pending | I6 source | never-run → no telemetry/link | orch 12096, 8788 |
| other never-run telemetry writers | 4129/4141, 4241, 5289, 5482 | effects_state='telemetry' | I6 | distinct status | effects consumer 8333 |
| `finalize_turn_lineage` etc | 3470/3447/4281/4297 | flow_run_id at enqueue | membership before run | I6: void on withdraw | orch 11491, 11548, 11960 |
| `list_flow_links` | 6642 | ASC LIMIT 200 | links | I7 | db 7402,8814,8980; orch 4425,4638,6661,6803,11538,11997,12195; work.py; cost_read_model 220 |
| `get_session_turns` | 6271 | ASC LIMIT; Python filter | chat | I7 newest + SQL exclude | transcript.py:239 |
| `enqueue_turn` sender | 3077 (:3266–3317, :3398) | only sender_session_id writer; needs turn_source=agent + capability | agent send | keep; dispatch identity | orch 11280; turn_admission 225; control_api 251 |
| `issue/validate/revoke_sender_capability` | 4509/4614/4673 | caps | identity | keep | task_server 1143 |
| `_sender_pair_allowed` | 11670 | case_role authZ | authZ only | keep (not addressing) | db 3304 |
| `_case_latest_manager`/`case_manager_session_id` | 11679/8810 | newest manager link | successor | D4 source (rebind record) | |
| `record_respawn_link` | 8649 | W manager link + case.manager_respawned{dead_session_id} | rebind record | keep; D4 | orch respawn |
| `turn_held_by_case_rebind`/`withdraw_rebound_automation` | 8745/8759 | rebind withdraw | keep; returns msgs to pending | orch 2364; claim 5243 |
| `ensure_cache_heartbeat_owner` (case_wait_group) | 10442 | heartbeat | liveness | → pending_for | orch 1597 |

Terminal txn: complete_turn 5624–5728 one _write(): terminal UPDATE (effects_state='pending') → task_events → outbox (5712) → identity. task.finished + flow_links + telemetry written AFTER commit by effects consumer `_apply_managed_turn_projections` (orch 12741/12794). Legacy complete_task (3005) writes no outbox row.

Lineage D4: sessions.continued_from (new→old, INSERT only, no index, no reader). Case manager rebind = newer flow_links role='manager' (record_respawn_link) + event case.manager_respawned{dead_session_id}. Workers: no successor.

sender_session_id: only writer enqueue_turn; requires turn_source='agent', flow_run_id, live capability matching session+Case; refuses self-sends; fanout 30/10min. Caps minted at managed claim (task_server 1143), revoked on rebind/close/case change. Worker dispatch never sets it.

list_flow_events callers (18): db 6989 close_case, 7295 latest_spec_review, 7458 record_worker_wait, 7508 reconcile_worker_waits, 7578 backfill, 7684 arm_wait_group, 7787 compute_continuation_tick, 8275 reviewed_task_ids, 8981 get_case_brief, 9133 boot_reconcile_case; orch 1613 _cache_heartbeat_owner_live, 2097 _continue_case_once round-cap idempotency, 3711 resume_case (limit 1000), 4328 _escalate_headless_case, 4410 interrupt_case, 6759 close_case advancement; routes/work.py 105 detail count, 124 timeline.

## orchestrator.py / session_reason / turn_scheduler inventory
| symbol | file:line | store | role | fate | consumers |
|---|---|---|---|---|---|
| `_wake_dispatcher_tick_once` | orchestrator.py:1513 | R list_open_cases, max_flow_event_ids; calls finalizer :1537, recovery :1541, per-Case :1576, heartbeats :1589 | tick | keep; iterate recipients with ready `pending_for` rows; drop event skip-cache :1568-1586 | loop :1505 |
| `_continue_case_once` | :1987 | R mode :2044, tick :2054 / outbox :2046; W retire_only :2064; round cap events :2097; `case_manager_session_id` :2122 | producer (1) | repoint → `pending_for` + D2; delete mode branch/retire_only; recipient = message recipient; I7 :2094 | :1576 |
| cont token creation | :2194, :2261, :2270, :2281, :2295 | W `cont:` sentinel | single-flight | replace with inbox `delivered` (conditional update in admission txn) | `_continue_case_managed` |
| `_continue_case_managed` | :2243 | token; `_enqueue_task` :2303 | wake admission | repoint: admission marks messages delivered | :2190 |
| `_compute_outbox_tick` | :2319 | R pending_case_outbox, reviewed_task_ids; W mark delivered | outbox satisfaction | delete (→ `pending_for`; review ack in record_review txn) | :2046 |
| `_render_wake_turn` | :2534 | — | wake text | keep; input = message about_task_ids | :2216, :2286 |
| `_withdraw_rebound_continuation` | :2364 | withdraw + finalize | rebind | repoint: messages follow lineage (D4), no re-arm | :2131 |
| `_reconcile_continuation_finalizers` → db `reconcile_finalizers`/`_finalize_producer_token` | :2516 → db 7981/8030 | consumed vs re-arm attempt+1 (8075-89) | finalizer (3) | delete re-arm; settlement moves into terminal/withdraw txns (I4/I5) | tick :1537, :2396 |
| `_finalize_continuation` → `record_continuation_consumed` | :4245 → db 7867 | watermark | legacy finalize (unreachable since 8b) | delete | :2233 |
| `_admit_managed_session_turn` | :11189 (sender :11237-42, :11274-82) | W mesh_tasks (+sender) | producer-1 admission | keep; accept validated requester for automation dispatch | `_enqueue_task` :6285 |
| `_admit_managed_producer_turn` | :11372 | admit_turn_async :11446 → db enqueue_turn 3077; lineage :11454; telemetry :11465/8 | wake/retry/respawn/heartbeat admission | keep; carry inbox message ids; never-run → no telemetry/links (I6) | :11212, :11219 |
| `_managed_lineage_converge` → `member()` | :11491 → :11548 | W flow_links task (system/manager), task.attached, affiliation | Case membership; source of junk addressing | audit only; no addressing reads it; I6 for automation kinds | :11587, :11594, :11599, :11607 |
| `_write_managed_lineage` | :11648 | W mesh_tasks.flow_run_id (db 3470) | scope stamp | keep (provenance) | :11297, :11454, :11733 |
| `_record_flow_run_start` (legacy) | :6375 | links/events | one-off path | audit-only | :6303 |
| `_admit_managed_recovery_turn` | :3488 | producer | retry/respawn turns | keep; no inbox row (no requester) | :4161 |
| `_admit_managed_cache_heartbeat` | :1818 | producer | heartbeat | keep; no inbox row | :1774 |
| `_managed_turn_obsolete` | :12096 (tick :12155 → "reviewed") | R token, legacy tick | activation check (2) | **repoint → `pending_for`** (same function as producer) | `_prepare_managed_turn` :12062 ← scheduler |
| turn_scheduler `_activate_head` obsolete branch | turn_scheduler.py:103-133 | withdraw_turn | withdrawal | keep; withdraw txn settles carried messages (I5) | run_scheduler_pass :235 |
| expiry withdraw | turn_scheduler.py:218-234 | withdraw_turn | withdrawal | keep (same settlement) | — |
| `_managed_recovery_obsolete` / `_managed_heartbeat_obsolete` | :12166 / :12218 | tokens | activation | keep | :12121/:12115 |
| `_reap_lost_carriers` (PR #203) | :2461 (outbox-flag gate :2478) | list_stale_managed_children; synthesize_managed_terminal | lost carrier | keep; gate on inbox not flag | :2456 |
| `_handle_dead_manager_session` / `_do_respawn_manager_for_case` / `_respawn_manager_managed` | :3940 / :4023 / :4085 (new session :4141-57, record_respawn_link :4173, boot_reconcile :4187) | respawn | A55 | keep; set `continued_from`; messages follow lineage (D4) | :2171 |
| `_is_restart_dead_session` (A98 O2) / `_mesh_dispatch_payload` (A98 O1) | :7887 / :12805 | session | dead detection / same-id fork | keep | :2158 |
| `resume_case` | :3648 (events limit 1000 :3711; tick :3772) | R | quota resume | keep; I7; generation from inbox | cases.py:394 |
| `interrupt_case` | :4350 (events :4407; links :4425) | R | kill | keep; I7 | cases.py:324 |
| `_escalate_headless_case` / `_escalate_case_continuation_cap` | :4316 / :4302 | events oldest-500; notify_error no chat | alerts | keep; I7; push seam | :2125, :2109 |
| `sweep_orphaned_cases` | :4521 (await sync close_case :4648) | — | orphan sweep | keep; close → dead(case_closed) | route |
| `_handle_quota_paused_case` / `_handle_transient_paused_case` | :3147 / :3375 | pauses | pre-wake gates | keep | :2022/:2029 |
| `_cache_heartbeat_owner_live` | :1597 (fold :1613) | events oldest-500 | heartbeat liveness (owner `case_wait_group`) | → `pending_for` | :1631 |
| `close_case` advancement gate | :6704 (:6759) | events oldest-500 | close gate | keep; I7; dead(case_closed) | cases.py:122/434; :7145; :11993 |
| `record_review` | :6849 | W review.* | ack signal | + ack inbox(about_task_id) | cases.py:155 |
| tool seams record_worker_wait / arm_wait_group / reconcile_worker_waits / get_case_brief / boot_reconcile_case | :6973 / :6999 / :7025 / :7044 / :7059 | ledger | Manager tools | shims (D5/D7) | cases.py routes; :4187 |
| `_recover_stale_busy_sessions` + backfill | :1228 (:1348, :1359) | task.finished | boot | audit-only | start() |
| `_emit_task_finished` / `_apply_managed_turn_projections` / `_run_managed_turn_effects` | :7628 / :12741 / :12546 | task.finished, llm_turns | effects | audit; never-run → no llm_turns (I6) | effects drain :12517 |
| `_void_withdrawn_lineage` | :11960 | links | void | keep (D6) | scheduler |
| `submit_instruction` / control_api `_submit_managed_instruction` | :7689 / control_api.py:188 | metadata | dispatch entry, no sender | stamp requester (R1) | routes |
| `_validate_agent_sender` / `_submit_agent_instruction` | control_api.py:220 / :238 | caps | agent identity | keep | turn_requests.py |
| `session_reason._case_has_unresolved_wait_group` | session_reason.py:106 | events oldest-500 | waiting_workers | delete → `pending_for` | build_reason_batch :217 |
| `build_reason_batch` / `derive_session_reason(s)` / `_is_manager` | session_reason.py:129 / :231 / :270 / :102 | — | reason | repoint, role-free | session_service.py:376; routes/sessions.py:165; session_timeline.py:289 |

Wake lifecycle today: terminal txn writes outbox (any task link) → tick → `_continue_case_once` (outbox tick) → token pending → admit `cturn_*` (+ member() link, task.attached, telemetry) → scheduler `_managed_turn_obsolete` reads LEGACY tick → "reviewed" → `withdraw_turn` (effects 'telemetry' → llm_turns 'cancelled') → finalizer re-arms token attempt+1 → same tick re-admits. Repeat every ~32 s.

I6 never-run write sites: (1) mesh_tasks row via turn_admission.py:211 → db enqueue_turn 3077; (2) flow_links via member() :11551; (3) task.attached via member() :11555; (4) llm_turns via `_emit_turn_telemetry('turn.accepted'/'turn.queued')` :11465/:11468 (+:11308/:11311) and withdraw → effects 'telemetry' → TelemetryStore.reconcile 'cancelled' (telemetry_store.py:866); (5) mesh_tasks.flow_run_id via :11682.

## API / MCP / worker-compat inventory (@64ebfa7)
| symbol | file:line | store (db fn) | role | fate | consumer |
|---|---|---|---|---|---|
| MCP dispatch_worker | scripts/mcp_manager.py:289-485 | POST /api/sessions; POST /api/instructions (no sender); POST /waits → record_worker_wait | dispatch | keep; send requester_session_id from env SESSION_ID/AI_TEAM_SESSION_ID; reply text | Manager LLM |
| POST /api/instructions + InstructionBody | routes/sessions.py:239-303; control_api.py:188-215, 336-358 (no extra=forbid) | submit_instruction 7689 → admission 11237 | dispatch entry | keep; optional requester_session_id; stamp child sender | mcp_manager old+new, web |
| MCP wait_for_worker | mcp_manager.py:534-624 | /api/flows, /api/work/{id}/timeline (oldest 500 → I7 bug: never sees task.finished on >500) | in-turn poll | shim (D5) | Manager |
| MCP reconcile_waits | :631-662 | /waits/reconcile → reconcile_worker_waits | recovery | shim over pending_for, same shape | Manager |
| MCP arm_wait_group | :669-700 | /wait-group → arm_wait_group | arm | shim → D2 filter | Manager |
| MCP get_case_brief | :751-814 | /brief → get_case_brief | brief | repoint, keep keys | Manager, respawn prompt |
| MCP read_session_history | :826-866 | /messages → get_session_turns (oldest N — I7) | history | server-side I7 fix | Manager |
| MCP open_case / close_case / record_review | :873-1024 | cases routes | lifecycle | keep; texts; review acks inbox; close → dead(case_closed) | Manager |
| MCP release_worker / spec tools | :1043-1217 | — | — | keep | Manager |
| mcp_sender send_instruction | scripts/mcp_sender.py:54-81; agent_sender.py:153-198 | turn-requests w/ capability | agent→agent | keep (sender source) | |
| POST /api/sessions/{id}/turn-requests | routes/turn_requests.py:115-215 | validate_sender_capability; admission db 3266-3310 | operator/agent admission | keep; human ⇒ no inbox | UI, sender tool |
| POST /api/turn-requests/{id}/withdraw | turn_requests.py:274-305 | withdraw_turn | operator withdraw | keep; carried msgs → state machine | UI |
| GET /api/flows, /api/flows/{id}, /api/work, affiliations | routes/work.py:27-91 | list/get_flow_run | reads | keep | |
| GET /api/work/{id} | work.py:93-110 | list_flow_events(1000) for count | detail | I7 COUNT | web |
| GET /api/work/{id}/timeline | work.py:112-126 | list_flow_events oldest 500 | audit + old wait_for_worker detector | I7 (newest window; still contains task.finished) | mcp_manager, web |
| GET /api/work/{id}/roster | work.py:142-170 | get_session_turn_counts 10005 | roster | I6 | web |
| POST /api/manager | cases.py:47-90 | invoke_manager 7180 | Manager birth | keep; boot turn → no inbox row | web |
| POST /api/cases, /close, /review | cases.py:92-176 | open/close/append review | lifecycle | keep; review acks inbox | tools |
| POST /api/cases/{id}/waits | cases.py:178-190 | record_worker_wait | durable wait marker | shim: 200 no-op (old mcp_manager calls on every dispatch) | old mcp_manager |
| POST /waits/reconcile, /wait-group, GET /brief, POST /boot-reconcile | cases.py:192-315 | ledger fns | — | shims, same shapes | tools, tests |
| POST /control/cases/{id}/boot-reconcile | task_server.py:904-919 | boot_reconcile_case | worker boot | FROZEN shim (D7): {ok,reason} / {ok,reconciled{resolved,pending},rearmed} | worker controller_state_client.py:99-107 ← claude_driver.py:1950 |
| interrupt / resume-state / resume | cases.py:317-405 | interrupt_case, resume_case | — | keep; resume no ledger re-arm | web |
| GET /api/sessions (reason) | sessions.py:54-69; session_service.py:369-377 | derive_session_reasons | badge | repoint | web |
| GET /api/sessions/{id}/messages | sessions.py:120-135; transcript.py:223,401 | get_session_turns | chat | I7 | web, read_session_history |
| GET /api/sessions/{id}/timeline | sessions.py:137-180; session_timeline.py:76-316 | reasons + telemetry | info tab | I6 | web |
| POST /tasks/{id}/result-managed | task_server.py:1318-1420 | complete_turn → inbox writer | terminal txn | payload frozen | worker agent.py:1803 |
| pending/claim/release-managed | task_server.py:1025,1072,1211 | mesh_tasks | carrier | frozen | worker |
| GET /control/runtime-flags | task_server.py:877 | flags | — | keep serving DURABLE_RELAY_ENABLED | worker |
| _boot_reconcile_manager_case | claude_driver.py:1924-1972 | — | worker boot hook | untouched (D7) | |
| _PROFILE_TOOLS manager_v1 | claude_role_adapter.py:26-44; claude_driver.py:558-585 | — | allowlist | keep 15 names | SDK |
| _render_wake_turn | orchestrator.py:2534-2550 | — | wake text | list inbox messages | Manager |
| respawn prompt | orchestrator.py:4229-4242 | — | "reconcile_waits" text | update | Manager |

Texts to update: mcp_manager.py :422-424, :471-484, :1226-1243, :652-661, :696-700, :795-813, :912-916, :1265, :1277, :1300-1308, :1379-1389, :1396-1452; orchestrator :2538-2550, :4234-4237; docs/harness/roles/manager.md:31-33,146-174,218. Tests pinning: test_mcp_manager.py:815,833,850-879,476-592; compat: test_database_authority.py:139-160, _process:108-111.

D7 reach: mcp_manager runs from the Manager host's checkout (124/129 Managers since 2026-09 on Horse, un-redeployed) → new requester field absent on Horse until worker redeploy → gateway needs a role-free server-side requester resolution for requester-less dispatches. Worker profile has NO dispatch_worker (claude_role_adapter.py:22-25).

## Web UI (all backend repoints; no web change if field shapes stay)
| component | file:line | endpoint | field | fate |
|---|---|---|---|---|
| StatusChip reasonSublabel | web/src/components/ui/StatusChip.tsx:64-69 | props | reason.kind waiting_workers → "on workers" | keep |
| SessionRow | web/src/components/sessions/SessionRow.tsx:113 | GET /api/sessions (session_service.py:376 derive_session_reasons) | reason | repoint backend (session_reason → pending_for) |
| SessionStateSequence (Info tab) | web/src/screens/SessionDetailScreen.tsx:227,289 | /api/sessions/{id}/timeline (session_timeline.py:289) | reason summary | repoint backend |
| SessionTurns "LLM turns (N)" | SessionDetailScreen.tsx:445,457; components/timeline/SessionTurns.tsx:136,214 | /api/turns (monitoring.py:165, llm_turns) | turns, final_status | I6: never-run rows excluded |
| CaseRosterView turnCount | components/work/CaseRosterView.tsx:92 | /api/work/{id}/roster → db.get_session_turn_counts 10006 | COUNT llm_turns | I6 |
| TurnQueuePanel | components/timeline/TurnQueuePanel.tsx; lib/turnQueue.ts | /api/sessions/{id}/turn-requests (turn_requests.py:217) | status/source/kind/sender | keep |
| Chat transcript | hooks/useSessionTimeline.ts:71 | /api/sessions/{id}/messages?limit=1000 → transcript.py:239 → get_session_turns | turns | I7 |
| CaseTimelineView | components/work/CaseTimelineView.tsx:24,47 | /api/work/{id}/timeline?limit=500 (work.py:112) | events | keep audit + newest window |
| WorkDetail counts | screens/WorkDetailScreen.tsx:285,290 | /api/work/{id} (work.py:105, limit 1000) | counts.events | I7 (COUNT) |

## Tests
| module | #tests | pins | breaks? | plan |
|---|---|---|---|---|
| test_case_continuation | 25 | legacy tick/watermark/cont tokens/ANY-ALL/round cap/respawn | yes ~15 | rewrite wake tests on pending_for; keep sweep/respawn |
| test_completion_outbox_drain | 5 | outbox drain | yes | becomes base of Gate 3 matrix |
| test_completion_outbox | 10 | atomic row; T07 legacy no row; T09 no link no row; T11 mode | yes (T07/T09/T11 contradict I2) | update |
| test_completion_outbox_reaper | 14 | lost-carrier synth row | maybe | update |
| test_turn_queue_4c | 32 | real managed wake; Q05/Q06/Q21 pin unbounded re-arm | yes | update heavily (H3 harness) |
| test_session_reason | 18 | waiting_workers from ledger | yes | update |
| test_case_brief | 6 | brief armed groups; boot re-arm | yes | update |
| test_control_api_wait_group | 24 | wait-group + boot routes | maybe | update |
| test_durable_relay | 8 | record_worker_wait/reconcile ledger | yes | rewrite/delete |
| test_recovery_wait_resolution | 8 | backfill resolves groups | yes | update |
| test_wake_dispatcher_eventdriven | 2 | max_flow_event_ids gate | yes | update/delete |
| test_database_authority(+_process) | 24/2 | boot_reconcile route/client signature | no | keep (D7 proof) |
| test_mcp_manager | 66 | tool texts, wait_for_worker polling | maybe | update |
| test_manager_role / test_claude_driver_manager_tools | 32/11 | allowlist | maybe/keep | keep compat |
| test_case_respawn / quota_resume / transient_resume | 5/57/17 | seed via arm_wait_group | maybe | update seeds |
| test_session_cache_heartbeat | 17 | liveness via wait_pending | maybe | update |
| test_turn_queue_4b / 4d / 4e_review / 4e_review_r1 / producers | 51/39/11/14/8 | seeds, finalizer, obsolete withdrawal | maybe | update |
| test_turn_queue_stage6 | 25 | withdrawn turns in API/transcript | maybe | update |
| test_turn_queue_a84_effects | 31 | completion effects | maybe | keep + inbox |
| test_pending_reaper | 17 | cont tokens reaped | maybe | update |
| test_transcript_read_a81 | 3 | ASC + index plan | yes | update |
| test_case_closure / test_review_emitter | 32/15 | close gate / review | maybe | keep + dead/ack |
| others (sender, scheduler, stage8a, flow links, work read model…) | — | — | no | keep |

Harness: H1 `_FakeOrch` (test_case_continuation:33-244) never runs scheduler → activation check untested (why loop passed CI). H3 real managed harness: test_turn_queue_4c.py:31-102 (`_env`, `_tick`, `_pass`, `_run`, `_complete`, `_reconcile`, `_fresh`) + producer1/4b helpers → build Gate 3 on H3. conftest autouse `_isolate_db` file-backed MeshDB per test.

## Delete / shim rows — downstream consumers and how each keeps working (Phase 1 item 2)
| item (fate) | downstream consumers | how they keep working |
|---|---|---|
| `compute_continuation_tick` (delete) | producer :2054, activation :12155, resume_case :3772, brief db 9049 | producer + activation call `pending_for`; resume_case's generation → inbox round count; brief → `pending_for` (same keys) |
| `continuation_watermark` / `list_continuation_rows` / `record_continuation_consumed` / `_finalize_continuation` (delete) | outbox tick :2354; legacy finalize :4293 (unreachable); a84_outbox_e2e script | consumption = message state; round count = acked wake turns from the inbox; e2e script deleted with the outbox drain (its proof is superseded by Gate 3) |
| `_compute_outbox_tick`, `pending_case_outbox`, `mark_case_outbox_delivered`, `reviewed_task_ids`, `_mark_outbox_delivered_conn`, `case_continuation_mode` (delete) | producer only | `pending_for` + state transitions |
| `_finalize_producer_token` re-arm / `reconcile_finalizers` (repoint) | tick :1537, rebind :2396 | settlement happens in the wake turn's terminal txn and in `withdraw_turn`'s txn; the finalizer loop is left only as a sweeper for rows stuck `delivered` on a turn already terminal (crash safety) |
| `cont:` token arithmetic (delete for new wakes) | close discharge db 7018, pending_reaper, activation `continuation_token_for_turn`, rebind | close → `dead(case_closed)`; activation reads the carried message ids from the wake turn; existing `cont:` rows discharged by Phase 4 |
| `arm_wait_group` (shim, D5) | MCP tool via `/wait-group` (old + new mcp_manager), boot re-arm db 9151, heartbeat owner db 7704 | writes the D2 filter row; returns `{ok, reason}` unchanged; boot re-arm becomes a no-op (filters are durable) |
| `record_worker_wait` + `POST /api/cases/{id}/waits` (shim) | **old mcp_manager on every case dispatch** | 200 `{ok:true}` no-op (the inbox row is written by the terminal txn) |
| `reconcile_worker_waits` (shim) | MCP `reconcile_waits` (`/waits/reconcile`), boot_reconcile | returns `{ok, reason, resolved:[{task_id,outcome}], pending:[{task_id}]}` built from `pending_for` |
| `boot_reconcile_case` (shim, D7 frozen) | worker `controller_state_client.py:99` ← claude_driver.py:1950; task_server.py:904; orchestrator :4187, :7073; routes/cases.py:314 | same response shape `{ok, reason}` / `{ok, reconciled{resolved,pending}, rearmed:[]}`; compat test in test_database_authority stays green |
| `get_case_brief` (repoint) | MCP brief, respawn prompt, quota/transient resume, dead-manager handler | keys kept: `workers`, `open_waits`, `ready_waits`, `wait_groups`, `latest_review`, `rounds_*` |
| `wait_for_worker` (shim) | Manager LLM (old tool reads `/api/work/{id}/timeline`) | the timeline route serves newest window (I7) so old `wait_for_worker` sees `task.finished` |
| session_reason ledger fold (delete) | `/api/sessions`, session timeline, monitoring | `waiting_workers` from `pending_for` (outstanding requests or undelivered messages) |
| `_cache_heartbeat_owner_live` wait-group fold (repoint) | heartbeat sync :1631 | owner live ⇔ `pending_for(...)` has outstanding requests for the Case |
| `max_flow_event_ids` skip-cache (delete) | tick :1560 | ready-recipient query is index-served, no skip-cache needed |
| `CASE_COMPLETION_OUTBOX_ENABLED` + `continuation_mode` routing (delete, Phase 5) | open_case :6790, registry :275, reaper gate :2478 | no routing remains; column stays as inert audit; flag registry row left readable (ignored) |
| `get_session_turns` ASC window (fix I7) | transcript.py:239 → `/messages` (web chat, read_session_history), backfill script | newest window, never-run excluded in SQL, ascending order preserved in the response |

## Live data classification (Phase 1 item 3) — snapshot 2026-10-09 ~17:30Z, ro
Genuinely pending: **`task_b7ba302c`** (Case 4d8a46b5, legacy; Manager f078ce07f3d1 closed; token `cont:4d8a46b5…:5`
pending since 2026-10-07T14:20Z) and **`task_e7ae0733`** (Case 534463b6, outbox; failed usage_limit; Manager
0d33070dab74 awaiting_input — the only live Manager; Case quota-paused). `task_7b175284` (83d10aec) is reviewed
(accepted) but its outbox row is still undelivered (correction 2) → must become `acked`. Stuck tokens:
`cont:312ef564…:1` (attempt 661), `cont:83d10aec…:3` (attempt 588), `cont:c96c7785…:1` (attempt 133),
`cont:4d8a46b5…:5` (pre-A82 shape). Junk outbox rows undelivered: 6 (c96c7785 `task_786e66ee`; 312ef564
`task_a6896384`; 534463b6 `cturn_0a26d677…`; 83d10aec `cturn_9a02de18…`, `task_637e22b0`, `task_f5327548`).
Withdrawn mesh_tasks: 1,560 (1,554 `cturn_`). Per-Case detail and the reusable SQL follow.

## Live classification detail (verbatim from the ro sweep)
## Per-Case detail

### The 7 one-turn legacy Cases (4e1895ef, a096b953, a7516d4a, 989ada13, af00910e, 2982ef8d, cec750ec, 9ac80939)
Each has 3 events (`flow.created`, `task.attached`, `task.finished`; a7516d4a has 5 because of one operator message). The only task is the Manager boot turn (`manager_invoke`, completed), plus an operator message `task_b26222da` on a7516d4a. There are no workers, wait groups, `cont:` rows or outbox rows, and every Manager session is closed. These are abandoned Case shells with nothing pending and could simply be closed. af00910e and 8b7466c2 share Manager 87e09f7807b3, whose `current_case_id` points to 8b7466c2.

### 8b7466c2 (legacy, 163 events, 65 task.attached)
- 9 children: 6 completed, 3 failed. Each one was presented by one of `cont:…:1..9`, all completed, so each is consumed. Nothing is pending.
- 49 MANAGER_OWN links (legacy-era own continuation turns) and 7 OPERATOR_MSG links.

### 00c48658 (legacy, BLOCKED, 52 events, 18 task.attached; the A98 incident Case)
- Children on worker b6240c496a45:
  - task_4ca148e1: rework_requested
  - task_a1514c84: accepted
  - task_65319fc4: rework_requested
  - task_12d052f6: completed, consumed by cont:4, never reviewed
- MANAGER_OWN ×6: boot `task_32b3b2de`, continuations `task_cfa2313f`/`fd088716`/`f7320a28`, the failed `task_0aed6f65`, and heartbeat `task_5861db90`. OPERATOR_MSG ×1: `task_3b066daa` (failed).
- WORKER_SESSION_SYSTEM ×7 (`watched_job`): task_48a671db, e316922d, 00f9c229, 46a36566, fedd74a0, d3a1a477, 39e71b2c.
- No open wait groups and no pending token. Nothing is genuinely pending. The Manager is dead (`closed`, driver lost).

### 4d8a46b5 (legacy, 43 events, 12 task.attached): GENUINE PENDING
- Children:
  - f80b3bd8: accepted
  - b81cbb47: failed, rework_requested
  - b97f7ffa: failed, rework_requested
  - 6c65cb72: accepted
  - **b7ba302c**: completed 2026-10-07T14:14:56Z, unreviewed and unconsumed
- Wait group `s6-validation` (ANY, [task_b7ba302c]) has been open since then. Token `cont:4d8a46b5…:5` was created 2026-10-07T14:20:31Z, is still `pending`, and has never been updated. Its payload has no `attempt` key (legacy, pre-A82 shape), and it presents task_b7ba302c.
- Manager f078ce07f3d1 is `closed`. The finished worker result was never delivered to anyone.
- MANAGER_OWN ×5, OPERATOR_MSG ×2 (task_b036f886, task_c91d9e26).

### c96c7785 (outbox, 136 events, 133 task.attached)
- Its only task is the boot turn `task_786e66ee` (MANAGER_OWN). That turn got an outbox row (`outcome=success`, still pending), which is the wrong-addressing defect.
- `cont:c96c7785…:1` is pending with attempt 133. It produced 132 withdrawn `cturn_` wakes, each with a link and a `task.attached` event, between 2026-10-08T08:42Z and 10:02Z (its last update was 10:03Z).
- Manager 26375814992b is closed. No workers. Pure junk.

### 534463b6 (outbox, 133 events, 111 task.attached): GENUINE PENDING; the only live Manager
- Manager 0d33070dab74 is `awaiting_input`. The Case is quota-paused: `flow.quota_paused` at 11:27:10Z on `cturn_0a26d677…` (usage_limit), followed by `approval.requested appr_959c3efb247d` at 14:10Z. While the pause is open, no gen-5 token is minted, so this Case is not looping.
- Children:
  - 2a4276bb: accepted
  - d70a8aca: accepted
  - 4c0fd8cf: accepted
  - 7f714e51: consumed by cont:4, outbox delivered `wake`, not reviewed
  - **e7ae0733**: failed (usage_limit) at 11:26:46Z, unreviewed, unconsumed, outbox PENDING
- Junk outbox rows: boot `task_04522111` (delivered wake), operator messages `task_920ec6ac` and `task_95497872` (delivered wake), and wake turns `cturn_2affab0c…` and `cturn_14870664…` (delivered wake). `cturn_28a1e294…` was delivered too. `cturn_0a26d677…` (failed) is **pending**.
- Wake turns: 3 completed, 1 failed, 99 withdrawn (10:15Z–11:25Z).
- Open wait groups: 4, all ANY, and all contain e7ae0733. None ever got `wait_resolved`, because outbox mode never retires them.

### 312ef564 (outbox, 664 events, 661 task.attached): runaway loop #1
- Its only task is the boot turn `task_a6896384` (MANAGER_OWN, `manager_invoke`). That row has a pending outbox entry, and `cont:312ef564…:1` presents it.
- The token is at **attempt 661** with 660 withdrawn wakes (10:20:25Z → 16:57:18Z). The token was last updated at 16:57:52Z.
- Manager 5a23135eeb97 is closed, and its `current_case_id` is 83d10aec. This Case is a duplicate opened 2 minutes before 83d10aec by the same Manager session. It has no workers and nothing genuine.

### 83d10aec (outbox, BLOCKED, 650 events, 635 task.attached): runaway loop #2
- Children:
  - task_b6fb1008 (worker c1c638847ef3): accepted, delivered `reviewed_in_turn`
  - task_e8f72394 (worker c1c638847ef3): accepted, delivered `reviewed_in_turn`
  - task_7b175284 (worker 243299713009): finished at 15:16:28Z, `review.accepted` at 16:31:05Z, but its outbox row is still **PENDING** and it is not in the watermark
- Root cause of the stale row, new evidence: the review is event #629 in the Case's ledger. `reviewed_task_ids()` (db.py:8266) reads `list_flow_events`, which returns the oldest 500 events, so `_compute_outbox_tick` (orchestrator.py:2319) never sees this review and never suppresses the row. The `task.finished` for 7b175284 is event #506, also past the 500-row window.
- Junk outbox rows:
  - wake `cturn_1756b4b3…`: delivered
  - wake `cturn_9a02de18…`: pending
  - operator `task_637e22b0` (14:28Z): pending
  - operator `task_f5327548` (16:29Z, "the worker didn't notify u…"): pending
- `cont:83d10aec…:3` is pending at **attempt 588** and presents [cturn_9a02de18…, task_637e22b0, task_7b175284, task_f5327548]. Wakes: 628 withdrawn (10:35:21Z → 16:48:03Z) and 2 completed.
- Open wait groups: preflight, instrument and gpu-enable. All three are ALL groups with one member each, every member is reviewed, and none has `wait_resolved`.
- Nothing is genuinely pending, because the only real child (7b175284) has been reviewed. A migration should mark its outbox row delivered (`reviewed_in_turn`) and drop the 3 junk rows.

## Stuck continuation tokens (all `cont:%` with status pending/claimed; there are no claimed rows)

| token | status | attempt | presents | last update |
|---|---|---|---|---|
| cont:312ef564a2b142d49b1f50d74a29bc9a:1 | pending | **661** | [task_a6896384] (boot) | 2026-10-09T16:57:52Z |
| cont:83d10aec8e2445679eff537908f4f536:3 | pending | **588** | [cturn_9a02de18f39c1be04b6fff9f, task_637e22b0, task_7b175284, task_f5327548] | 2026-10-09T16:48:44Z |
| cont:c96c77855a4c4bbab95faef631df4205:1 | pending | 133 | [task_786e66ee] (boot) | 2026-10-08T10:03:28Z |
| cont:4d8a46b52d8a48bfa965350aa9bf0a64:5 | pending | (none, legacy shape) | [task_b7ba302c] | 2026-10-07T14:20:31Z |

Completed tokens with high attempt counts, which shows the loop pattern also hit rounds that eventually delivered:
- 534463b6: :1 = 29, :2 = 13, :3 = 30, :4 = 31
- 83d10aec: :2 = 42
- 7e138377 (closed): :2 = 33

`cont:` totals overall: 794 completed, 4 pending, 2 cancelled.

## Counts

- Withdrawn `mesh_tasks` rows: **1,560** total, of which 1,554 are `cturn_`. By `flow_run_id`:

  | flow_run_id | withdrawn rows |
  |---|---|
  | 312ef564 | 660 |
  | 83d10aec | 628 |
  | c96c7785 | 132 |
  | 534463b6 | 99 |
  | 7e138377 (closed) | 34 |
  | fc6661f4 (closed) | 2 |
  | NULL | 5 |

- `cturn_` overall: 1,554 withdrawn, 19 completed, 2 failed.
- Junk task links (WAKE + MANAGER_OWN + OPERATOR_MSG) and `task.attached` events per Case:

  | case | junk links | task.attached |
  |---|---|---|
  | 312ef564 | 661 | 661 |
  | 83d10aec | 632 | 635 |
  | c96c7785 | 133 | 133 |
  | 534463b6 | 106 | 111 |
  | 8b7466c2 | 56 | 65 |
  | 00c48658 | 7 (+7 watched_job) | 18 |
  | 4d8a46b5 | 7 | 12 |
  | a7516d4a | 2 | 2 |
  | other 7 legacy shells | 1 each | 1 each |

  Across open Cases: 1,618 `system` task links and 26 `manager` task links.
- Junk outbox rows on open Cases: 13 in total, of which 6 are pending:
  - c96c7785: task_786e66ee
  - 312ef564: task_a6896384
  - 534463b6: cturn_0a26d677…
  - 83d10aec: cturn_9a02de18…, task_637e22b0, task_f5327548

  Real-child outbox rows still pending: task_e7ae0733 (genuine) and task_7b175284 (already reviewed).

## sender_session_id

Exactly **1** `mesh_tasks` row has a non-null `sender_session_id`: `task_834db4e9`.

| field | value |
|---|---|
| session | ee93d4266cd2 |
| sender | 53dc2f3c328c |
| action | resume_session |
| status | completed |
| turn_source / turn_kind | agent / instruction |
| idempotency_scope | `agent:53dc2f3c328c:ee93d4266cd2:instruction` |
| source | `agent_session` |
| Case | fc6661f4 (closed) |
| created | 2026-10-07T16:52:54Z |

This is a `mcp_sender` agent-to-agent message. No dispatched worker child sets the field today, so the inbox addressing has to be backfilled from `flow_links` (created_by='manager') plus the Case's manager session link, not from `sender_session_id`.

## Exact SQL (run with `sqlite3 "file:$HOME/ai-team-data/controller/state/mesh.db?mode=ro" "<sql>"`)

```sql
-- Q1 open/blocked Cases
SELECT flow_run_id, COALESCE(continuation_mode,'legacy') AS mode, COALESCE(status,'') AS status, created_at,
       (SELECT COUNT(*) FROM flow_events e WHERE e.flow_run_id=fr.flow_run_id) AS events
FROM flow_runs fr WHERE COALESCE(status,'') NOT IN ('closed','cancelled') ORDER BY created_at;

-- Q2 session links per Case (manager/worker) + liveness
SELECT l.id AS link_id, l.entity_id AS session_id, l.role, l.created_by, l.created_at,
       s.status, s.driver_status, s.current_case_id, s.case_role, s.backend, s.machine_id
FROM flow_links l LEFT JOIN sessions s ON s.session_id=l.entity_id
WHERE l.flow_run_id=:case AND l.entity_type='session' ORDER BY l.id;

-- Q3 task links per Case with classification inputs
SELECT l.id AS link_id, l.entity_id AS task_id, l.role AS link_role, l.created_by,
       t.action, t.status, t.session_id, t.sender_session_id, t.turn_source, t.turn_kind,
       t.idempotency_scope, t.queue_protocol, t.created_at, t.completed_at,
       json_extract(t.payload,'$.metadata.source') AS src
FROM flow_links l LEFT JOIN mesh_tasks t ON t.id=l.entity_id
WHERE l.flow_run_id=:case AND l.entity_type='task' ORDER BY l.id;

-- Q4 ledger facts (finished / reviews / wait groups); order by id, last pending vs resolved wins per group
SELECT id, event_type, entity_type, entity_id, payload_json FROM flow_events
WHERE flow_run_id=:case AND (event_type IN ('task.finished','worker.wait_pending','worker.wait_resolved')
      OR event_type LIKE 'review.%') ORDER BY id;

-- Q4b open wait groups directly
SELECT p.flow_run_id, p.entity_id AS wait_group, json_extract(p.payload_json,'$.condition') cond,
       json_extract(p.payload_json,'$.member_task_ids') members, p.created_at
FROM flow_events p
WHERE p.event_type='worker.wait_pending' AND p.entity_type='wait_group'
  AND p.flow_run_id IN (SELECT flow_run_id FROM flow_runs WHERE COALESCE(status,'') NOT IN ('closed','cancelled'))
  AND NOT EXISTS (SELECT 1 FROM flow_events r WHERE r.flow_run_id=p.flow_run_id AND r.event_type='worker.wait_resolved'
                  AND r.entity_type='wait_group' AND r.entity_id=p.entity_id AND r.id>p.id);

-- Q5 continuation tokens per Case (watermark = union of result.consumed_task_ids over status='completed')
SELECT id, status, json_extract(payload,'$.attempt') AS attempt, json_extract(payload,'$.presented_task_ids') AS presented,
       producer_turn_id, result, updated_at
FROM mesh_tasks WHERE action='manager_continuation' AND id LIKE 'cont:' || :case || ':%' ORDER BY id;

-- Q5b stuck / high-attempt tokens
SELECT id, status, json_extract(payload,'$.attempt') att, updated_at FROM mesh_tasks
WHERE id LIKE 'cont:%' AND (status IN ('pending','claimed') OR CAST(json_extract(payload,'$.attempt') AS INT) > 3) ORDER BY id;

-- Q6 outbox per Case
SELECT * FROM completion_outbox WHERE case_id=:case ORDER BY created_at;

-- Q7 genuine pending (set-based; consumed watermark via json_each over completed cont rows)
WITH oc AS (SELECT flow_run_id FROM flow_runs WHERE COALESCE(status,'') NOT IN ('closed','cancelled')),
child AS (SELECT l.flow_run_id, t.id, t.status FROM flow_links l JOIN mesh_tasks t ON t.id=l.entity_id
          WHERE l.entity_type='task' AND l.created_by='manager' AND l.flow_run_id IN (SELECT * FROM oc)),
consumed AS (SELECT substr(m.id,6,32) AS case_id, j.value AS tid FROM mesh_tasks m, json_each(json_extract(m.result,'$.consumed_task_ids')) j
             WHERE m.action='manager_continuation' AND m.status='completed' AND m.id LIKE 'cont:%')
SELECT c.flow_run_id, c.id, c.status FROM child c
WHERE c.status NOT IN ('queued','pending','claimed','running','recovery_required')
  AND NOT EXISTS (SELECT 1 FROM flow_events e WHERE e.flow_run_id=c.flow_run_id AND e.event_type LIKE 'review.%'
                  AND e.entity_type='task' AND e.entity_id=c.id)
  AND NOT EXISTS (SELECT 1 FROM consumed x WHERE x.case_id=c.flow_run_id AND x.tid=c.id)
  AND NOT EXISTS (SELECT 1 FROM completion_outbox o WHERE o.child_task_id=c.id AND o.delivered_at IS NOT NULL);
-- -> 534463b6…|task_e7ae0733|failed ; 4d8a46b5…|task_b7ba302c|completed

-- Q8 junk outbox rows (no manager-created task link)
SELECT o.* FROM completion_outbox o JOIN flow_runs f ON f.flow_run_id=o.case_id
WHERE COALESCE(f.status,'') NOT IN ('closed','cancelled')   -- -> 13 rows (6 undelivered)
  AND NOT EXISTS (SELECT 1 FROM flow_links l WHERE l.flow_run_id=o.case_id AND l.entity_type='task'
                  AND l.entity_id=o.child_task_id AND l.created_by='manager');

-- Q9 counts
SELECT COALESCE(flow_run_id,'<null>'), COUNT(*) FROM mesh_tasks WHERE status='withdrawn' GROUP BY 1;
SELECT flow_run_id, COUNT(*) FROM flow_events WHERE event_type='task.attached' GROUP BY 1;
SELECT l.flow_run_id, COUNT(*) FROM flow_links l LEFT JOIN mesh_tasks t ON t.id=l.entity_id
 WHERE l.entity_type='task' AND l.created_by='system' GROUP BY 1;   -- junk-candidate links (incl. 00c48658 watched_job)

-- Q10 sender_session_id
SELECT id, session_id, sender_session_id, action, status, turn_source, turn_kind, flow_run_id, idempotency_scope, created_at
FROM mesh_tasks WHERE sender_session_id IS NOT NULL;

-- Q11 event position (shows reviews hidden past list_flow_events' 500-row window)
SELECT (SELECT COUNT(*) FROM flow_events e2 WHERE e2.flow_run_id=e.flow_run_id AND e2.id<=e.id) AS pos, event_type, entity_id
FROM flow_events e WHERE flow_run_id=:case AND event_type IN ('review.accepted','review.rework_requested','review.waived','task.finished','worker.wait_pending');
```
