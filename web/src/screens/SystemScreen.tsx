/**
 * System screen — LIVE (UI-1 gate). Target list binds /api/nodes via useTargets,
 * using the derived `live` flag + heartbeat_age_sec, NOT the stale status column
 * (gap-doc §2). Each target is an elevated card with a heartbeat readout.
 */
import { Cpu, HeartPulse, Layers, Settings2 } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { HealthChip } from "../components/ui/StatusChip";
import { useTargets } from "../hooks/useLiveData";

function ageLabel(sec: number | null): string {
  if (sec == null) return "never seen";
  if (sec < 90) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  return `${Math.round(sec / 3600)}h ago`;
}

export function SystemScreen() {
  const { data: targets, isLoading, error } = useTargets();

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

      <SectionHeader label="Settings" />
      <div className="px-4">
        <div className="card-elev flex items-start gap-3 rounded-xl px-4 py-3.5 text-ink-soft">
          <Settings2 className="mt-0.5 size-4 text-ink-muted" />
          <p className="text-[13px]">
            Notifications, approval policy, security, and diagnostics arrive in
            later phases (spec §7.10).
          </p>
        </div>
      </div>
    </div>
  );
}
