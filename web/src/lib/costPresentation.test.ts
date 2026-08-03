/**
 * costPresentation unit tests — the pure display helpers behind the Cost tab.
 * Range math, honest USD/token formatting, and the basename project label.
 */
import { describe, it, expect } from "vitest";
import {
  COST_RANGES,
  DEFAULT_COST_RANGE,
  rangeToFromTo,
  formatUsd,
  formatTokens,
  projectLabel,
} from "./costPresentation";

describe("rangeToFromTo — the Cost tab's day-range window", () => {
  const now = Date.parse("2026-08-03T12:00:00Z");

  it("anchors 7d (the default) to a 168h window ending at now", () => {
    const { from, to } = rangeToFromTo("7d", now);
    expect(to).toBe(new Date(now).toISOString());
    expect(from).toBe(new Date(now - 168 * 3_600_000).toISOString());
  });

  it("matches the COST_RANGES table for every preset", () => {
    for (const r of COST_RANGES) {
      const { from, to } = rangeToFromTo(r.key, now);
      const span = Date.parse(to) - Date.parse(from);
      expect(span).toBe(r.hours * 3_600_000);
    }
  });

  it("defaults to 7d for an unknown key", () => {
    const { from } = rangeToFromTo("bogus" as never, now);
    expect(from).toBe(new Date(now - 168 * 3_600_000).toISOString());
  });

  it("uses now when no clock is passed", () => {
    const before = Date.now() - 1000;
    const { from, to } = rangeToFromTo("24h");
    expect(Date.parse(from)).toBeLessThanOrEqual(Date.now());
    expect(Date.parse(to)).toBeGreaterThanOrEqual(before);
  });

  it("declares the default range as 7d", () => {
    expect(DEFAULT_COST_RANGE).toBe("7d");
  });
});

describe("formatUsd — honest USD, never fabricated", () => {
  it("renders two decimals under $100", () => {
    expect(formatUsd(22.468)).toBe("$22.47");
    expect(formatUsd(0)).toBe("$0.00");
  });

  it("keeps up to two decimals at/over $100", () => {
    expect(formatUsd(112.345)).toBe("$112.35");
    expect(formatUsd(2244.3)).toBe("$2,244.3");
  });

  it("is a dash for null / NaN — never a made-up number", () => {
    expect(formatUsd(null)).toBe("—");
    expect(formatUsd(undefined)).toBe("—");
    expect(formatUsd(Number.NaN)).toBe("—");
  });
});

describe("formatTokens — compact magnitudes", () => {
  it("scales B/M/K", () => {
    expect(formatTokens(4_400_000_000)).toBe("4.4B");
    expect(formatTokens(892_800_000)).toBe("892.8M");
    expect(formatTokens(12_300)).toBe("12.3K");
  });

  it("keeps small counts exact", () => {
    expect(formatTokens(123)).toBe("123");
    expect(formatTokens(0)).toBe("0");
  });

  it("is a dash for null / NaN", () => {
    expect(formatTokens(null)).toBe("—");
    expect(formatTokens(Number.NaN)).toBe("—");
  });
});

describe("projectLabel — basename of a repo path", () => {
  it("strips directories and trailing slashes", () => {
    expect(projectLabel("/home/cifran/dev/AI-team")).toBe("AI-team");
    expect(projectLabel("C:\\Users\\me\\app\\")).toBe("app");
  });

  it("falls back to the full path when nothing to split", () => {
    expect(projectLabel("AI-team")).toBe("AI-team");
  });
});
