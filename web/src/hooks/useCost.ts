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
import { rangeToFromTo, type CostRangeKey } from "../lib/costPresentation";

const POLL_MS = 15000;

const retry = (count: number, err: unknown) =>
  !(err instanceof ApiError && [401, 500].includes(err.status)) && count < 3;

export interface CostWindow {
  from?: string;
  to?: string;
  repoPath?: string;
  range?: CostRangeKey;
}

function rollingWindow(opts: CostWindow): CostWindow {
  return opts.range ? { ...opts, ...rangeToFromTo(opts.range) } : opts;
}

export function useCostExplorer(
  opts: {
    dimension?: CostDimension;
    granularity?: CostGranularity;
    from?: string;
    to?: string;
    repoPath?: string;
    limit?: number;
    range?: CostRangeKey;
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
      opts.range ?? null,
    ],
    queryFn: async ({ signal }) =>
      toCostExplorer(
        await api.costExplorer(token, {
          dimension: opts.dimension,
          granularity: opts.granularity,
          ...rollingWindow(opts),
          repoPath: opts.repoPath,
          limit: opts.limit,
        }, signal),
      ),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
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
    range?: CostRangeKey;
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
      opts.range ?? null,
    ],
    queryFn: async ({ signal }) =>
      toCostTop(
        await api.costTop(token, {
          by: opts.by,
          ...rollingWindow(opts),
          repoPath: opts.repoPath,
          limit: opts.limit,
        }, signal),
      ),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    retry,
  });
}

export function useCostProjects(opts: { from?: string; to?: string; range?: CostRangeKey } = {}) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["cost-projects", opts.from ?? null, opts.to ?? null, opts.range ?? null],
    queryFn: async ({ signal }) =>
      toCostProjects(await api.costProjects(token, rollingWindow(opts), signal)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    retry,
  });
}

export function useCaseUsage(flowRunId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["case-usage", flowRunId],
    queryFn: async ({ signal }) => toCaseUsage(await api.caseUsage(token, flowRunId!, signal)),
    enabled: Boolean(token) && Boolean(flowRunId),
    refetchInterval: POLL_MS,
    retry,
  });
}

export function useCostAlerts() {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["cost-alerts"],
    queryFn: async ({ signal }) => toCostAlerts(await api.costAlerts(token, signal)),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}
