import { useEffect, useRef, useState } from "react";
import {
  Cpu,
  Layers,
  Settings2,
  Download,
  CheckCircle2,
  AlertCircle,
  Clock,
  Activity,
} from "lucide-react";
import { Link } from "react-router-dom";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { HealthChip } from "../components/ui/StatusChip";
import { Button } from "../components/ui/Button";
import { NodeDetailSheet } from "../components/system/NodeDetailSheet";
import { JobsPanel } from "../components/system/JobsPanel";
import { useTargets } from "../hooks/useLiveData";
import { useActivityLog } from "../hooks/useActivityLog";
import type { Target } from "../domain/models";
import type { LogSeverity, LogLine } from "../transport/eventLog";

type BeforeInstallPromptEvent = Event & { prompt: () => Promise<void> };

function useInstallPrompt() {
  const promptRef = useRef<BeforeInstallPromptEvent | null>(null);
  const [canInstall, setCanInstall] = useState(false);
  const isIos =
    /iphone|ipad|ipod/i.test(navigator.userAgent) &&
    !(window.navigator as { standalone?: boolean }).standalone;

  useEffect(() => {
    const handler = (e: Event) => {
      e.preventDefault();
      promptRef.current = e as BeforeInstallPromptEvent;
      setCanInstall(true);
    };
    window.addEventListener("beforeinstallprompt", handler);
    return () => window.removeEventListener("beforeinstallprompt", handler);
  }, []);

  const install = async () => {
    if (!promptRef.current) return;
    await promptRef.current.prompt();
    promptRef.current = null;
    setCanInstall(false);
  };

  return { canInstall, isIos, install };
}

function ageLabel(sec: number | null): string {
  if (sec == null) return "never seen";
  if (sec < 90) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  return `${Math.round(sec / 3600)}h ago`;
}

const SEVERITY_DOT: Record<LogSeverity, string> = {
  info: "bg-ink-muted",
  success: "bg-ok",
  warning: "bg-warn",
  error: "bg-bad",
};

const SEVERITY_TEXT: Record<LogSeverity, string> = {
  info: "text-ink-muted",
  success: "text-ok",
  warning: "text-warn",
  error: "text-bad",
};

function clockLabel(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

const MAX_ROWS = 60;

function ActivityRow({ line }: { line: LogLine }) {
  return (
    <div className="flex items-start gap-2.5 px-4 py-2.5 text-[13px]">
      <span
        className={`mt-1.5 size-1.5 shrink-0 rounded-full ${SEVERITY_DOT[line.severity]}`}
      />
      <div className="min-w-0 flex-1">
        <p className={`leading-snug ${line.severity === "info" ? "text-ink-soft" : SEVERITY_TEXT[line.severity]}`}>
          {line.text}
        </p>
        <div className="mt-0.5 flex items-center gap-2 text-[11px] text-ink-muted">
          <span className="font-mono text-accent/70">{line.kind}</span>
          {line.sessionId && (
            <Link to={`/sessions/${line.sessionId}`} className="text-accent/70 hover:text-accent">
              → session
            </Link>
          )}
          {clockLabel(line.at) && (
            <span className="ml-auto flex items-center gap-1">
              <Clock className="size-2.5 opacity-50" />
              {clockLabel(line.at)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function TargetCard({ target, onClick }: { target: Target; onClick: () => void }) {
  return (
    <button
      className="card-elev w-full rounded-xl px-4 py-3.5 text-left transition-transform active:scale-[0.99]"
      onClick={onClick}
      aria-label={`View details for ${target.id}`}
    >
      <div className="flex items-center gap-2">
        <Cpu className="size-4 shrink-0 text-ink-muted" />
        <span className="min-w-0 flex-1 truncate font-medium text-ink">{target.id}</span>
        <HealthChip health={target.health} />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
        <span className="inline-flex items-center gap-1">
          {target.live ? (
            <CheckCircle2 className="size-3 text-ok" />
          ) : (
            <AlertCircle className="size-3 text-bad" />
          )}
          {ageLabel(target.heartbeatAgeSec)}
        </span>
        {target.backends.length > 0 && (
          <span className="inline-flex items-center gap-1">
            <Layers className="size-3" />
            <span className="font-mono text-accent/90">{target.backends.join(" · ")}</span>
          </span>
        )}
        <span className="ml-auto text-ink-muted">max {target.maxConcurrent}</span>
      </div>
    </button>
  );
}

export function SystemScreen() {
  const { data: targets, isLoading, error } = useTargets();
  const { lines, connection } = useActivityLog();
  const rows = lines.slice(0, MAX_ROWS);
  const { canInstall, isIos, install } = useInstallPrompt();
  const [selectedTarget, setSelectedTarget] = useState<Target | null>(null);

  const liveCount = (targets ?? []).filter((t) => t.live).length;
  const totalCount = (targets ?? []).length;

  return (
    <div className="pb-8">
      <CompactTopBar title="System" subtitle="Operational status" />

      {/* ── Jobs (most operationally urgent) ── */}
      <SectionHeader label="Jobs" />
      <JobsPanel />

      {/* ── Nodes ── */}
      <SectionHeader
        label="Nodes"
        count={totalCount || undefined}
        action={
          totalCount > 0 ? (
            <span className="text-[11px] text-ink-muted">
              {liveCount}/{totalCount} online
            </span>
          ) : undefined
        }
      />
      <div className="space-y-2.5 px-4">
        {isLoading && (
          <div className="card-elev animate-pulse rounded-xl px-4 py-3.5">
            <div className="flex items-center gap-2">
              <div className="size-4 rounded bg-surface-2" />
              <div className="h-4 flex-1 rounded bg-surface-2" />
              <div className="h-5 w-16 rounded-full bg-surface-2" />
            </div>
          </div>
        )}
        {error && (
          <p className="py-4 text-center text-sm text-bad">Couldn't load nodes.</p>
        )}
        {(targets ?? []).map((t) => (
          <TargetCard key={t.id} target={t} onClick={() => setSelectedTarget(t)} />
        ))}
        {!isLoading && !error && (targets ?? []).length === 0 && (
          <p className="py-6 text-center text-sm text-ink-muted">No registered nodes.</p>
        )}
      </div>

      {/* ── Live activity ── */}
      <SectionHeader
        label="Live activity"
        count={rows.length > 0 ? rows.length : undefined}
        action={
          connection === "reconnecting" ? (
            <span className="flex items-center gap-1 text-[11px] text-warn">
              <Activity className="size-3" />
              Reconnecting…
            </span>
          ) : undefined
        }
      />
      <div className="card-elev mx-4 overflow-hidden rounded-xl divide-y divide-hairline">
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

      {/* ── Settings (least urgent, bottom) ── */}
      <SectionHeader label="Settings" />
      <div className="space-y-2.5 px-4">
        {canInstall && (
          <div className="card-elev flex items-center gap-3 rounded-xl px-4 py-3.5">
            <Download className="size-4 shrink-0 text-accent" />
            <p className="min-w-0 flex-1 text-[13px] text-ink-soft">
              Install as app for quick access.
            </p>
            <Button size="sm" onClick={install}>
              Install
            </Button>
          </div>
        )}
        {isIos && (
          <div className="card-elev flex items-start gap-3 rounded-xl px-4 py-3.5">
            <Download className="mt-0.5 size-4 shrink-0 text-accent" />
            <p className="text-[13px] text-ink-soft">
              Tap <strong className="text-ink">Share</strong> then{" "}
              <strong className="text-ink">Add to Home Screen</strong> to install.
            </p>
          </div>
        )}
        <div className="card-elev flex items-start gap-3 rounded-xl px-4 py-3.5 text-ink-soft">
          <Settings2 className="mt-0.5 size-4 shrink-0 text-ink-muted" />
          <p className="text-[13px]">
            Notifications, approval policy, and security settings coming in later phases.
          </p>
        </div>
      </div>

      {selectedTarget && (
        <NodeDetailSheet
          target={selectedTarget}
          onClose={() => setSelectedTarget(null)}
        />
      )}
    </div>
  );
}
