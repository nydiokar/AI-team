/**
 * Backend Account + Usage (#30/#33). Renders provable facts only:
 * configured/observed model + recent token usage from telemetry. Limits, reset
 * times, and account identity are shown as "unknown" — never fabricated.
 */
import { useEffect, useState } from "react";
import { SectionHeader } from "../ui/SectionHeader";
import { api, type BackendUsageRow } from "../../transport/apiClient";
import { useAuthStore } from "../../stores/authStore";

function totalTokens(usage: Record<string, number> | null): number | null {
  if (!usage) return null;
  if (typeof usage.total_tokens === "number") return usage.total_tokens;
  const inTok = usage.input_tokens ?? 0;
  const outTok = usage.output_tokens ?? 0;
  const sum = inTok + outTok;
  return sum > 0 ? sum : null;
}

function coverageLabel(row: BackendUsageRow): string {
  switch (row.usage_coverage) {
    case "observed":
      return `${row.recent_turn_count} recent turn${row.recent_turn_count === 1 ? "" : "s"}`;
    case "no_data":
      return "no recent activity";
    case "usage_fields_absent":
      return "usage not reported";
    case "telemetry_unavailable":
      return "telemetry unavailable";
    default:
      return row.usage_coverage;
  }
}

function Row({ row }: { row: BackendUsageRow }) {
  const tokens = totalTokens(row.recent_usage);
  const model = row.observed_models[0] || row.configured_model || "default";
  return (
    <div className="card-elev rounded-xl px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[13px] font-medium text-ink">{row.backend}</span>
        <span className="text-[11px] text-ink-muted">{coverageLabel(row)}</span>
      </div>
      <div className="mt-1 flex items-center justify-between gap-3 text-[12px] text-ink-soft">
        <span className="truncate">{model}</span>
        <span className="shrink-0 tabular-nums">
          {tokens !== null ? `${tokens.toLocaleString()} tok` : "—"}
        </span>
      </div>
      {/* Honest unknown — no invented quota. */}
      <p className="mt-1 text-[11px] text-ink-muted">Limits &amp; quota: unknown</p>
    </div>
  );
}

export function BackendUsagePanel() {
  const token = useAuthStore((s) => s.token);
  const [rows, setRows] = useState<BackendUsageRow[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .backendsUsage(token)
      .then((r) => alive && setRows(r.backends))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [token]);

  if (failed || (rows && rows.length === 0)) return null;

  return (
    <>
      <SectionHeader label="Backends" />
      <div className="space-y-2 px-4">
        {rows === null ? (
          <div className="card-elev rounded-xl px-4 py-3 text-[12px] text-ink-muted">
            Loading…
          </div>
        ) : (
          rows.map((r) => <Row key={r.backend} row={r} />)
        )}
        <p className="px-1 text-[11px] leading-relaxed text-ink-muted">
          Token counts are observed usage, not a quota. No backend reports account
          limits or reset times yet.
        </p>
      </div>
    </>
  );
}
