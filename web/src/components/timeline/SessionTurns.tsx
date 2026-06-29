/**
 * LLM turn observability (Feature #37) + context-usage display (Feature #35).
 *
 * Surfaces the telemetry the backend already exposes at GET /api/turns — one row
 * per agent turn from the llm_turns projection — inside the Session "Info" tab.
 * Each turn shows its status, model, timing and headline context-token count;
 * expanding it reveals the full token accounting the projection computes.
 *
 * On #35: the backend reports context-token COUNTS, not a percentage — there is
 * no per-model context-window size to divide by — so we show the count
 * ("ctx 48.2k"), not a fabricated "%". A true percentage needs a model-window
 * table added backend-side later; until then a count is the honest signal.
 */
import { useState } from "react";
import { ChevronDown, ChevronRight, Activity } from "lucide-react";
import type { RawTurn, RawTurnMetrics } from "../../transport/rawApi";
import { relAgeFrom } from "../../lib/time";
import { cn } from "../../lib/cn";

/** Compact a token count: 48217 → "48.2k", 980 → "980", null → "—". */
export function compactTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Turn wall time from started/ended ISO stamps → "1.4s" / "2m 03s" / "—". */
export function turnDuration(turn: RawTurn): string {
  // Prefer the projection's measured wall time; fall back to the stamps.
  const ms =
    typeof turn.metrics.wall_time_ms === "number"
      ? turn.metrics.wall_time_ms
      : turn.started_at && turn.ended_at
        ? new Date(turn.ended_at).getTime() - new Date(turn.started_at).getTime()
        : null;
  if (ms == null || Number.isNaN(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

// Turn `final_status` vocabulary, from src/core/telemetry_projection.py: the turn
// starts "running" and is set from the turn-completed event's status attr —
// observed as "success" / "failed", with "unknown" as the fallback. (NOT
// "completed" — that string never appears; an earlier guess that left successful
// turns rendering in default grey.)
const STATUS_STYLES: Record<string, string> = {
  success: "text-emerald-400",
  running: "text-accent",
  failed: "text-rose-400",
  unknown: "text-amber-400",
};

function statusClass(status: string): string {
  return STATUS_STYLES[status] ?? "text-ink-soft";
}

/** The "context usage" headline for #35 — the largest context-token signal we
 * have for the turn, preferring the peak, then the turn-exit, then the raw count. */
export function contextTokens(m: RawTurnMetrics): number | null {
  return (
    m.peak_context_tokens ??
    m.turn_exit_context_tokens ??
    m.context_tokens ??
    null
  );
}

function MetricRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-4 py-1.5">
      <span className="text-[11px] text-ink-muted">{label}</span>
      <span className="font-mono text-[12px] text-ink">{value}</span>
    </div>
  );
}

function TurnCard({ turn }: { turn: RawTurn }) {
  const [open, setOpen] = useState(false);
  const m = turn.metrics;
  const model = turn.observed_models?.[0] ?? turn.requested_model ?? turn.backend ?? "—";
  const ctx = contextTokens(m);

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
          {/* Context-token count (#35) — a count, deliberately not a % */}
          ctx {compactTokens(ctx)}
        </span>
      </button>

      {open && (
        <div className="divide-y divide-hairline border-t border-hairline bg-surface-1/40">
          <MetricRow label="Duration" value={turnDuration(turn)} />
          {turn.ended_at && (
            <MetricRow label="Ended" value={relAgeFrom(turn.ended_at)} />
          )}
          <MetricRow label="Context tokens" value={compactTokens(ctx)} />
          <MetricRow label="Input" value={compactTokens(m.input_tokens)} />
          <MetricRow label="Output" value={compactTokens(m.output_tokens)} />
          <MetricRow label="Cache read" value={compactTokens(m.cache_read_tokens)} />
          {m.reasoning_tokens != null && (
            <MetricRow label="Reasoning" value={compactTokens(m.reasoning_tokens)} />
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
  // Telemetry is best-effort: an empty list is a normal state (a session that
  // hasn't run a turn, or a backend that doesn't emit turn telemetry). Say so
  // plainly rather than rendering an empty card.
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
      <div className="card-elev overflow-hidden rounded-xl divide-y divide-hairline">
        {turns.map((t) => (
          <TurnCard key={t.turn_id} turn={t} />
        ))}
      </div>
    </div>
  );
}
