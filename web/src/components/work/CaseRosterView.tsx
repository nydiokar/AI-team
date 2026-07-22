/**
 * CaseRosterView — the LIVE operational head of a case (Cockpit).
 *
 * Answers the question the static ledger never did: *who is working right now,
 * on what, for how many tokens, and are any scripts still running or orphaned?*
 *
 *  • Sessions  — the manager + worker sessions on this case, each with role,
 *    model, live status, turn depth, token spend, and its last reported line.
 *    Tap-through to the session's runtime detail.
 *  • Scripts   — the watch_job processes those sessions launched. A job that
 *    shells out `claude -p …` is flagged as an AGENT SPAWN: an off-substrate
 *    worker with no session/telemetry — the exact misuse this view makes visible.
 *
 * Read-only. Duration is derived on the client from started_epoch (the read model
 * is clock-free); everything else is authoritative substrate data.
 */
import { Link } from "react-router-dom";
import { ExternalLink, TriangleAlert, Terminal, Coins, MessagesSquare } from "lucide-react";
import type { CaseRoster, RosterSession, RosterJob } from "../../domain/work";
import { compactTokens } from "../timeline/SessionTurns";
import { roleLabel, roleTone } from "../../lib/workPresentation";
import { cn } from "../../lib/cn";

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 60) return "just now";
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Live elapsed seconds since an epoch, formatted compactly (for running jobs). */
function durationSince(startedEpoch: number | null): string {
  if (!startedEpoch) return "";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - startedEpoch));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

// Session op-status → a status dot color. Busy pulses (actively working now).
function statusDot(status: string | null): { cls: string; pulse: boolean } {
  const s = (status ?? "").toLowerCase();
  if (s === "busy") return { cls: "bg-emerald-400", pulse: true };
  if (s === "awaiting_input") return { cls: "bg-amber-400", pulse: false };
  if (s === "error") return { cls: "bg-rose-400", pulse: false };
  if (s === "closed" || s === "cancelled") return { cls: "bg-slate-500", pulse: false };
  return { cls: "bg-slate-400", pulse: false }; // idle / unknown
}

// Maps a role Tone (workPresentation.roleTone: running | warn | idle) to a chip style.
function toneRing(tone: string): string {
  switch (tone) {
    case "running": return "bg-accent-dim/60 text-accent";
    case "warn": return "bg-amber-500/15 text-amber-300";
    default: return "bg-surface-2 text-ink-soft"; // idle
  }
}

function SessionCard({ s }: { s: RosterSession }) {
  const dot = statusDot(s.status);
  const inner = (
    <>
      <div className="flex items-center gap-2">
        <span className={cn("relative flex size-2 shrink-0 rounded-full", dot.cls)}>
          {dot.pulse && (
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-60" />
          )}
        </span>
        <span className={cn("shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium", toneRing(roleTone(s.role)))}>
          {roleLabel(s.role)}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-ink-soft">
          {s.sessionId || "(no id)"}
        </span>
        {s.present && <ExternalLink className="size-3.5 shrink-0 text-ink-muted" />}
      </div>

      {/* Metrics: model · turns · tokens · last activity */}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-muted">
        {s.model && (
          <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-accent">{s.model}</span>
        )}
        {s.node && s.node !== "__local__" && (
          <span className="font-mono">@{s.node}</span>
        )}
        <span className="inline-flex items-center gap-1">
          <MessagesSquare className="size-3 opacity-60" />
          {s.turnCount} {s.turnCount === 1 ? "turn" : "turns"}
        </span>
        <span className="inline-flex items-center gap-1">
          <Coins className="size-3 opacity-60" />
          {compactTokens(s.tokens.total)} tok
        </span>
        {s.lastActivity && <span className="ml-auto">{relativeTime(s.lastActivity)}</span>}
      </div>

      {!s.present && (
        <p className="mt-1.5 text-[11px] text-amber-300/80">
          Linked to this case, but the session row is gone.
        </p>
      )}
      {s.present && s.lastReport && (
        <p className="mt-1.5 line-clamp-2 text-[11px] leading-snug text-ink-muted">
          “{s.lastReport}”
        </p>
      )}
    </>
  );

  const cardCls =
    "block rounded-xl bg-surface-1 px-3 py-2.5 ring-1 ring-hairline transition-colors";
  return s.present ? (
    <Link to={`/sessions/${encodeURIComponent(s.sessionId)}`} className={cn(cardCls, "hover:bg-surface-2")}>
      {inner}
    </Link>
  ) : (
    <div className={cardCls}>{inner}</div>
  );
}

function jobStatusChip(status: string): string {
  switch (status) {
    case "running": return "bg-emerald-500/15 text-emerald-300";
    case "done": return "bg-slate-600/40 text-ink-soft";
    case "failed":
    case "lost": return "bg-rose-500/15 text-rose-300";
    default: return "bg-surface-2 text-ink-muted";
  }
}

function JobCard({ j }: { j: RosterJob }) {
  const running = j.status === "running";
  return (
    <div
      className={cn(
        "rounded-xl bg-surface-1 px-3 py-2.5 ring-1",
        j.isAgentSpawn ? "ring-amber-500/40" : "ring-hairline",
      )}
    >
      <div className="flex items-center gap-2">
        <Terminal className="size-3.5 shrink-0 text-ink-muted" />
        <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-ink-soft">
          {j.label || j.jobId}
        </span>
        <span className={cn("shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium", jobStatusChip(j.status))}>
          {j.status}
        </span>
      </div>

      {j.commandSummary && (
        <p className="mt-1.5 truncate font-mono text-[11px] text-ink-muted">{j.commandSummary}</p>
      )}

      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-muted">
        {j.node && <span className="font-mono">@{j.node}</span>}
        {running ? (
          <span>running {durationSince(j.startedEpoch)}</span>
        ) : (
          j.exitCode != null && <span>exit {j.exitCode}</span>
        )}
        {j.orphaned && (
          <span className="inline-flex items-center gap-1 text-rose-300">
            <TriangleAlert className="size-3" /> orphaned
          </span>
        )}
      </div>

      {j.isAgentSpawn && (
        <p className="mt-2 flex items-start gap-1.5 rounded-lg bg-amber-500/10 px-2 py-1.5 text-[11px] leading-snug text-amber-200/90">
          <TriangleAlert className="mt-0.5 size-3 shrink-0" />
          <span>
            Agent spawned via <span className="font-mono">watch_job</span> — an off-substrate process
            with no worker session or token telemetry. Dispatch with a model tier instead.
          </span>
        </p>
      )}

      {(j.status === "failed" || j.status === "lost") && j.tail && (
        <p className="mt-1.5 line-clamp-2 font-mono text-[10.5px] leading-snug text-rose-300/70">
          {j.tail}
        </p>
      )}
    </div>
  );
}

export function CaseRosterView({ roster }: { roster: CaseRoster }) {
  const { sessions, jobs, counts, tokenTotals } = roster;

  if (counts.sessions === 0 && counts.jobs === 0) {
    return (
      <p className="rounded-xl bg-surface-1 px-3 py-3 text-[12px] text-ink-muted ring-1 ring-hairline">
        No sessions or scripts on this case yet. When the manager dispatches a worker
        (or launches a script) it appears here live.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {/* One-glance summary: workers live now + case token spend. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-muted">
        <span>
          <span className="font-semibold text-ink-soft">{counts.sessions}</span>{" "}
          {counts.sessions === 1 ? "session" : "sessions"}
        </span>
        {counts.runningJobs > 0 && (
          <span className="text-emerald-300">
            <span className="font-semibold">{counts.runningJobs}</span> script
            {counts.runningJobs === 1 ? "" : "s"} running
          </span>
        )}
        <span className="ml-auto inline-flex items-center gap-1">
          <Coins className="size-3 opacity-60" />
          {compactTokens(tokenTotals.total)} tok total
        </span>
      </div>

      {sessions.length > 0 && (
        <div className="space-y-1.5">
          {sessions.map((s, i) => (
            // Stable key even when a linked session has no id (present=false) — a
            // random key would remount the card on every 5s poll.
            <SessionCard key={s.sessionId || `idx-${i}`} s={s} />
          ))}
        </div>
      )}

      {jobs.length > 0 && (
        <div>
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-ink-muted">
            Scripts · {jobs.length}
          </p>
          <div className="space-y-1.5">
            {jobs.map((j) => (
              <JobCard key={j.jobId} j={j} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
