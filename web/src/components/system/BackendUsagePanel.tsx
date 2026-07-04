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

/** Break the summed usage into its named parts so cache read/write is visible
 *  rather than folded into one number. Only parts the backend actually reported
 *  are shown (never fabricate a zero for a field the backend never sent). Claude
 *  uses inclusive-cache semantics — input_tokens already includes cache-read —
 *  so this is a decomposition, not additional spend. */
function usageParts(usage: Record<string, number> | null): { label: string; value: number }[] {
  if (!usage) return [];
  const parts: { label: string; value: number }[] = [
    { label: "in", value: usage.input_tokens ?? -1 },
    { label: "out", value: usage.output_tokens ?? -1 },
    { label: "cache-r", value: usage.cache_read_tokens ?? -1 },
    { label: "cache-w", value: usage.cache_creation_tokens ?? -1 },
  ];
  return parts.filter((p) => p.value >= 0);
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
  const parts = usageParts(row.recent_usage);
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
          {tokens !== null
            ? `${tokens.toLocaleString()} tok${row.usage_aggregation === "peak" ? " peak" : ""}`
            : "—"}
        </span>
      </div>
      {/* Cache read/write is captured by telemetry — surface it instead of
          folding it into one opaque total. */}
      {parts.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] tabular-nums text-ink-muted">
          {parts.map((p) => (
            <span key={p.label}>
              {p.label} {p.value.toLocaleString()}
            </span>
          ))}
        </div>
      )}
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
