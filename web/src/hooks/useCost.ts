/**
 * Server-state hooks for the A65 cost read-model (TanStack Query, token-gated,
 * same retry policy as useLiveData).
 *
 * Cost is a spend view, not a live heartbeat — it changes as turns complete, on
 * the scale of seconds-to-minutes, so it polls on a gentle 15 s tier rather than
 * the 3 s live tier (matching the Work detail tier). Every query is READ-ONLY.
 */
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../transport/apiClient";
import {
  toCostExplorer,
  toCostTop,
  toCostProjects,
  toCaseUsage,
  toCostAlerts,
} from "../transport/costAdapter";
import type { CostDimension, CostGranularity } from "../domain/cost";
import { useAuthStore } from "../stores/authStore";

const POLL_MS = 15000;

const retry = (count: number, err: unknown) =>
  !(err instanceof ApiError && [401, 500].includes(err.status)) && count < 3;

export interface CostWindow {
  from?: string;
  to?: string;
  repoPath?: string;
}

export function useCostExplorer(
  opts: {
    dimension?: CostDimension;
    granularity?: CostGranularity;
    from?: string;
    to?: string;
    repoPath?: string;
    limit?: number;
  } = {},
) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: [
      "cost-explorer",
      opts.dimension ?? "project",
      opts.granularity ?? "day",
      opts.from ?? null,
      opts.to ?? null,
      opts.repoPath ?? null,
      opts.limit ?? 100,
    ],
    queryFn: async () =>
      toCostExplorer(
        await api.costExplorer(token, {
          dimension: opts.dimension,
          granularity: opts.granularity,
          from: opts.from,
          to: opts.to,
          repoPath: opts.repoPath,
          limit: opts.limit,
        }),
      ),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}

export function useCostTop(
  opts: {
    by?: "usd" | "tokens";
    from?: string;
    to?: string;
    repoPath?: string;
    limit?: number;
  } = {},
) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: [
      "cost-top",
      opts.by ?? "usd",
      opts.from ?? null,
      opts.to ?? null,
      opts.repoPath ?? null,
      opts.limit ?? 10,
    ],
    queryFn: async () =>
      toCostTop(
        await api.costTop(token, {
          by: opts.by,
          from: opts.from,
          to: opts.to,
          repoPath: opts.repoPath,
          limit: opts.limit,
        }),
      ),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}

export function useCostProjects(opts: { from?: string; to?: string } = {}) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["cost-projects", opts.from ?? null, opts.to ?? null],
    queryFn: async () =>
      toCostProjects(await api.costProjects(token, opts)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}

export function useCaseUsage(flowRunId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["case-usage", flowRunId],
    queryFn: async () => toCaseUsage(await api.caseUsage(token, flowRunId!)),
    enabled: Boolean(token) && Boolean(flowRunId),
    refetchInterval: POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}

export function useCostAlerts() {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["cost-alerts"],
    queryFn: async () => toCostAlerts(await api.costAlerts(token)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}
