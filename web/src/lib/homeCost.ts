/**
 * homeCost — pure derivation of a compact daily spend series for the Home
 * screen's cost sparkline, from the A65 cost explorer read model.
 *
 * The explorer returns (time-bucket × dimension) rows; we fold them to one
 * honest USD number per day (only priced models contribute to `usd.known`).
 */
import type { CostExplorer } from "../domain/cost";

export interface DailySpend {
  /** Day bucket label (as the API returned it, e.g. "2026-10-22"). */
  bucket: string;
  /** Summed known USD for that day. */
  usd: number;
}

/** Fold explorer series into one known-USD value per day, ordered oldest→newest. */
export function dailySpend(explorer: CostExplorer | undefined | null): DailySpend[] {
  if (!explorer || !explorer.series?.length) return [];
  const byDay = new Map<string, number>();
  for (const row of explorer.series) {
    byDay.set(row.bucket, (byDay.get(row.bucket) ?? 0) + (row.usd?.known ?? 0));
  }
  return [...byDay.entries()]
    .map(([bucket, usd]) => ({ bucket, usd }))
    .sort((a, b) => a.bucket.localeCompare(b.bucket));
}

/** Window total known USD (prefer the server's totals; fall back to the series). */
export function totalKnownUsd(explorer: CostExplorer | undefined | null): number {
  if (!explorer) return 0;
  if (explorer.totals?.usd) return explorer.totals.usd.known ?? 0;
  return dailySpend(explorer).reduce((sum, d) => sum + d.usd, 0);
}

/**
 * Map a numeric series to SVG polyline points within a [w × h] box (0,0 at the
 * top-left). A flat/empty series returns a single baseline point so the caller
 * can still render something honest.
 */
export function sparklinePoints(values: number[], w: number, h: number, pad = 1): string {
  if (values.length === 0) return "";
  const max = Math.max(...values, 0);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const innerH = h - pad * 2;
  const step = values.length > 1 ? (w - pad * 2) / (values.length - 1) : 0;
  return values
    .map((v, i) => {
      const x = pad + i * step;
      const y = pad + innerH - ((v - min) / span) * innerH;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}
