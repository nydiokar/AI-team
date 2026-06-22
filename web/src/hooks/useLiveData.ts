/**
 * Server-state hooks (TanStack Query) for the LIVE read API. Sessions + System
 * bind to these (UI-1 acceptance gate). Tasks/timeline use fixtures in UI-1.
 *
 * Polling (3s, matching the dashboard) is the transport — there is no WS/SSE
 * until Move F (gap-doc §7). Raw payloads are translated through the adapters
 * here so components only ever see canonical ../domain types (spec §11.1).
 */
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../transport/apiClient";
import { toSessions } from "../transport/sessionAdapter";
import { toTargets } from "../transport/nodeAdapter";
import { toTasks } from "../transport/taskAdapter";
import { useAuthStore } from "../stores/authStore";

const POLL_MS = 3000;

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
    refetchInterval: POLL_MS,
    retry: (count, err) =>
      !(err instanceof ApiError && [401, 500].includes(err.status)) && count < 3,
  });
}

/**
 * Live tasks — available but NOT required for the UI-1 gate (Tasks screen renders
 * from fixtures per scope). Exposed so the Tasks screen can opt into live data
 * where the flat /api/tasks rows suffice; richer sectioning waits for Move G′.
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
