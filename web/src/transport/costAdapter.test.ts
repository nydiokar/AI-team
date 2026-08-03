/**
 * costAdapter tests — raw A65 cost payloads → canonical domain types. Pure
 * mapping: coverage/known USD come straight from the server and must pass
 * through unchanged; absent fields fall back to honest defaults.
 */
import { describe, it, expect } from "vitest";
import {
  toTokenBuckets,
  toCostUsd,
  toCostExplorer,
  toCostTop,
  toCostProjects,
  toCaseUsage,
  toCostAlerts,
} from "./costAdapter";
import type {
  RawCostExplorerResponse,
  RawCostTopResponse,
  RawCostProjectsResponse,
  RawCaseUsageResponse,
  RawCostAlertsResponse,
} from "./rawApi";

const T = (total = 100, extra: Partial<Record<string, number>> = {}) => ({
  input: 30,
  output: 10,
  cache_read: 50,
  cache_creation: 10,
  total,
  ...extra,
});

const USD = (known: number, unpriced_tokens = 0, coverage_pct = 100) => ({
  known,
  unpriced_tokens,
  coverage_pct,
});

describe("toTokenBuckets / toCostUsd — defaults for absent fields", () => {
  it("maps the four buckets + total", () => {
    expect(toTokenBuckets(T(100))).toEqual({
      input: 30,
      output: 10,
      cacheRead: 50,
      cacheCreation: 10,
      total: 100,
    });
  });

  it("is zero when the payload omits a bucket", () => {
    expect(toTokenBuckets(null)).toEqual({
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheCreation: 0,
      total: 0,
    });
    expect(toTokenBuckets(undefined)).toEqual(
      toTokenBuckets(null),
    );
  });

  it("passes the honest coverage numbers through unchanged", () => {
    expect(toCostUsd(USD(22.47, 44_000_000, 49))).toEqual({
      known: 22.47,
      unpricedTokens: 44_000_000,
      coveragePct: 49,
    });
    expect(toCostUsd(undefined)).toEqual({ known: 0, unpricedTokens: 0, coveragePct: 100 });
  });
});

describe("toCostExplorer", () => {
  const base = (over: Partial<RawCostExplorerResponse> = {}): RawCostExplorerResponse => ({
    ok: true,
    dimension: "project",
    granularity: "day",
    from: null,
    to: null,
    repo_path: null,
    series: [
      {
        bucket: "2026-08-02",
        dim: "AI-team",
        models: [
          {
            model: "gpt-5.6-terra",
            usd_total: 12.3,
            known: true,
            reason: null,
            tokens_total: 4_000_000,
          },
          {
            model: "<unknown>",
            usd_total: null,
            known: false,
            reason: "no price",
            tokens_total: 500_000,
          },
        ],
        tokens: T(4_500_000),
        usd: USD(12.3, 500_000, 88.9),
      },
    ],
    totals: { tokens: T(4_500_000), usd: USD(12.3, 500_000, 88.9) },
    unattributed: {
      tokens: T(11_060_000),
      usd: USD(0, 11_060_000, 0),
      models: [],
    },
    ...over,
  });

  it("maps series, totals and the unattributed bucket", () => {
    const out = toCostExplorer(base());
    expect(out.dimension).toBe("project");
    expect(out.granularity).toBe("day");
    expect(out.series).toHaveLength(1);
    expect(out.series[0]).toMatchObject({
      bucket: "2026-08-02",
      dim: "AI-team",
      tokens: { total: 4_500_000 },
      usd: { known: 12.3, coveragePct: 88.9 },
    });
    expect(out.series[0].models[1]).toEqual({
      model: "<unknown>",
      usdTotal: null,
      known: false,
      reason: "no price",
      tokensTotal: 500_000,
    });
    expect(out.unattributed.tokens.total).toBe(11_060_000);
    expect(out.unattributed.usd.known).toBe(0);
  });

  it("normalizes granularity and unknown dimensions without inventing data", () => {
    const out = toCostExplorer(base({ dimension: "bogus", granularity: "hour" }));
    expect(out.dimension).toBe("project");
    expect(out.granularity).toBe("day");
  });

  it("normalizes granularity=none", () => {
    const out = toCostExplorer(base({ granularity: "none" }));
    expect(out.granularity).toBe("none");
  });
});

describe("toCostTop", () => {
  const raw: RawCostTopResponse = {
    ok: true,
    by: "usd",
    limit: 1,
    rows: [
      {
        session_id: "3549863198",
        repo_path: "/home/cifran/dev/AI-team",
        backend: "codex",
        role: "manager",
        models: [{ model: "gpt-5.6-terra", usd_total: 112.35, known: true, reason: null, tokens_total: 446_900_000 }],
        tokens: T(446_900_000),
        usd: USD(112.35, 0, 100),
      },
    ],
    totals: { tokens: T(446_900_000), usd: USD(112.35, 0, 100) },
  };

  it("maps a top-spender row with its session identity + honest coverage", () => {
    const out = toCostTop(raw);
    expect(out.by).toBe("usd");
    const row = out.rows[0];
    expect(row.sessionId).toBe("3549863198");
    expect(row.repoPath).toBe("/home/cifran/dev/AI-team");
    expect(row.backend).toBe("codex");
    expect(row.role).toBe("manager");
    expect(row.usd.known).toBe(112.35);
    expect(row.usd.coveragePct).toBe(100);
  });

  it("normalizes by=tokens and nulls absent identity fields", () => {
    const out = toCostTop({ ...raw, by: "tokens", rows: [{ ...raw.rows[0], backend: null, repo_path: null, role: null }] });
    expect(out.by).toBe("tokens");
    expect(out.rows[0].backend).toBeNull();
    expect(out.rows[0].repoPath).toBeNull();
    expect(out.rows[0].role).toBeNull();
  });
});

describe("toCostProjects", () => {
  it("maps the project dropdown list (token weight, no fabricated USD)", () => {
    const raw: RawCostProjectsResponse = {
      ok: true,
      limit: 2,
      projects: [
        { repo_path: "/home/cifran/dev/AI-team", tokens: T(935_400_000) },
        { repo_path: "/srv/other", tokens: T(12_000) },
      ],
    };
    const out = toCostProjects(raw);
    expect(out.projects).toHaveLength(2);
    expect(out.projects[0]).toEqual({
      repoPath: "/home/cifran/dev/AI-team",
      tokens: { input: 30, output: 10, cacheRead: 50, cacheCreation: 10, total: 935_400_000 },
    });
  });

  it("is empty for an empty payload", () => {
    expect(toCostProjects({ ok: true, limit: 0, projects: [] }).projects).toEqual([]);
  });
});

describe("toCaseUsage", () => {
  const raw: RawCaseUsageResponse = {
    ok: true,
    flow_run_id: "flow_a1b2c3d4",
    case: {
      status: "active",
      objective_lock: "<task_name>A65</task_name>",
      created_at: "2026-08-01T00:00:00Z",
    },
    sessions: [
      {
        session_id: "mgr1",
        role: "manager",
        models: [{ model: "gpt-5.6", usd_total: 8.0, known: true, reason: null, tokens_total: 1_000_000 }],
        tokens: T(1_000_000),
        usd: USD(8.0),
      },
      {
        session_id: "wk1",
        role: "worker",
        models: [{ model: "gpt-5.5", usd_total: 3.0, known: true, reason: null, tokens_total: 500_000 }],
        tokens: T(500_000),
        usd: USD(3.0),
      },
    ],
    mgr_vs_workers: {
      manager: { tokens: T(1_000_000), usd: USD(8.0) },
      workers: { tokens: T(500_000), usd: USD(3.0) },
      workers_share_pct: 27.3,
      worker_sessions: 1,
    },
    totals: { tokens: T(1_500_000), usd: USD(11.0) },
  };

  it("maps the manager-vs-workers split the operator asked to see", () => {
    const out = toCaseUsage(raw);
    expect(out.flowRunId).toBe("flow_a1b2c3d4");
    expect(out.mgrVsWorkers.manager.usd.known).toBe(8.0);
    expect(out.mgrVsWorkers.workers.usd.known).toBe(3.0);
    expect(out.mgrVsWorkers.workersSharePct).toBe(27.3);
    expect(out.mgrVsWorkers.workerSessions).toBe(1);
    expect(out.sessions.map((s) => s.role)).toEqual(["manager", "worker"]);
  });

  it("defaults an absent session role to member (never infers)", () => {
    const out = toCaseUsage({
      ...raw,
      sessions: [{ ...raw.sessions[0], role: undefined as unknown as string }],
    });
    expect(out.sessions[0].role).toBe("member");
  });

  it("defaults missing mgr_vs_workers to zeros, not guesses", () => {
    const out = toCaseUsage({
      ...raw,
      mgr_vs_workers: {
        manager: { tokens: T(1_000_000), usd: USD(8.0) },
        workers: { tokens: T(500_000), usd: USD(3.0) },
        workers_share_pct: null,
        worker_sessions: 0,
      },
    });
    expect(out.mgrVsWorkers.workersSharePct).toBeNull();
    expect(out.mgrVsWorkers.workerSessions).toBe(0);
  });
});

describe("toCostAlerts — P3 budget/burn-rate alerts", () => {
  const raw: RawCostAlertsResponse = {
    ok: true,
    enabled: true,
    budgets: { daily_budget_usd: 100, session_burn_usd: 0, case_total_usd: 0 },
    alerts: [
      { rule: "daily_budget", scope: "today (UTC)", value_usd: 2244.3, budget_usd: 100, pct: 2244.3 },
      { rule: "session_burn", scope: "sess-1", value_usd: 73.6, budget_usd: 50, pct: 147.2 },
    ],
    enforcement: {
      enabled: false,
      mechanism: "sdk_max_budget_usd",
      governor_sdk_max_budget_usd: null,
    },
  };

  it("maps fired alerts, budgets and the enforcement surface honestly", () => {
    const out = toCostAlerts(raw);
    expect(out.enabled).toBe(true);
    expect(out.budgets).toEqual({ dailyBudgetUsd: 100, sessionBurnUsd: 0, caseTotalUsd: 0 });
    expect(out.alerts[0]).toEqual({
      rule: "daily_budget",
      scope: "today (UTC)",
      valueUsd: 2244.3,
      budgetUsd: 100,
      pct: 2244.3,
    });
    expect(out.alerts[1].rule).toBe("session_burn");
    expect(out.enforcement.enabled).toBe(false);
    expect(out.enforcement.mechanism).toBe("sdk_max_budget_usd");
    expect(out.enforcement.governorSdkMaxBudgetUsd).toBeNull();
  });

  it("normalizes an unknown rule to daily_budget (never invents)", () => {
    const out = toCostAlerts({ ...raw, alerts: [{ ...raw.alerts[0], rule: "bogus" }] });
    expect(out.alerts[0].rule).toBe("daily_budget");
  });

  it("defaults absent fields instead of guessing", () => {
    const out = toCostAlerts({
      ok: true,
      enabled: false,
      budgets: undefined as never,
      alerts: [],
      enforcement: undefined as never,
    });
    expect(out.enabled).toBe(false);
    expect(out.budgets).toEqual({ dailyBudgetUsd: 0, sessionBurnUsd: 0, caseTotalUsd: 0 });
    expect(out.alerts).toEqual([]);
    expect(out.enforcement.enabled).toBe(false);
    expect(out.enforcement.governorSdkMaxBudgetUsd).toBeNull();
  });
});
