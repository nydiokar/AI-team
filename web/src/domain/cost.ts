/**
 * Cost read-model canonical types (A65) — the ONLY shape components consume.
 *
 * Translated from the raw Control API payloads by ../transport/costAdapter
 * (spec §11.1: raw snake_case never leaks into components). Every USD number
 * is the honest `known` figure; the same payload carries `coveragePct` and
 * `unpricedTokens` so a partial price is visible, never silent.
 */

export type CostDimension =
  | "project"
  | "backend"
  | "model"
  | "role"
  | "case"
  | "session";

export type CostGranularity = "day" | "none";

/** The four token buckets + grand total (all four summed, matching pricing). */
export interface TokenBuckets {
  input: number;
  output: number;
  cacheRead: number;
  cacheCreation: number;
  total: number;
}

/** Honest USD rollup: only priced models contribute to `known`; the rest are
 *  surfaced as unpriced tokens with an explicit coverage %. */
export interface CostUsd {
  known: number;
  unpricedTokens: number;
  coveragePct: number;
}

/** One model's contribution inside a bucket — which model drove the spend. */
export interface DominantModel {
  model: string;
  usdTotal: number | null;
  known: boolean;
  reason: string | null;
  tokensTotal: number;
}

export interface CostBucket {
  tokens: TokenBuckets;
  usd: CostUsd;
}

/** One (time-bucket × dimension) explorer row. */
export interface CostSeries extends CostBucket {
  bucket: string;
  dim: string;
  models: DominantModel[];
}

/** Whole-window totals + the honest unattributed bucket (no-session turns). */
export interface CostExplorer {
  dimension: CostDimension;
  granularity: CostGranularity;
  from: string | null;
  to: string | null;
  repoPath: string | null;
  series: CostSeries[];
  totals: CostBucket;
  unattributed: CostBucket & { models: DominantModel[] };
}

/** One top-spender session (ranked by USD or tokens). */
export interface TopSession extends CostBucket {
  sessionId: string;
  repoPath: string | null;
  backend: string | null;
  role: string | null;
  models: DominantModel[];
}

export interface CostTop {
  by: "usd" | "tokens";
  rows: TopSession[];
  totals: CostBucket;
}

/** Project-filter dropdown fuel: list + per-project token rollup. */
export interface CostProject {
  repoPath: string;
  tokens: TokenBuckets;
}

export interface CostProjects {
  projects: CostProject[];
}

/** One session inside a case's usage breakdown. */
export interface CaseUsageSession extends CostBucket {
  sessionId: string;
  role: string;
  models: DominantModel[];
}

/** Per-case cost (the manager/workers split the operator asked to see). */
export interface CaseUsage {
  flowRunId: string;
  case: {
    status: string | null;
    objectiveLock: string | null;
    createdAt: string | null;
  };
  sessions: CaseUsageSession[];
  mgrVsWorkers: {
    manager: CostBucket;
    workers: CostBucket;
    workersSharePct: number | null;
    workerSessions: number;
  };
  totals: CostBucket;
}

// ── Budget / burn-rate alerts (P3) ──────────────────────────────────────────
export type CostAlertRule = "daily_budget" | "session_burn" | "case_total";

/** One fired alert: the read-model's known USD crossed a configured budget. */
export interface CostAlert {
  rule: CostAlertRule;
  scope: string;
  valueUsd: number;
  budgetUsd: number;
  pct: number;
}

export interface CostBudgetKnobs {
  dailyBudgetUsd: number;
  sessionBurnUsd: number;
  caseTotalUsd: number;
}

/** Enforcement never adds a new kill mechanism — it surfaces the existing SDK
 *  governor ceiling and stays flag-gated OFF by default. */
export interface CostEnforcement {
  enabled: boolean;
  mechanism: string;
  governorSdkMaxBudgetUsd: number | null;
}

export interface CostAlerts {
  enabled: boolean;
  budgets: CostBudgetKnobs;
  alerts: CostAlert[];
  enforcement: CostEnforcement;
}
