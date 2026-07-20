/**
 * Thin fetch client for the embedded Control API (src/control/control_api.py).
 *
 * Auth: Bearer DASHBOARD_TOKEN on every /api/* call (control_api._require_auth).
 * The token is supplied by the operator and held in the auth store; this module
 * stays stateless and takes it per-call.
 *
 * UI-2: write methods added (backend Move F / U3 landed). Each mutation carries
 * an Idempotency-Key the backend dedupes (control_api `_idem` cache), so a retry
 * after a flaky network never double-submits. Rejections come back as
 * {ok:false, reason} with a 4xx and no prose — the client owns the wording.
 */
import type {
  RawSessionView,
  RawNode,
  RawTask,
  RawEventsResponse,
  RawTaskSectionsResponse,
  RawApproval,
  RawArtifactSummary,
  RawArtifactDetailResponse,
  RawTranscriptTurn,
  RawProject,
  RawModelOption,
  RawUploadResult,
  RawJob,
  RawTurn,
  RawMeshHealthResponse,
  RawSessionTimelineResponse,
  RawWorkListResponse,
  RawCaseDetailResponse,
  RawCaseTimelineResponse,
  RawCaseGraphResponse,
  RawSessionAffiliationsResponse,
  RawWorkBucket,
} from "./rawApi";

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function get<T>(path: string, token: string): Promise<T> {
  const res = await fetch(path, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

/**
 * POST a write. On a non-2xx, the backend returns {ok:false, reason, ...} (or a
 * FastAPI {detail} envelope). We surface `reason` as the ApiError message so the
 * caller can map a stable machine code to UI copy — never raw prose.
 */
async function post<T>(
  path: string,
  token: string,
  body: unknown,
  idempotencyKey?: string,
): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const res = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(body ?? {}),
  });
  const data = await res.json().catch(() => ({}) as Record<string, unknown>);
  if (!res.ok) {
    // FastAPI nests the command envelope under `detail`. Our envelope carries a
    // stable machine `reason` and, when a reject has human context (e.g. a bad
    // repo_path → "Path does not exist."), a `detail` string. Prefer that human
    // string; fall back to the machine reason, then the HTTP status line.
    const inner = (data as { detail?: unknown }).detail;
    const envelope =
      inner && typeof inner === "object"
        ? (inner as { reason?: string; detail?: string })
        : (data as { reason?: string; detail?: string });
    const message =
      envelope.detail ||
      envelope.reason ||
      (typeof inner === "string" ? inner : "") ||
      `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, String(message));
  }
  return data as T;
}

/** A fresh idempotency key for one logical mutation attempt (retries reuse it). */
export function newIdempotencyKey(): string {
  return (
    (crypto as { randomUUID?: () => string }).randomUUID?.() ??
    `idem-${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
}

/** Backend command envelope: {ok, reason, session} (control_api._command_envelope). */
export interface CommandEnvelope {
  ok: boolean;
  reason: string;
  session: RawSessionView | null;
}

/** POST /api/instructions response (control_api.api_instructions). */
export interface InstructionResponse {
  ok: boolean;
  task_id: string;
  session: RawSessionView | null;
}

export const api = {
  async sessions(token: string, limit = 200): Promise<RawSessionView[]> {
    const data = await get<{ sessions: RawSessionView[] }>(
      `/api/sessions?limit=${limit}`,
      token,
    );
    return data.sessions ?? [];
  },

  async nodes(token: string): Promise<RawNode[]> {
    const data = await get<{ nodes: RawNode[] }>(`/api/nodes`, token);
    return data.nodes ?? [];
  },

  async tasks(token: string, limit = 50): Promise<RawTask[]> {
    const data = await get<{ tasks: RawTask[] }>(
      `/api/tasks?limit=${limit}`,
      token,
    );
    return data.tasks ?? [];
  },

  /** Sectioned tasks (Move G′): backend-bucketed supervised lifecycle. */
  async taskSections(
    token: string,
    limit = 50,
  ): Promise<RawTaskSectionsResponse> {
    return get<RawTaskSectionsResponse>(
      `/api/tasks?limit=${limit}&sectioned=true`,
      token,
    );
  },

  /** Newest-first artifact summaries (UI-4 — results/<task>.json headers). */
  async artifacts(token: string, limit = 50): Promise<RawArtifactSummary[]> {
    const data = await get<{ artifacts: RawArtifactSummary[] }>(
      `/api/artifacts?limit=${limit}`,
      token,
    );
    return data.artifacts ?? [];
  },

  /** One artifact's header + its normalized changed files (UI-4). */
  async artifact(
    token: string,
    taskId: string,
  ): Promise<RawArtifactDetailResponse> {
    return get<RawArtifactDetailResponse>(
      `/api/artifacts/${encodeURIComponent(taskId)}`,
      token,
    );
  },

  /**
   * The session's real conversation, reconstructed server-side from on-disk
   * artifacts + summary (control_api.api_session_messages). Each turn = user
   * instruction → assistant result, oldest→newest. This is the source the
   * timeline was missing — without it a Telegram-started session looked empty.
   */
  async sessionMessages(
    token: string,
    sessionId: string,
    limit = 1000,
  ): Promise<RawTranscriptTurn[]> {
    const data = await get<{ messages: RawTranscriptTurn[] }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`,
      token,
    );
    return data.messages ?? [];
  },

  /**
   * LLM turn observability for a session (Feature #37). One row per agent turn
   * from the llm_turns projection, newest-first. `metrics` carries the token
   * accounting that also powers the context-usage display (Feature #35).
   * Returns [] when telemetry is unavailable (no TelemetryStore / empty DB).
   */
  async turns(token: string, sessionId: string, limit = 50): Promise<RawTurn[]> {
    const data = await get<{ turns: RawTurn[] }>(
      `/api/turns?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`,
      token,
    );
    return data.turns ?? [];
  },

  /** Durable session-owned execution timeline, distinct from live events. */
  async sessionTimeline(
    token: string,
    sessionId: string,
    limit = 50,
    cursor?: string | null,
  ): Promise<RawSessionTimelineResponse> {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (cursor) qs.set("cursor", cursor);
    return get<RawSessionTimelineResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}/timeline?${qs.toString()}`,
      token,
    );
  },

  /** Live event tail (poll). Pass the returned offset back as `since`. */
  async events(token: string, since = 0, limit = 100): Promise<RawEventsResponse> {
    return get<RawEventsResponse>(
      `/api/events?since=${since}&limit=${limit}`,
      token,
    );
  },

  /** Unauthenticated health probe (control_api /health). */
  async health(): Promise<boolean> {
    try {
      const res = await fetch(`/health`);
      return res.ok;
    } catch {
      return false;
    }
  },

  // ── write surface (UI-2 / Move F) ────────────────────────────────────────

  /**
   * Submit an instruction. With `sessionId` it mirrors the Telegram session path
   * (session → BUSY, source=web_session); otherwise a one-off (source=web_oneoff).
   * Idempotency-keyed: a retry with the same key returns the original task_id.
   */
  async submitInstruction(
    token: string,
    args: {
      description: string;
      sessionId?: string;
      cwd?: string;
      targetFiles?: string[];
      /** [Session-fork] Verbatim digest of the marked messages carried over from a
       *  forked source session — injected once as reference-only prior context on
       *  this (the new session's first) turn. Omit on every normal turn. */
      continueInline?: string;
      /** [Session-fork] Case the forked session belongs to; joins this turn to it. */
      caseId?: string;
    },
    idempotencyKey: string,
  ): Promise<InstructionResponse> {
    return post<InstructionResponse>(
      `/api/instructions`,
      token,
      {
        description: args.description,
        session_id: args.sessionId ?? null,
        cwd: args.cwd ?? null,
        target_files: args.targetFiles ?? null,
        continue_inline: args.continueInline ?? null,
        case_id: args.caseId ?? null,
      },
      idempotencyKey,
    );
  },

  /** [Session-fork] Continue a stalled session as a FRESH session under one Case
   *  (POST /api/sessions/{id}/fork). Creates a new session shaped from `args` and
   *  binds both the source and the new session to a carrier Case. Returns
   *  {ok, new_session_id, case_id}. The marked-message digest is NOT sent here — it
   *  is delivered on the new session's first instruction via `continueInline`. */
  async forkSession(
    token: string,
    sessionId: string,
    args: { backend: string; repoPath: string; model?: string; nodeId?: string; title?: string },
    idempotencyKey: string,
  ): Promise<{ ok: boolean; reason?: string; new_session_id?: string; case_id?: string }> {
    return post(
      `/api/sessions/${encodeURIComponent(sessionId)}/fork`,
      token,
      {
        backend: args.backend,
        repo_path: args.repoPath,
        model: args.model ?? null,
        node_id: args.nodeId ?? null,
        title: args.title ?? null,
      },
      idempotencyKey,
    );
  },

  /** Stop the session's in-flight task (control_api.api_stop_session). */
  async stopSession(
    token: string,
    sessionId: string,
  ): Promise<{ ok: boolean; cancelled: boolean; task_id: string | null }> {
    return post(`/api/sessions/${encodeURIComponent(sessionId)}/stop`, token, {});
  },

  /** Close a session (control_api.api_close_session). */
  async closeSession(token: string, sessionId: string): Promise<CommandEnvelope> {
    return post<CommandEnvelope>(
      `/api/sessions/${encodeURIComponent(sessionId)}/close`,
      token,
      {},
    );
  },

  /** Restore a closed session (control_api.api_restore_session). */
  async restoreSession(token: string, sessionId: string): Promise<CommandEnvelope> {
    return post<CommandEnvelope>(
      `/api/sessions/${encodeURIComponent(sessionId)}/restore`,
      token,
      {},
    );
  },

  /** Pending approval queue (Move H). status="" lists all. */
  async approvals(token: string, status = "pending"): Promise<RawApproval[]> {
    const data = await get<{ approvals: RawApproval[] }>(
      `/api/approvals?status=${encodeURIComponent(status)}`,
      token,
    );
    return data.approvals ?? [];
  },

  /** Resolve a pending approval (control_api.api_resolve_approval). */
  async resolveApproval(
    token: string,
    approvalId: string,
    decision: "approved" | "rejected",
    idempotencyKey: string,
  ): Promise<{ ok: boolean; approval: RawApproval | null }> {
    return post(
      `/api/approvals/${encodeURIComponent(approvalId)}/resolve`,
      token,
      { decision },
      idempotencyKey,
    );
  },

  /** Create a web-origin session (control_api.api_create_session).
   *  `roleBoot: "worker"` boots the session with the canonical Worker role
   *  profile; omit it for a bare (tier-0) session. */
  async createSession(
    token: string,
    args: { backend: string; repoPath: string; model?: string; nodeId?: string; roleBoot?: string },
    idempotencyKey: string,
  ): Promise<CommandEnvelope> {
    return post<CommandEnvelope>(
      `/api/sessions`,
      token,
      {
        backend: args.backend,
        repo_path: args.repoPath,
        model: args.model ?? null,
        node_id: args.nodeId ?? null,
        role_boot: args.roleBoot ?? null,
      },
      idempotencyKey,
    );
  },

  /** Invoke the autonomous Manager loop (control_api.api_manager → POST /api/manager).
   *  Boots a Case-owning Manager session with the Manager role profile and delivers
   *  `objective` as its first assignment; the Manager then self-orients from the
   *  project CLAUDE.md and drives workers. Returns {ok, session_id, case_id, task_id}. */
  async invokeManager(
    token: string,
    args: {
      objective: string;
      repoPath: string;
      backend?: string;
      model?: string;
      completionCriteria?: string;
      nodeId?: string;
    },
    idempotencyKey: string,
  ): Promise<{ ok: boolean; reason?: string; session_id?: string; case_id?: string; task_id?: string }> {
    return post(
      `/api/manager`,
      token,
      {
        objective: args.objective,
        repo_path: args.repoPath,
        backend: args.backend ?? "claude",
        model: args.model ?? null,
        completion_criteria: args.completionCriteria ?? null,
        node_id: args.nodeId ?? null,
      },
      idempotencyKey,
    );
  },

  // ── parity endpoints (Telegram feature port) ─────────────────────────────

  /** GET /api/projects — discoverable repos for a node (local filesystem scan or DB). */
  async projects(token: string, nodeId = "__local__"): Promise<RawProject[]> {
    const data = await get<{ projects: RawProject[] }>(
      `/api/projects?node_id=${encodeURIComponent(nodeId)}`,
      token,
    );
    return data.projects ?? [];
  },

  /** GET /api/models — model catalog for a backend (config/models.py). */
  async models(token: string, backend: string): Promise<RawModelOption[]> {
    const data = await get<{ backend: string; models: RawModelOption[] }>(
      `/api/models?backend=${encodeURIComponent(backend)}`,
      token,
    );
    return data.models ?? [];
  },

  /** POST /api/sessions/{id}/upload — multipart file upload to session's uploads/ dir. */
  async uploadFile(token: string, sessionId: string, file: File): Promise<RawUploadResult> {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/upload`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    });
    const data = await res.json().catch(() => ({}) as Record<string, unknown>);
    if (!res.ok) {
      const reason =
        (data as { detail?: string }).detail ?? `${res.status} ${res.statusText}`;
      throw new ApiError(res.status, String(reason));
    }
    return data as RawUploadResult;
  },

  /** POST /api/sessions/{id}/compact — collapse Claude context window. */
  async compactSession(
    token: string,
    sessionId: string,
  ): Promise<{ ok: boolean; output: string; errors: string[] }> {
    return post(
      `/api/sessions/${encodeURIComponent(sessionId)}/compact`,
      token,
      {},
    );
  },

  /** POST /api/sessions/{id}/model — set model for a session. */
  async setModel(token: string, sessionId: string, model: string | null): Promise<CommandEnvelope> {
    return post<CommandEnvelope>(
      `/api/sessions/${encodeURIComponent(sessionId)}/model`,
      token,
      { model },
    );
  },

  /** POST /api/sessions/{id}/effort — set persistent thinking effort. */
  async setEffort(token: string, sessionId: string, effort: string | null): Promise<CommandEnvelope> {
    return post<CommandEnvelope>(
      `/api/sessions/${encodeURIComponent(sessionId)}/effort`,
      token,
      { effort },
      newIdempotencyKey(),
    );
  },

  /** POST /api/sessions/{id}/inspect — run a repo inspection op routed to the owning node. */
  async inspectSession(
    token: string,
    sessionId: string,
    op: string,
    params: Record<string, unknown> = {},
  ): Promise<unknown> {
    return post(
      `/api/sessions/${encodeURIComponent(sessionId)}/inspect`,
      token,
      { op, ...params },
    );
  },

  /** GET /api/jobs — watched jobs: running + recent. */
  async jobs(
    token: string,
    limit = 20,
    sessionId?: string,
    ownership?: "all" | "unowned",
  ): Promise<{ running: RawJob[]; recent: RawJob[] }> {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (sessionId) qs.set("session_id", sessionId);
    else if (ownership) qs.set("ownership", ownership);
    return get(`/api/jobs?${qs.toString()}`, token);
  },

  async meshHealth(token: string, limit = 24): Promise<RawMeshHealthResponse> {
    return get(`/api/mesh/health?limit=${limit}`, token);
  },

  // ── Work / Case read model (A27) — read-only ─────────────────────────────

  /** GET /api/work — case summaries + attention bucket tallies (newest first). */
  async work(
    token: string,
    opts: { bucket?: RawWorkBucket; limit?: number } = {},
  ): Promise<RawWorkListResponse> {
    const qs = new URLSearchParams({ limit: String(opts.limit ?? 50) });
    if (opts.bucket) qs.set("bucket", opts.bucket);
    return get<RawWorkListResponse>(`/api/work?${qs.toString()}`, token);
  },

  /** GET /api/work/{id} — one case: summary + ledger + parent/children. */
  async workDetail(token: string, flowRunId: string): Promise<RawCaseDetailResponse> {
    return get<RawCaseDetailResponse>(
      `/api/work/${encodeURIComponent(flowRunId)}`,
      token,
    );
  },

  /** GET /api/work/{id}/timeline — append-only audit events + evidence pointers. */
  async workTimeline(
    token: string,
    flowRunId: string,
    limit = 500,
  ): Promise<RawCaseTimelineResponse> {
    return get<RawCaseTimelineResponse>(
      `/api/work/${encodeURIComponent(flowRunId)}/timeline?limit=${limit}`,
      token,
    );
  },

  /** GET /api/work/{id}/graph — compact parent/self/children lineage graph. */
  async workGraph(token: string, flowRunId: string): Promise<RawCaseGraphResponse> {
    return get<RawCaseGraphResponse>(
      `/api/work/${encodeURIComponent(flowRunId)}/graph`,
      token,
    );
  },

  /**
   * GET /api/work/affiliations/sessions — the whole-substrate session→case
   * affiliation index (one authoritative JOIN; no per-case fanout, no cap). Used
   * by the Sessions surface to label a session's case role. A session absent from
   * the response is standalone (never inferred).
   */
  async workAffiliations(token: string): Promise<RawSessionAffiliationsResponse> {
    return get<RawSessionAffiliationsResponse>(
      "/api/work/affiliations/sessions",
      token,
    );
  },

  // ── web push (#21) ───────────────────────────────────────────────────────

  /** GET /api/push/status — is push available + the public VAPID key to subscribe. */
  async pushStatus(
    token: string,
  ): Promise<{ available: boolean; reason: string | null; vapid_public_key: string }> {
    return get(`/api/push/status`, token);
  },

  /** POST /api/push/subscribe — register a browser PushSubscription (idempotent). */
  async pushSubscribe(
    token: string,
    subscription: PushSubscriptionJSON,
    label?: string,
  ): Promise<{ ok: boolean }> {
    return post(`/api/push/subscribe`, token, {
      endpoint: subscription.endpoint,
      keys: subscription.keys,
      label: label ?? null,
    });
  },

  /** POST /api/push/unsubscribe — disable a subscription by endpoint. */
  async pushUnsubscribe(token: string, endpoint: string): Promise<{ ok: boolean }> {
    return post(`/api/push/unsubscribe`, token, { endpoint });
  },

  // ── backend account + usage (#30/#33) ────────────────────────────────────

  /** GET /api/backends/usage — honest per-backend account/usage facts. */
  async backendsUsage(token: string): Promise<BackendUsageResponse> {
    return get<BackendUsageResponse>(`/api/backends/usage`, token);
  },
};

export interface BackendUsageRow {
  backend: string;
  configured_model: string | null;
  observed_models: string[];
  recent_usage: Record<string, number> | null;
  recent_turn_count: number;
  /** How recent_usage was aggregated: "sum" (additive) or "peak" (cumulative
   *  counters, e.g. Codex context size — max observed, not a running total). */
  usage_aggregation?: string;
  account_identity: string | null;
  account_identity_reason: string | null;
  daily_limit: number | null;
  weekly_limit: number | null;
  limit_reset_at: string | null;
  limit_reason: string | null;
  usage_coverage: string;
}

export interface BackendUsageResponse {
  telemetry_available: boolean;
  backends: BackendUsageRow[];
  limits_source: string | null;
  limits_reason: string | null;
}
