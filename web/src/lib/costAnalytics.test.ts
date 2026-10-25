import { describe, expect, it } from "vitest";
import type { CostSeries } from "../domain/cost";
import { aggregateModels, dailyUsage, dailyUsageCsv, tokenComposition, isFilterableProject, formatCostUsd } from "./costAnalytics";

function row(bucket: string, dim: string, usd: number, unpriced = 0): CostSeries {
  return {
    bucket, dim, models: [],
    tokens: { input: 70, output: 30, cacheRead: 0, cacheCreation: 0, total: 100 },
    usd: { known: usd, unpricedTokens: unpriced, coveragePct: 100 - unpriced },
  };
}

describe("daily usage", () => {
  it("sums models, fills UTC days, preserves partial boundaries and excludes out-of-window buckets", () => {
    const days = dailyUsage([
      row("2026-10-24", "a", 2), row("2026-10-24", "b", 3, 100),
      row("2026-10-26", "a", 1), row("2026-10-20", "a", 20),
    ], "2026-10-24T12:00:00Z", "2026-10-26T12:00:00Z");
    expect(days.map((d) => d.usd.known)).toEqual([5, 0, 1]);
    expect(days.map((d) => d.partial)).toEqual([true, false, true]);
    expect(days[0].tokens.total).toBe(200);
    expect(days[0].usd.coveragePct).toBe(50);
  });

  it("keeps a single UTC day and handles empty usage", () => {
    const days = dailyUsage([], "2026-10-24T00:00:00Z", "2026-10-24T12:00:00Z");
    expect(days).toHaveLength(1);
    expect(days[0].usd.known).toBe(0);
    expect(days[0].partial).toBe(true);
  });
});

it("aggregates models without losing zero-USD unpriced usage or mutating source rows", () => {
  const rows = [row("2026-10-24", "known", 3), row("2026-10-25", "known", 2), row("2026-10-25", "unknown", 0, 100)];
  const before = JSON.stringify(rows);
  const models = aggregateModels(rows);
  expect(models.map((m) => [m.dim, m.usd.known, m.tokens.total])).toEqual([["known", 5, 200], ["unknown", 0, 100]]);
  expect(models[1].usd.coveragePct).toBe(0);
  expect(JSON.stringify(rows)).toBe(before);
});

it("uses all four token buckets for composition and returns no NaN for zero", () => {
  expect(tokenComposition({ input: 10, output: 20, cacheRead: 60, cacheCreation: 10, total: 100 }).map((s) => s.pct)).toEqual([10, 20, 60, 10]);
  expect(tokenComposition({ input: 0, output: 0, cacheRead: 0, cacheCreation: 0, total: 0 }).every((s) => s.pct === 0)).toBe(true);
});

it("does not offer the unfilterable server sentinel as a project action", () => {
  expect(isFilterableProject("<no repo path>")).toBe(false);
  expect(isFilterableProject("<unknown>")).toBe(false);
  expect(isFilterableProject("/home/user/project")).toBe(true);
});

it("exports raw numeric values and explicit partial-day flags without rounding chart amounts", () => {
  const days = dailyUsage([row("2026-10-24", "known", 0.000123)], "2026-10-24T12:00:00Z", "2026-10-24T13:00:00Z");
  expect(dailyUsageCsv(days).split("\r\n")[1]).toBe("2026-10-24,true,0.000123,70,30,0,0,100,0");
});

it("keeps cents on financial displays without hiding positive sub-cent estimates", () => {
  expect(formatCostUsd(290.5)).toBe("$290.50");
  expect(formatCostUsd(0)).toBe("$0.00");
  expect(formatCostUsd(.000123)).toBe("<$0.01");
  expect(formatCostUsd(.000123, true)).toBe("$0.000123");
});
