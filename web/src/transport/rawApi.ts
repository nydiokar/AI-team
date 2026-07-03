/**
 * Raw backend payload shapes — EXACTLY as `src/control/dashboard.py` returns
 * them. These are the snake_case rows that must NOT leak into components
 * (spec §11.1); the adapters in this directory translate them into ../domain.
 *
 * Verified against dashboard.py + core/view_models.py + control/db.py
 * (2026-06-22). If the backend shape changes, change it HERE and in the adapter,
 * never in components.
 */

// GET /api/sessions → { sessions: RawSessionView[] }
// Shape = core.view_models.SessionView.to_dict() (asdict of the dataclass).
export interface RawSessionView {
  session_id: string;
  backend: string;
  repo_path: string;
  /** SessionStatus value: idle|busy|awaiting_input|error|cancelled|closed. */
  status: string;
  machine_id: string;
  /** Native session id the backend returned (resume key) — "" when not captured. */
  backend_session_id: string;
  model: string | null;
  /** The backend's default model — shown when `model` is null. */
  default_model: string | null;
  last_task_id: string;
  last_summary: string;
  last_files_modified: string[];
  needs_input: boolean; //  status == awaiting_input
  is_active: boolean; //    status not in {closed,error}; cancelled remains resumable
  origin_channel: string;
  origin_kind: string;
  updated_at: string;
}

// GET /api/nodes → { nodes: RawNode[] }
// db.list_nodes() rows + dashboard._annotate_node_liveness() derived fields.
// NOTE: trust `live` + `heartbeat_age_sec` (derived per-request), NOT `status`
// (a stale column another process owns) — gap-doc §2.

/** One active task as reported in a worker's live_state heartbeat payload. */
export interface RawLiveStateTask {
  task_id: string;
  backend: string;
  /** Mesh action: run_oneoff | run_session | create_session | resume_session | compact_session | cancel */
  action: string;
  /** Execution phase (optional — worker schema v2+). */
  phase?: string;
  /** ISO timestamp when the task started on this worker; null when not captured. */
  started_at: string | null;
}

/** Worker live_state snapshot — sent every ~30 s via heartbeat. */
export interface RawLiveState {
  slots_used?: number;
  slots_total?: number;
  /** Task IDs currently executing (summary list). */
  active_tasks?: string[];
  /** Rich per-task metadata (superset of active_tasks). */
  active_task_details?: RawLiveStateTask[];
  canary?: boolean;
  incarnation_id?: string;
  /** Schema version for forward-compat. */
  v?: number;
}

export interface RawNode {
  node_id: string;
  tailscale_ip: string;
  api_port: number;
  /** JSON-encoded array string OR already-parsed — see adapter (defensive). */
  backends: string | string[];
  max_concurrent: number;
  /** stale column — DO NOT use for liveness; here only for completeness. */
  status: string;
  last_heartbeat: string;
  registered_at: string;
  updated_at: string;
  // ── derived by dashboard._annotate_node_liveness ──
  live: boolean;
  heartbeat_age_sec: number | null;
  // ── live_state: worker heartbeat snapshot (already present in API response) ──
  live_state?: RawLiveState | null;
  live_state_updated_at?: string | null;
}

// GET /api/tasks → { tasks: RawTask[] }
// db.list_tasks() = `SELECT * FROM mesh_tasks`. PK column is `id` (the dashboard
// JS reads `task_id||id`); mesh status set: pending|claimed|completed|failed|
// failed_node_offline (NOT the 4-state TaskStatus enum).
export interface RawTask {
  id: string;
  session_id: string | null;
  machine_id: string | null;
  backend: string;
  action: string; // create_session|resume_session|run_oneoff|cancel|compact_session
  payload: string; // JSON
  status: string; // mesh status
  claimed_by: string | null;
  claimed_at: string | null;
  completed_at: string | null;
  result: string | null; // JSON ExecutionResult
  error: string | null;
  artifact_path: string | null;
  parent_task_id: string | null;
  created_at: string;
  updated_at: string;
}

// GET /api/events?since=<offset> → RawEventsResponse
// observability.read_recent_events(): events carry a snake_case `event` name and
// correlation ids; `offset` is fed back as `since` to poll (no replay).
export interface RawEvent {
  /** snake_case operational event name, e.g. "task_received","mesh_dispatch". */
  event: string;
  timestamp: string;
  session_id?: string | null;
  task_id?: string | null;
  node_id?: string | null;
  /** any other operational fields ride along untyped. */
  [k: string]: unknown;
}

export interface RawEventsResponse {
  events: RawEvent[];
  /** byte offset to pass back as ?since= on the next poll. */
  offset: number;
}

// GET /api/tasks?sectioned=true → RawTaskSectionsResponse (Move G′).
// Each task is a RawTask + the backend-derived supervised lifecycle fields
// (control_api.task_lifecycle): `ui_state` (the canonical UI TaskState) and
// `section` (which bucket it belongs to). The backend owns the overlay (a task's
// owning-session status overlaid on the mesh status), so the client trusts these.
export interface RawSectionedTask extends RawTask {
  /** Canonical UI TaskState (matches domain/status TaskState). */
  ui_state: string;
  /** attention | running | queued | failed | recent. */
  section: string;
}

export interface RawTaskSectionsResponse {
  sections: {
    attention: RawSectionedTask[];
    running: RawSectionedTask[];
    queued: RawSectionedTask[];
    failed: RawSectionedTask[];
    recent: RawSectionedTask[];
  };
}

// GET /api/artifacts → { artifacts: RawArtifactSummary[] } (UI-4).
// src.control.artifacts.list_artifacts() — newest-first shallow headers over the
// on-disk results/<task_id>.json files. Per-file detail is on the {task_id} route.
export interface RawArtifactSummary {
  task_id: string;
  artifact_path: string;
  success: boolean;
  /** Free-form artifact timestamp string (not always ISO). */
  timestamp: string;
  file_count: number;
  files_modified: string[];
  has_changes: boolean;
  session_id: string | null;
  parent_task_id: string | null;
}

// GET /api/sessions/{id}/messages → { messages: RawTranscriptTurn[] }.
// src.control.transcript.get_transcript() — one turn per session task_history
// entry (FULL user_message → result_summary), oldest→newest. `result` is "" when
// there genuinely was no output, or an honest "(no output — …)" / "(task failed
// …)" note on a failed turn (never fabricated).
export interface RawTokenUsage {
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  reasoning_output_tokens?: number;
}

export interface RawTranscriptTurn {
  task_id: string;
  timestamp: string;
  success: boolean;
  instruction: string;
  result: string;
  file_count: number;
  /** Token usage for the turn (codex turn.completed); null when not reported. */
  usage: RawTokenUsage | null;
}

// GET /api/artifacts/{task_id} → { artifact: RawArtifactDetail, files: RawRemoteFile[] }.
// src.control.artifacts.get_artifact() + to_remote_files(). Raw stdout/stderr are
// intentionally NOT surfaced here (UI-5 logs).
export interface RawArtifactDetail {
  task_id: string;
  success: boolean;
  timestamp: string;
  execution_time: number | null;
  errors: string[];
  files_modified: string[];
  /** Rich per-file shape when the artifact stored it; else null. */
  file_changes:
    | Array<{
        path: string;
        git_status?: string;
        change_type?: string;
        added_lines?: number | null;
        deleted_lines?: number | null;
      }>
    | null;
  session_id: string | null;
  parent_task_id: string | null;
}

// Normalized changed-file row (artifacts.to_remote_files()). `change` is already
// added|modified|deleted; line counts are null when the artifact didn't store them.
export interface RawRemoteFile {
  path: string;
  change: string; // added|modified|deleted
  added: number | null;
  deleted: number | null;
}

export interface RawArtifactDetailResponse {
  artifact: RawArtifactDetail;
  files: RawRemoteFile[];
}

// GET /api/projects?node_id=<id> → { projects: RawProject[] }
// _list_projects_for_node() in control_api.py. Local: filesystem scan. Remote: DB repos JSON.
export interface RawProject {
  name: string;
  path: string;
}

// GET /api/models?backend=<b> → { backend: string, models: RawModelOption[] }
// config/models.py BACKEND_MODELS catalog.
export interface RawModelOption {
  name: string;
  is_default: boolean;
}

// POST /api/sessions/{id}/upload (multipart) → RawUploadResult
export interface RawUploadResult {
  ok: boolean;
  filename: string;
  size: number;
  /** Relative to repo root: "uploads/<filename>" */
  path: string;
}

// GET /api/jobs → { running: RawJob[], recent: RawJob[] }
// db.list_jobs() rows from mesh_jobs table.
export interface RawJob {
  id: string;
  session_id: string | null;
  node_id: string;
  label: string | null;
  status: string; // running|done|failed|lost
  pid: number | null;
  last_checked_at: string | null;
  last_probe_error: string | null;
  exit_code: number | null;
  notify: number | null;
  notify_agent: number | null;
  created_at: string;
  updated_at: string;
}

// GET /api/mesh/health -> current mesh snapshot, recent samples, reconcile spool.
export interface RawMeshHealthSample {
  id?: number;
  sampled_at: string;
  source: string;
  sessions_busy: number;
  tasks_pending: number;
  tasks_claimed: number;
  nodes_online: number;
  nodes_total: number;
  slots_used: number;
  slots_total: number;
  slots_available: number;
  active_tasks: number;
  stale_busy_sessions: number;
  nodes_with_live_state: number;
  nodes_without_live_state: number;
  stale_live_state_nodes: string[];
}

export interface RawMeshHealthCurrent {
  sessions_total?: number;
  sessions_busy?: number;
  tasks_pending?: number;
  tasks_claimed?: number;
  tasks_completed?: number;
  tasks_failed?: number;
  nodes_online?: number;
  nodes_total?: number;
  schema_version?: number;
  mesh_load?: {
    slots_used?: number;
    slots_total?: number;
    slots_available?: number;
    active_tasks?: number;
    stale_busy_sessions?: number;
    nodes_with_live_state?: number;
    nodes_without_live_state?: number;
    stale_live_state_nodes?: string[];
  };
}

export interface RawMeshReconcileStatus {
  total: number;
  pending: number;
  reconciled: number;
  invalid: number;
  oldest_pending_at: string | null;
  latest_reconciled_at: string | null;
}

export interface RawMeshHealthResponse {
  current: RawMeshHealthCurrent;
  history: { recent: RawMeshHealthSample[] };
  reconcile: RawMeshReconcileStatus;
}

// GET /api/approvals → { approvals: RawApproval[] } (Move H).
// Row from the `approvals` table (control_api). `reversible` is stored 0|1.
export interface RawApproval {
  id: string;
  session_id: string | null;
  task_id: string | null;
  action: string;
  risk: string; // low|medium|high
  reversible: number; // 0 | 1 (SQLite int)
  status: string; // pending|approved|rejected|expired
  requested_by: string;
  resolved_by: string | null;
  payload: string | null; // JSON
  created_at: string;
  resolved_at: string | null;
  expires_at: string | null;
}

// GET /api/turns?session_id=<id> → { turns: RawTurn[] } (LLM turn observability,
// Feature #37). One row per agent turn from the llm_turns projection
// (TelemetryStore.list_turns → _decode_turn). `metrics` is the decoded
// metrics_json — token accounting + per-turn aggregates produced by
// src/core/telemetry_projection.py. All metric fields are optional/nullable
// because coverage varies by backend; the UI degrades to "—" on absence.
export interface RawTurnMetrics {
  // Token accounting (from the merged usage block).
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_creation_tokens?: number | null;
  reasoning_tokens?: number | null;
  context_tokens?: number | null;
  uncached_input_tokens?: number | null;
  // Context-window accounting (Feature #35 — context usage). These are token
  // COUNTS, not a percentage: the backend has no per-model window size to divide
  // by, so the UI shows the count. A true % needs a model-window table later.
  peak_context_tokens?: number | null;
  turn_entry_context_tokens?: number | null;
  turn_exit_context_tokens?: number | null;
  intra_turn_context_growth?: number | null;
  context_window_tokens?: number | null;
  context_used_ratio?: number | null;
  context_remaining_tokens?: number | null;
  total_token_work?: number | null;
  aggregate_input_tokens?: number | null;
  aggregate_output_tokens?: number | null;
  aggregate_cache_read_tokens?: number | null;
  aggregate_reasoning_tokens?: number | null;
  session_cumulative_input_tokens?: number | null;
  session_cumulative_output_tokens?: number | null;
  session_cumulative_cache_read_tokens?: number | null;
  session_cumulative_reasoning_tokens?: number | null;
  session_cumulative_total_tokens?: number | null;
  rate_limit_primary_used_percent?: number | null;
  rate_limit_secondary_used_percent?: number | null;
  // Per-turn aggregates.
  tool_call_count?: number | null;
  subagent_count?: number | null;
  invocations_per_turn?: number | null;
  retry_count?: number | null;
  wall_time_ms?: number | null;
  metric_quality?: string | null; // request | aggregate_only | unavailable
  // Other keys exist; index signature keeps the type open without `any`.
  [key: string]: number | string | null | undefined;
}

export interface RawTurn {
  turn_id: string;
  session_id: string | null;
  task_id: string;
  backend: string | null;
  requested_model: string | null;
  observed_models: string[];
  started_at: string | null;
  ended_at: string | null;
  final_status: string; // running | completed | failed | ...
  timeout_status: string;
  final_exit_code: number | null;
  metrics: RawTurnMetrics;
  coverage: Record<string, unknown>;
  data_quality: unknown[];
}

// GET /api/sessions/{id}/timeline -> durable session-owned execution timeline.
// This is distinct from the live /api/events SSE/poll stream: rows here are
// backend-derived durable read-model facts with explicit confidence/staleness.
export interface RawSessionTimelineItem {
  id: string;
  kind: string;
  source: string;
  durability: string;
  timestamp: string;
  session_id: string | null;
  task_id: string | null;
  turn_id: string | null;
  job_id: string | null;
  node_id: string | null;
  backend: string | null;
  status: string | null;
  confidence: string;
  staleness: string;
  summary: string;
  detail: Record<string, unknown>;
  raw_refs: Record<string, string | number | boolean | null>;
}

export interface RawSessionTimelineResponse {
  items: RawSessionTimelineItem[];
  next_cursor: string | null;
  generated_at: string;
  coverage: Record<string, string>;
}
