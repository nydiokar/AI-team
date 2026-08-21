/**
 * Server-state hook for the liveness-outage log (~/scripts/aiteam-healthcheck.sh
 * writes it; the gateway only reads it). Read-only, gentle poll — this is a
 * banner, not a live heartbeat.
 */
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../transport/apiClient";
import { toSystemAlerts } from "../transport/systemAlertsAdapter";
import { useAuthStore } from "../stores/authStore";

const POLL_MS = 15000;

const retry = (count: number, err: unknown) =>
  !(err instanceof ApiError && [401, 500].includes(err.status)) && count < 3;

export function useSystemAlerts() {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["system-alerts"],
    queryFn: async () => toSystemAlerts(await api.systemAlerts(token)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}
