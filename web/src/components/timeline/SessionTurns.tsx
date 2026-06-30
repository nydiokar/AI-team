import { useState } from "react";
import { ChevronDown, ChevronRight, Activity } from "lucide-react";
import type { RawTurn, RawTurnMetrics } from "../../transport/rawApi";
import { relAgeFrom } from "../../lib/time";
import { cn } from "../../lib/cn";

export function compactTokens(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function compactPercent(n: number | null | undefined): string {
  if (n == null) return "-";
  return `${(n * 100).toFixed(n < 0.1 ? 1 : 0)}%`;
}

export function turnDuration(turn: RawTurn): string {
  const ms =
    typeof turn.metrics.wall_time_ms === "number"
      ? turn.metrics.wall_time_ms
      : turn.started_at && turn.ended_at
        ? new Date(turn.ended_at).getTime() - new Date(turn.started_at).getTime()
        : null;
  if (ms == null || Number.isNaN(ms) || ms < 0) return "-";
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

const STATUS_STYLES: Record<string, string> = {
  success: "text-emerald-400",
  running: "text-accent",
  failed: "text-rose-400",
  unknown: "text-amber-400",
};

function statusClass(status: string): string {
  return STATUS_STYLES[status] ?? "text-ink-soft";
}

export function contextTokens(m: RawTurnMetrics): number | null {
  return (
    m.turn_exit_context_tokens ??
    m.peak_context_tokens ??
    m.context_tokens ??
    null
  );
}

function contextLabel(m: RawTurnMetrics): string {
  const ctx = contextTokens(m);
  if (ctx == null) return "ctx -";
  if (m.context_window_tokens != null) {
    return `ctx ${compactTokens(ctx)}/${compactTokens(m.context_window_tokens)}`;
  }
  return `ctx ${compactTokens(ctx)}`;
}

function MetricRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-4 py-1.5">
      <span className="text-[11px] text-ink-muted">{label}</span>
      <span className="font-mono text-[12px] text-ink">{value}</span>
    </div>
  );
}

function latestSessionMetrics(turns: RawTurn[]): RawTurnMetrics | null {
  let latest: RawTurn | null = null;
  for (const turn of turns) {
    const m = turn.metrics;
    if (
      m.session_cumulative_total_tokens != null ||
      m.session_cumulative_input_tokens != null ||
      m.rate_limit_primary_used_percent != null
    ) {
      if (
        latest == null ||
        String(turn.ended_at ?? turn.started_at ?? "") >
          String(latest.ended_at ?? latest.started_at ?? "")
      ) {
        latest = turn;
      }
    }
  }
  return latest?.metrics ?? null;
}

function SessionUsageSummary({ turns }: { turns: RawTurn[] }) {
  const m = latestSessionMetrics(turns);
  if (!m) return null;
  return (
    <div className="card-elev mb-3 overflow-hidden rounded-xl divide-y divide-hairline">
      <MetricRow
        label="Session cumulative"
        value={compactTokens(
          m.session_cumulative_total_tokens ?? m.session_cumulative_input_tokens,
        )}
      />
      <MetricRow label="Cumulative input" value={compactTokens(m.session_cumulative_input_tokens)} />
      <MetricRow label="Cumulative cache read" value={compactTokens(m.session_cumulative_cache_read_tokens)} />
      {m.rate_limit_primary_used_percent != null && (
        <MetricRow label="Primary limit" value={`${m.rate_limit_primary_used_percent}%`} />
      )}
      {m.rate_limit_secondary_used_percent != null && (
        <MetricRow label="Secondary limit" value={`${m.rate_limit_secondary_used_percent}%`} />
      )}
    </div>
  );
}

function TurnCard({ turn }: { turn: RawTurn }) {
  const [open, setOpen] = useState(false);
  const m = turn.metrics;
  const model = turn.observed_models?.[0] ?? turn.requested_model ?? turn.backend ?? "-";
  const turnWork = m.total_token_work ?? m.input_tokens ?? null;

  return (
    <div className="overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="size-3.5 shrink-0 text-ink-muted" />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-ink-muted" />
        )}
        <span className={cn("text-[12px] font-medium", statusClass(turn.final_status))}>
          {turn.final_status}
        </span>
        <span className="truncate font-mono text-[11px] text-ink-soft">{model}</span>
        <span className="ml-auto shrink-0 font-mono text-[11px] text-ink-muted">
          {contextLabel(m)}
        </span>
      </button>

      {open && (
        <div className="divide-y divide-hairline border-t border-hairline bg-surface-1/40">
          <MetricRow label="Duration" value={turnDuration(turn)} />
          {turn.ended_at && (
            <MetricRow label="Ended" value={relAgeFrom(turn.ended_at)} />
          )}
          <MetricRow label="Context used" value={contextLabel(m)} />
          {m.context_used_ratio != null && (
            <MetricRow label="Context filled" value={compactPercent(m.context_used_ratio)} />
          )}
          {m.context_remaining_tokens != null && (
            <MetricRow label="Context free" value={compactTokens(m.context_remaining_tokens)} />
          )}
          <MetricRow label="Turn token work" value={compactTokens(turnWork)} />
          <MetricRow label="Turn input" value={compactTokens(m.input_tokens)} />
          {m.uncached_input_tokens != null && (
            <MetricRow label="Uncached input" value={compactTokens(m.uncached_input_tokens)} />
          )}
          <MetricRow label="Output" value={compactTokens(m.output_tokens)} />
          <MetricRow label="Cache read" value={compactTokens(m.cache_read_tokens)} />
          {m.reasoning_tokens != null && (
            <MetricRow label="Reasoning" value={compactTokens(m.reasoning_tokens)} />
          )}
          {m.aggregate_input_tokens != null && m.metric_quality !== "request" && (
            <MetricRow label="Provider aggregate" value={compactTokens(m.aggregate_input_tokens)} />
          )}
          {m.tool_call_count != null && (
            <MetricRow label="Tool calls" value={String(m.tool_call_count)} />
          )}
          {m.subagent_count != null && Number(m.subagent_count) > 0 && (
            <MetricRow label="Subagents" value={String(m.subagent_count)} />
          )}
          {m.retry_count != null && Number(m.retry_count) > 0 && (
            <MetricRow label="Retries" value={String(m.retry_count)} />
          )}
          {m.metric_quality && (
            <MetricRow label="Coverage" value={String(m.metric_quality)} />
          )}
          <MetricRow label="Turn ID" value={turn.turn_id} />
        </div>
      )}
    </div>
  );
}

export function SessionTurns({
  turns,
  loading,
}: {
  turns: RawTurn[];
  loading: boolean;
}) {
  if (!loading && turns.length === 0) {
    return (
      <div>
        <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
          <Activity className="size-3" /> LLM turns
        </p>
        <p className="card-elev rounded-xl px-4 py-3 text-center text-[12px] text-ink-muted">
          No turn telemetry yet.
        </p>
      </div>
    );
  }

  return (
    <div>
      <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
        <Activity className="size-3" /> LLM turns
        {turns.length > 0 && (
          <span className="ml-1 text-ink-soft">({turns.length})</span>
        )}
      </p>
      <SessionUsageSummary turns={turns} />
      <div className="card-elev overflow-hidden rounded-xl divide-y divide-hairline">
        {turns.map((t) => (
          <TurnCard key={t.turn_id} turn={t} />
        ))}
      </div>
    </div>
  );
}
