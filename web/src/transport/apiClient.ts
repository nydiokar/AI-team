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
    const reason =
      (data as { reason?: string; detail?: { reason?: string } | string }).reason ??
      (typeof (data as { detail?: unknown }).detail === "object"
        ? ((data as { detail?: { reason?: string } }).detail?.reason ?? "")
        : (data as { detail?: string }).detail) ??
      `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, String(reason));
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
    limit = 50,
  ): Promise<RawTranscriptTurn[]> {
    const data = await get<{ messages: RawTranscriptTurn[] }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`,
      token,
    );
    return data.messages ?? [];
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

  /** Create a web-origin session (control_api.api_create_session). */
  async createSession(
    token: string,
    args: { backend: string; repoPath: string; model?: string; nodeId?: string },
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
  async jobs(token: string, limit = 20): Promise<{ running: RawJob[]; recent: RawJob[] }> {
    return get(`/api/jobs?limit=${limit}`, token);
  },
};
