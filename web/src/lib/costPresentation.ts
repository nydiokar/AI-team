/**
 * Cost display helpers: the day-range presets (24h / 48h / 7d / 30d) that drive
 * the Cost tab's time filter, and honest USD/token formatting. All pure — no
 * server data, no fabrication (an unpriced or absent number formats as "—").
 */

export const COST_RANGES = [
  { key: "24h", label: "24h", hours: 24 },
  { key: "48h", label: "48h", hours: 48 },
  { key: "7d", label: "7d", hours: 168 },
  { key: "30d", label: "30d", hours: 720 },
] as const;

export type CostRangeKey = (typeof COST_RANGES)[number]["key"];

export const DEFAULT_COST_RANGE: CostRangeKey = "7d";

/** ISO from/to for a preset, anchored at `now` (the tab's chosen window). */
export function rangeToFromTo(
  key: CostRangeKey,
  now: number = Date.now(),
): { from: string; to: string } {
  const hours = COST_RANGES.find((r) => r.key === key)?.hours ?? 168;
  return {
    from: new Date(now - hours * 3_600_000).toISOString(),
    to: new Date(now).toISOString(),
  };
}

/** USD, honest: two decimals under $100, otherwise thousands-aware. */
export function formatUsd(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: Math.abs(value) < 100 ? 2 : 0,
    maximumFractionDigits: Math.abs(value) < 100 ? 2 : 2,
  })}`;
}

/** Compact token count: 4.4B / 892.8M / 12.3K / 123. */
export function formatTokens(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  return String(value);
}

/** Short human repo label from a full path (basename) for the project filter. */
export function projectLabel(repoPath: string): string {
  const cleaned = repoPath.replace(/\\/g, "/").replace(/\/+$/, "");
  const base = cleaned.split("/").filter(Boolean).pop() ?? cleaned;
  return base || repoPath;
}
