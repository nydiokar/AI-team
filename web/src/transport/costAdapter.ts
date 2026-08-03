/**
 * Raw A65 cost read-model payloads → canonical ../domain/cost types.
 *
 * Pure field mapping + defaults for absent fields — never any computation that
 * could fabricate a number (the coverage/known USD comes straight from the
 * server, which already priced each model honestly).
 */
import type {
  RawTokenBuckets,
  RawCostUsd,
  RawDominantModel,
  RawCostSeries,
  RawCostExplorerResponse,
  RawCostTopResponse,
  RawCostProjectsResponse,
  RawCaseUsageResponse,
} from "./rawApi";
import type {
  TokenBuckets,
  CostUsd,
  DominantModel,
  CostSeries,
  CostBucket,
  CostExplorer,
  CostDimension,
  CostGranularity,
  CostTop,
  CostProjects,
  CaseUsage,
} from "../domain/cost";

export function toTokenBuckets(raw: RawTokenBuckets | null | undefined): TokenBuckets {
  return {
    input: raw?.input ?? 0,
    output: raw?.output ?? 0,
    cacheRead: raw?.cache_read ?? 0,
    cacheCreation: raw?.cache_creation ?? 0,
    total: raw?.total ?? 0,
  };
}

export function toCostUsd(raw: RawCostUsd | null | undefined): CostUsd {
  return {
    known: raw?.known ?? 0,
    unpricedTokens: raw?.unpriced_tokens ?? 0,
    coveragePct: raw?.coverage_pct ?? 100,
  };
}

function toBucket(tokens: RawTokenBuckets | undefined, usd: RawCostUsd | undefined): CostBucket {
  return { tokens: toTokenBuckets(tokens), usd: toCostUsd(usd) };
}

function toDominantModels(raw: RawDominantModel[] | null | undefined): DominantModel[] {
  return (raw ?? []).map((m) => ({
    model: m.model,
    usdTotal: m.usd_total ?? null,
    known: Boolean(m.known),
    reason: m.reason ?? null,
    tokensTotal: m.tokens_total ?? 0,
  }));
}

function toSeries(raw: RawCostSeries): CostSeries {
  return {
    ...toBucket(raw.tokens, raw.usd),
    bucket: raw.bucket ?? "",
    dim: raw.dim ?? "",
    models: toDominantModels(raw.models),
  };
}

const DIMENSIONS: CostDimension[] = [
  "project",
  "backend",
  "model",
  "role",
  "case",
  "session",
];

function normDimension(d: string | undefined): CostDimension {
  return d && (DIMENSIONS as string[]).includes(d) ? (d as CostDimension) : "project";
}

export function toCostExplorer(raw: RawCostExplorerResponse): CostExplorer {
  return {
    dimension: normDimension(raw.dimension),
    granularity: raw.granularity === "none" ? "none" : "day",
    from: raw.from ?? null,
    to: raw.to ?? null,
    repoPath: raw.repo_path ?? null,
    series: (raw.series ?? []).map(toSeries),
    totals: toBucket(raw.totals?.tokens, raw.totals?.usd),
    unattributed: {
      ...toBucket(raw.unattributed?.tokens, raw.unattributed?.usd),
      models: toDominantModels(raw.unattributed?.models),
    },
  };
}

export function toCostTop(raw: RawCostTopResponse): CostTop {
  return {
    by: raw.by === "tokens" ? "tokens" : "usd",
    rows: (raw.rows ?? []).map((r) => ({
      ...toBucket(r.tokens, r.usd),
      sessionId: r.session_id ?? "",
      repoPath: r.repo_path ?? null,
      backend: r.backend ?? null,
      role: r.role ?? null,
      models: toDominantModels(r.models),
    })),
    totals: toBucket(raw.totals?.tokens, raw.totals?.usd),
  };
}

export function toCostProjects(raw: RawCostProjectsResponse): CostProjects {
  return {
    projects: (raw.projects ?? []).map((p) => ({
      repoPath: p.repo_path,
      tokens: toTokenBuckets(p.tokens),
    })),
  };
}

export function toCaseUsage(raw: RawCaseUsageResponse): CaseUsage {
  return {
    flowRunId: raw.flow_run_id ?? "",
    case: {
      status: raw.case?.status ?? null,
      objectiveLock: raw.case?.objective_lock ?? null,
      createdAt: raw.case?.created_at ?? null,
    },
    sessions: (raw.sessions ?? []).map((s) => ({
      ...toBucket(s.tokens, s.usd),
      sessionId: s.session_id ?? "",
      role: s.role ?? "member",
      models: toDominantModels(s.models),
    })),
    mgrVsWorkers: {
      manager: toBucket(raw.mgr_vs_workers?.manager?.tokens, raw.mgr_vs_workers?.manager?.usd),
      workers: toBucket(raw.mgr_vs_workers?.workers?.tokens, raw.mgr_vs_workers?.workers?.usd),
      workersSharePct: raw.mgr_vs_workers?.workers_share_pct ?? null,
      workerSessions: raw.mgr_vs_workers?.worker_sessions ?? 0,
    },
    totals: toBucket(raw.totals?.tokens, raw.totals?.usd),
  };
}

// Kept out of the default export so tree-shaking can drop unused translations.
export type { CostGranularity };
