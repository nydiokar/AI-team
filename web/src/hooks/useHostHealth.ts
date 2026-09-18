/**
 * Server-state hook for the gateway's host/app health verdict. The verdict only changes
 * once per minute (the metrics rollup cadence), so a 60 s poll is exact, not lazy.
 * react-query pauses it while the tab is hidden.
 */
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../transport/apiClient";
import { useAuthStore } from "../stores/authStore";

const POLL_MS = 60000;

const retry = (count: number, err: unknown) =>
  !(err instanceof ApiError && [401, 404, 500].includes(err.status)) && count < 2;

export function useHostHealth() {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["host-health"],
    queryFn: () => api.hostHealth(token),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}
