import { describe, expect, it } from "vitest";
import { dailySpend, totalKnownUsd, sparklinePoints } from "./homeCost";
import type { CostExplorer } from "../domain/cost";

const usd = (known: number) => ({ known, unpricedTokens: 0, coveragePct: 100 });
const tokens = { input: 0, output: 0, cacheRead: 0, cacheCreation: 0, total: 0 };
const row = (bucket: string, known: number) => ({ bucket, dim: "claude", tokens, usd: usd(known), models: [] });

function explorer(series: ReturnType<typeof row>[], totalKnown = 0): CostExplorer {
  return {
    dimension: "backend",
    granularity: "day",
    from: null,
    to: null,
    repoPath: null,
    series,
    totals: { tokens, usd: usd(totalKnown) },
    unattributed: { tokens, usd: usd(0), models: [] },
  };
}

describe("dailySpend", () => {
  it("folds (day × dimension) rows to one total per day, oldest first", () => {
    const out = dailySpend(
      explorer([
        row("2026-10-21", 1.5),
        row("2026-10-22", 2.0),
        row("2026-10-21", 0.5), // same day, different backend
      ]),
    );
    expect(out).toEqual([
      { bucket: "2026-10-21", usd: 2.0 },
      { bucket: "2026-10-22", usd: 2.0 },
    ]);
  });

  it("is empty for no data", () => {
    expect(dailySpend(undefined)).toEqual([]);
    expect(dailySpend(explorer([]))).toEqual([]);
  });
});

describe("totalKnownUsd", () => {
  it("prefers the server totals", () => {
    expect(totalKnownUsd(explorer([row("d", 1)], 9.99))).toBe(9.99);
  });
});

describe("sparklinePoints", () => {
  it("maps values into the box with higher spend placed higher (smaller y)", () => {
    const pts = sparklinePoints([0, 10], 100, 20, 1).split(" ");
    expect(pts).toHaveLength(2);
    const y0 = Number(pts[0].split(",")[1]);
    const y1 = Number(pts[1].split(",")[1]);
    expect(y1).toBeLessThan(y0); // the 10 sits above the 0
  });

  it("returns empty for no values", () => {
    expect(sparklinePoints([], 100, 20)).toBe("");
  });
});
