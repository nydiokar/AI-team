import { useMemo, useState } from "react";
import { BarChart3, Download } from "lucide-react";
import type { CostExplorer } from "../../domain/cost";
import { dailyUsage, dailyUsageCsv, utcDateLabel, formatCostUsd as formatUsd } from "../../lib/costAnalytics";
import { formatTokens } from "../../lib/costPresentation";
import { cn } from "../../lib/cn";

export function CostTimeline({ data }: { data: CostExplorer }) {
  const [metric, setMetric] = useState<"usd" | "tokens">("usd");
  const [selected, setSelected] = useState<string | null>(null);
  const days = useMemo(() => dailyUsage(data.series, data.from ?? "", data.to ?? ""), [data]);
  const amount = (day: (typeof days)[number]) => metric === "usd" ? day.usd.known : day.tokens.total;
  const format = metric === "usd" ? formatUsd : formatTokens;
  const max = Math.max(0, ...days.map(amount));
  const active = days.find((day) => day.date === selected) ?? days[days.length - 1];
  const total = metric === "usd" ? data.totals.usd.known : data.totals.tokens.total;
  const peak = days.reduce<(typeof days)[number] | null>((best, day) => !best || amount(day) > amount(best) ? day : best, null);
  const download = () => {
    const url = URL.createObjectURL(new Blob([dailyUsageCsv(days)], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `cost-daily-${days[0]?.date ?? "usage"}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="mt-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="text-[28px] font-semibold leading-none tracking-tight tabular-nums text-ink">{format(total)}</div>
          <div className="mt-2 text-xs text-ink-muted">{metric === "usd" ? "Known USD in selected window" : "Recorded tokens in selected window"}</div>
        </div>
        <div className="cost-segment" role="group" aria-label="Timeline metric">
          {(["usd", "tokens"] as const).map((value) => (
            <button key={value} type="button" aria-pressed={metric === value} onClick={() => setMetric(value)}>{value === "usd" ? "Spend" : "Tokens"}</button>
          ))}
        </div>
      </div>

      {days.length > 0 ? (
        <>
          <div className="mt-7 flex gap-3" aria-label={`Daily ${metric === "usd" ? "known USD" : "token volume"} chart, UTC`} role="group">
            <div className="flex h-44 w-12 shrink-0 flex-col justify-between pb-0.5 text-right font-mono text-[10px] tabular-nums text-ink-muted" aria-hidden="true">
              <span>{format(max)}</span><span>{format(max / 2)}</span><span>{format(0)}</span>
            </div>
            <div className="relative min-w-0 flex-1">
              <div className="pointer-events-none absolute inset-x-0 top-0 flex h-44 flex-col justify-between" aria-hidden="true">
                {[0, 1, 2].map((n) => <div key={n} className="border-t border-dashed border-hairline/65" />)}
              </div>
              <div className="relative flex h-44 items-end gap-1 sm:gap-2">
                {days.map((day) => (
                  <button key={day.date} type="button"
                    aria-label={`${utcDateLabel(day.date)} UTC${day.partial ? ", partial day" : ""}: ${format(amount(day))}${metric === "tokens" ? " tokens" : " known USD"}`}
                    aria-pressed={active?.date === day.date}
                    onClick={() => setSelected(day.date)} onFocus={() => setSelected(day.date)}
                    className="cost-bar-target group relative flex h-full min-w-0 flex-1 items-end justify-center rounded-t-md"
                  >
                    <span className={cn("cost-bar block w-full max-w-14 rounded-t-[4px]", day.partial && "cost-bar-partial", active?.date === day.date && "cost-bar-active")}
                      style={{ height: `${max > 0 ? amount(day) / max * 100 : 0}%`, minHeight: amount(day) > 0 ? "2px" : "0" }} />
                  </button>
                ))}
              </div>
              <div className="mt-3 flex justify-between gap-2 font-mono text-[10px] text-ink-muted" aria-hidden="true">
                <span>{utcDateLabel(days[0].date)}</span>
                {days.length > 3 && <span>{utcDateLabel(days[Math.floor((days.length - 1) / 2)].date)}</span>}
                {days.length > 1 && <span>{utcDateLabel(days[days.length - 1].date)}</span>}
              </div>
            </div>
          </div>
          <div className="mt-5 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-hairline/60 pt-3 text-xs">
            <label className="flex min-h-11 items-center gap-2 text-ink-soft">
              Inspect day
              <select aria-label="Inspect UTC day" className="cost-day-select" value={active?.date ?? ""} onChange={(e) => setSelected(e.target.value)}>
                {days.map((day) => <option key={day.date} value={day.date}>{utcDateLabel(day.date)}{day.partial ? " · partial" : ""}</option>)}
              </select>
            </label>
            <span className="font-mono text-sm tabular-nums text-accent">{active ? format(amount(active)) : "—"}{metric === "tokens" ? " tokens" : ""}</span>
          </div>
          <p className="text-[11px] leading-relaxed text-ink-muted">
            UTC days · Striped bars are partial days.
            {peak && amount(peak) > 0 && <> Highest recorded bucket: {utcDateLabel(peak.date)} ({format(amount(peak))}).</>}
          </p>
          {data.totals.tokens.total === 0 && <p className="mt-3 text-xs text-ink-soft">No recorded usage in this window.</p>}
          {data.totals.tokens.total > 0 && data.totals.usd.known === 0 && metric === "usd" && <p className="mt-3 text-xs text-warn">Usage recorded without a known price. Switch to Tokens to see its volume.</p>}
          <details className="cost-details mt-4">
            <summary>View daily data</summary>
            <div className="overflow-x-auto">
              <table className="cost-data-table">
                <caption className="sr-only">Daily captured usage in UTC, limited to the selected window</caption>
                <thead><tr><th scope="col">UTC day</th><th scope="col">Known USD</th><th scope="col">Tokens</th><th scope="col">Unpriced tokens</th></tr></thead>
                <tbody>{days.map((day) => <tr key={day.date}><th scope="row">{day.date}{day.partial ? " *" : ""}</th><td>{formatUsd(day.usd.known, true)}</td><td>{day.tokens.total.toLocaleString("en-US")}</td><td>{day.usd.unpricedTokens.toLocaleString("en-US")}</td></tr>)}</tbody>
              </table>
            </div>
          </details>
          <button type="button" className="cost-text-button gap-1.5" onClick={download}><Download className="size-3.5" aria-hidden="true" />Export daily CSV</button>
        </>
      ) : <p className="flex items-center gap-2 py-12 text-sm text-ink-muted"><BarChart3 className="size-4" /> No daily buckets available.</p>}
    </div>
  );
}
