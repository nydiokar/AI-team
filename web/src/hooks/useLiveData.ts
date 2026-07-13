/**
 * Server-state hooks (TanStack Query) for the LIVE read API. Sessions + System
 * bind to these.
 *
 * Polling (3s, matching the dashboard) is the transport — there is no WS/SSE
 * until Move F (gap-doc §7). Raw payloads are translated through the adapters
 * here so components only ever see canonical ../domain types (spec §11.1).
 */
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../transport/apiClient";
import { toSessions } from "../transport/sessionAdapter";
import { toTargets } from "../transport/nodeAdapter";
import { toTasks, toTaskSections } from "../transport/taskAdapter";
import { toApprovals } from "../transport/approvalAdapter";
import { toArtifacts, toArtifactDetail } from "../transport/artifactAdapter";
import { toSessionActivityTimeline } from "../transport/sessionTimelineAdapter";
import { useAuthStore } from "../stores/authStore";

const POLL_MS = 3000;
// Slow tier for infra status that changes far slower than it's polled: node
// liveness is heartbeat-derived on a ~90s timeout, and mesh-health is a trend
// sample series. Polling these every 3s just keeps the mobile radio warm for no
// fresher data — 20s is still multiples finer than the underlying signal.
const SLOW_POLL_MS = 20000;

export function useSessions() {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["sessions"],
    queryFn: async () => toSessions(await api.sessions(token)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    retry: (count, err) =>
      // Don't hammer on auth failures — a bad token won't fix itself.
      !(err instanceof ApiError && [401, 500].includes(err.status)) && count < 3,
  });
}

export function useTargets() {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["nodes"],
    queryFn: async () => toTargets(await api.nodes(token)),
    enabled: Boolean(token),
    refetchInterval: SLOW_POLL_MS,
    retry: (count, err) =>
      !(err instanceof ApiError && [401, 500].includes(err.status)) && count < 3,
  });
}

/**
 * Live tasks. Kept for command invalidation and legacy consumers; primary
 * session progress now comes from the durable session timeline.
 */
export function useTasks(limit = 50) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["tasks", limit],
    queryFn: async () => toTasks(await api.tasks(token, limit)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
  });
}

/**
 * Sectioned task buckets bound to the backend's supervised lifecycle
 * (attention/running/queued/recent), not client-side bucketing.
 * The backend overlays each task's owning-session status, so `waiting_for_input`
 * lands in `attention` here where the flat status alone couldn't reach it.
 */
export function useTaskSections(limit = 50) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["task-sections", limit],
    queryFn: async () => toTaskSections(await api.taskSections(token, limit)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
  });
}

/**
 * Pending approval queue (Move H / UI-3). Polls /api/approvals; the queue is
 * what rebuilds the UI after a gateway restart (the pending rows are durable).
 */
export function useApprovals(status = "pending") {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["approvals", status],
    queryFn: async () => toApprovals(await api.approvals(token, status)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
  });
}

/**
 * Artifacts list (UI-4) — "what did the agent change?". Newest-first headers from
 * /api/artifacts (the on-disk results/<task>.json files). Per-file detail is a
 * separate per-artifact fetch (useArtifact) so the list stays cheap.
 */
export function useArtifacts(limit = 50) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["artifacts", limit],
    queryFn: async () => toArtifacts(await api.artifacts(token, limit)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
  });
}

/**
 * The session's real conversation (instruction → result turns), reconstructed
 * server-side from on-disk artifacts. This is what makes a Telegram-started
 * session show its actual messages instead of "No activity yet". Polls so a turn
 * that completes while you watch appears.
 */
export function useSessionMessages(sessionId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["session-messages", sessionId],
    queryFn: async () => api.sessionMessages(token, sessionId!),
    enabled: Boolean(token) && Boolean(sessionId),
    refetchInterval: POLL_MS,
    // The poll is the freshness guarantee, but a persisted cache can paint old
    // turns on a cold/offline open — refetch the moment the network returns so
    // we never sit on stale data longer than necessary.
    refetchOnReconnect: true,
    // Keep previous data visible during refetch so the chat doesn't flash to a
    // loading spinner every 3 s poll cycle.
    placeholderData: (prev) => prev,
  });
}

/**
 * LLM turn observability for a session (Feature #37) — one row per agent turn
 * from the llm_turns telemetry projection, newest-first. Drives the Session
 * "info" tab's turn list and the context-usage display (Feature #35). Polls so a
 * turn that completes while you watch updates its metrics; keeps previous data to
 * avoid a spinner flash on each cycle. Returns [] when telemetry is unavailable.
 */
export function useSessionTurns(sessionId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["session-turns", sessionId],
    queryFn: async () => api.turns(token, sessionId!),
    enabled: Boolean(token) && Boolean(sessionId),
    refetchInterval: POLL_MS,
    refetchOnReconnect: true,
    placeholderData: (prev) => prev,
  });
}

/**
 * One artifact's changed files (UI-4) — fetched on demand when a card expands.
 * Artifacts are immutable once written, so this does NOT poll.
 */
/**
 * Durable session execution timeline. This is the session-owned read model for
 * task/job/turn/approval facts and must not be mixed with rolling live events as
 * state authority.
 */
export function useSessionActivity(
  sessionId: string | undefined,
  limit = 50,
  cursor?: string | null,
) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["session-activity", sessionId, limit, cursor ?? null],
    queryFn: async () =>
      toSessionActivityTimeline(
        await api.sessionTimeline(token, sessionId!, limit, cursor),
      ),
    enabled: Boolean(token) && Boolean(sessionId),
    refetchInterval: POLL_MS,
    refetchOnReconnect: true,
    placeholderData: (prev) => prev,
  });
}

/** Fetch one immutable artifact detail on demand. */
export function useArtifact(taskId: string | null) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["artifact", taskId],
    queryFn: async () => toArtifactDetail(await api.artifact(token, taskId!)),
    enabled: Boolean(token) && Boolean(taskId),
  });
}

/**
 * Discoverable repos for a node — used by the repo picker in NewSessionSheet.
 * Stale for 30 s (repo list doesn't change often). Does NOT poll.
 */
export function useProjects(nodeId = "__local__") {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["projects", nodeId],
    queryFn: () => api.projects(token, nodeId),
    enabled: Boolean(token),
    staleTime: 30_000,
    retry: (count, err) =>
      !(err instanceof ApiError && [401, 500].includes(err.status)) && count < 2,
  });
}

/**
 * Model catalog for a backend — drives the web model picker (parity with /model).
 * Static server-side catalog; stale forever (never changes at runtime).
 */
export function useModels(backend: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["models", backend],
    queryFn: () => api.models(token, backend!),
    enabled: Boolean(token) && Boolean(backend),
    staleTime: Infinity,
  });
}

/**
 * Watched jobs — running + recently finished. Polls at the same rate as tasks.
 */
export function useJobs(
  limit = 20,
  sessionId?: string,
  ownership?: "all" | "unowned",
) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["jobs", limit, sessionId ?? null, ownership ?? "all"],
    queryFn: () => api.jobs(token, limit, sessionId, ownership),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
  });
}

export function useMeshHealth(limit = 24) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["mesh-health", limit],
    queryFn: () => api.meshHealth(token, limit),
    enabled: Boolean(token),
    refetchInterval: SLOW_POLL_MS,
  });
}
