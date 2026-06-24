/**
 * System screen — LIVE (UI-1 gate). Target list binds /api/nodes via useTargets,
 * using the derived `live` flag + heartbeat_age_sec, NOT the stale status column
 * (gap-doc §2). Each target is an elevated card with a heartbeat readout.
 */
import { Cpu, HeartPulse, Layers, Settings2 } from "lucide-react";
import { Link } from "react-router-dom";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { HealthChip } from "../components/ui/StatusChip";
import { useTargets } from "../hooks/useLiveData";
import { useActivityLog } from "../hooks/useActivityLog";
import type { LogSeverity, LogLine } from "../transport/eventLog";

function ageLabel(sec: number | null): string {
  if (sec == null) return "never seen";
  if (sec < 90) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  return `${Math.round(sec / 3600)}h ago`;
}

// Same severity palette as the session timeline (SessionTimeline.tsx).
const SEVERITY_DOT: Record<LogSeverity, string> = {
  info: "bg-ink-muted",
  success: "bg-ok",
  warning: "bg-warn",
  error: "bg-bad",
};

function clockLabel(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString();
}

/** Max rows rendered on the phone (the underlying stream is already bounded to 500). */
const MAX_ROWS = 100;

function ActivityRow({ line }: { line: LogLine }) {
  return (
    <div className="flex items-start gap-2.5 px-4 py-2 text-[13px]">
      <span className={`mt-1.5 size-1.5 shrink-0 rounded-full ${SEVERITY_DOT[line.severity]}`} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-ink-soft">{line.text}</p>
        <div className="mt-0.5 flex items-center gap-2 text-[11px] text-ink-muted">
          <span className="font-mono text-accent/80">{line.kind}</span>
          {line.sessionId && (
            <Link to={`/sessions/${line.sessionId}`} className="text-accent/80">
              session
            </Link>
          )}
          {clockLabel(line.at) && <span className="ml-auto">{clockLabel(line.at)}</span>}
        </div>
      </div>
    </div>
  );
}

export function SystemScreen() {
  const { data: targets, isLoading, error } = useTargets();
  const { lines, connection } = useActivityLog();
  const rows = lines.slice(0, MAX_ROWS);

  return (
    <div className="pb-8">
      <CompactTopBar title="System" subtitle="Live · targets & health" />

      <SectionHeader label="Targets" count={targets?.length} />
      <div className="space-y-2.5 px-4">
        {isLoading && <p className="py-6 text-center text-sm text-ink-muted">Loading nodes…</p>}
        {error && <p className="py-6 text-center text-sm text-bad">Couldn't load nodes.</p>}

        {(targets ?? []).map((t) => (
          <div key={t.id} className="card-elev rounded-xl px-4 py-3.5">
            <div className="flex items-center gap-2">
              <Cpu className="size-4 text-ink-muted" />
              <span className="min-w-0 flex-1 truncate text-[15px] font-medium text-ink">
                {t.id}
              </span>
              <HealthChip health={t.health} />
            </div>
            <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-ink-muted">
              <span className="inline-flex items-center gap-1">
                <HeartPulse className={`size-3.5 ${t.live ? "text-ok" : "text-ink-muted"}`} />
                {ageLabel(t.heartbeatAgeSec)}
              </span>
              {t.backends.length > 0 && (
                <span className="inline-flex items-center gap-1">
                  <Layers className="size-3.5" />
                  <span className="font-mono text-accent/90">{t.backends.join(" · ")}</span>
                </span>
              )}
              <span>max {t.maxConcurrent}</span>
            </div>
          </div>
        ))}

        {!isLoading && !error && (targets ?? []).length === 0 && (
          <p className="py-6 text-center text-sm text-ink-muted">No registered nodes.</p>
        )}
      </div>

      <SectionHeader label="Live activity" count={rows.length || undefined} />
      <div className="card-elev mx-4 divide-y divide-hairline rounded-xl">
        {rows.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-ink-muted">
            {connection === "reconnecting"
              ? "Reconnecting — showing last known activity…"
              : "No activity yet."}
          </p>
        ) : (
          rows.map((line) => <ActivityRow key={line.id} line={line} />)
        )}
      </div>

      <SectionHeader label="Settings" />
      <div className="px-4">
        <div className="card-elev flex items-start gap-3 rounded-xl px-4 py-3.5 text-ink-soft">
          <Settings2 className="mt-0.5 size-4 text-ink-muted" />
          <p className="text-[13px]">
            Notifications, approval policy, and security settings arrive in later
            phases (spec §7.10).
          </p>
        </div>
      </div>
    </div>
  );
}
