/**
 * Thin fetch client for the read-only dashboard API (src/control/dashboard.py).
 *
 * Auth: Bearer DASHBOARD_TOKEN on every /api/* call (dashboard._require_auth).
 * The token is supplied by the operator and held in the auth store; this module
 * stays stateless and takes it per-call.
 *
 * READ-ONLY by design (UI-0/UI-1 scope) — there are no write methods here.
 * send/stop/retry/approve arrive with backend Move F (UI-2), not in this client.
 */
import type {
  RawSessionView,
  RawNode,
  RawTask,
  RawEventsResponse,
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

  /** Live event tail (poll). Pass the returned offset back as `since`. */
  async events(token: string, since = 0, limit = 100): Promise<RawEventsResponse> {
    return get<RawEventsResponse>(
      `/api/events?since=${since}&limit=${limit}`,
      token,
    );
  },

  /** Unauthenticated health probe (dashboard /health). */
  async health(): Promise<boolean> {
    try {
      const res = await fetch(`/health`);
      return res.ok;
    } catch {
      return false;
    }
  },
};
