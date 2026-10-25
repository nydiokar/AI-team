import type { CostBucket, CostSeries, TokenBuckets } from "../domain/cost";

export const COST_COLORS = ["#66d9e8", "#a99cf6", "#f3b66a", "#68cfab", "#ed91b7", "#8793a3"];
const DAY_MS = 86_400_000;

/** Financial display keeps cents; a positive sub-cent estimate never reads as zero. */
export function formatCostUsd(value: number, exact = false): string {
  if (!Number.isFinite(value)) return "—";
  if (!exact && value > 0 && value < .01) return "<$0.01";
  return value.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: exact ? 6 : 2 });
}

export interface DailyUsage extends CostBucket {
  date: string;
  partial: boolean;
}

function sumBuckets(rows: readonly CostBucket[]): CostBucket {
  const tokens: TokenBuckets = rows.reduce((sum, row) => ({
    input: sum.input + row.tokens.input,
    output: sum.output + row.tokens.output,
    cacheRead: sum.cacheRead + row.tokens.cacheRead,
    cacheCreation: sum.cacheCreation + row.tokens.cacheCreation,
    total: sum.total + row.tokens.total,
  }), { input: 0, output: 0, cacheRead: 0, cacheCreation: 0, total: 0 });
  const unpricedTokens = rows.reduce((sum, row) => sum + row.usd.unpricedTokens, 0);
  return {
    tokens,
    usd: {
      known: rows.reduce((sum, row) => sum + row.usd.known, 0),
      unpricedTokens,
      coveragePct: tokens.total > 0 ? Math.max(0, 1 - unpricedTokens / tokens.total) * 100 : 100,
    },
  };
}

/** UTC dates match the existing API buckets; boundary bars contain partial days. */
export function dailyUsage(rows: readonly CostSeries[], from: string, to: string): DailyUsage[] {
  const start = Date.parse(from);
  const end = Date.parse(to);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return [];
  const groups = new Map<string, CostSeries[]>();
  for (const row of rows) {
    const group = groups.get(row.bucket);
    if (group) group.push(row);
    else groups.set(row.bucket, [row]);
  }
  const days: DailyUsage[] = [];
  // The UI supports at most 30 days. Keep malformed inputs bounded as well.
  for (let day = Math.floor(start / DAY_MS) * DAY_MS; day <= end && days.length < 32; day += DAY_MS) {
    const date = new Date(day).toISOString().slice(0, 10);
    days.push({ date, partial: start > day || end < day + DAY_MS, ...sumBuckets(groups.get(date) ?? []) });
  }
  return days;
}

/** Reuse the daily model dataset; no extra query and no averaging percentages. */
export function aggregateModels(rows: readonly CostSeries[]): CostSeries[] {
  const groups = new Map<string, CostSeries[]>();
  for (const row of rows) {
    const group = groups.get(row.dim);
    if (group) group.push(row);
    else groups.set(row.dim, [row]);
  }
  return [...groups.entries()].map(([dim, items]) => ({
    dim, bucket: "", models: [], ...sumBuckets(items),
  })).sort((a, b) => b.usd.known - a.usd.known || b.tokens.total - a.tokens.total || a.dim.localeCompare(b.dim));
}

export function tokenComposition(tokens: TokenBuckets): { label: string; value: number; pct: number; color: string }[] {
  return [
    { label: "Input", value: tokens.input },
    { label: "Output", value: tokens.output },
    { label: "Cache read", value: tokens.cacheRead },
    { label: "Cache write", value: tokens.cacheCreation },
  ].map((item, index) => ({ ...item, color: COST_COLORS[index], pct: tokens.total > 0 ? item.value / tokens.total * 100 : 0 }));
}

export function isFilterableProject(path: string): boolean {
  return Boolean(path) && !path.startsWith("<");
}

export function utcDateLabel(value: string): string {
  return new Date(`${value}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

export function utcWindowLabel(from: string, to: string): string {
  const format = (value: string) => new Date(value).toLocaleString("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  });
  return `${format(from)} – ${format(to)} UTC`;
}

export function dailyUsageCsv(days: readonly DailyUsage[]): string {
  const header = "date_utc,partial_day,known_usd,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,total_tokens,unpriced_tokens";
  return [header, ...days.map((day) => [
    day.date, day.partial, day.usd.known, day.tokens.input, day.tokens.output,
    day.tokens.cacheRead, day.tokens.cacheCreation, day.tokens.total, day.usd.unpricedTokens,
  ].join(","))].join("\r\n");
}
