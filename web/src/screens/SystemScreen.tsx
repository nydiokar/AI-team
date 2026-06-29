import { useEffect, useMemo, useRef, useState } from "react";
import {
  Cpu,
  Layers,
  Download,
  CheckCircle2,
  Clock,
  Activity,
  ChevronRight,
  ChevronDown,
} from "lucide-react";
import { Link } from "react-router-dom";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { HealthChip } from "../components/ui/StatusChip";
import { Button } from "../components/ui/Button";
import { NodeDetailSheet } from "../components/system/NodeDetailSheet";
import { JobsPanel } from "../components/system/JobsPanel";
import { useSessions, useTargets } from "../hooks/useLiveData";
import { useActivityLog } from "../hooks/useActivityLog";
import type { Target } from "../domain/models";
import type { LogSeverity } from "../transport/eventLog";
import { relAge, clockLabel } from "../lib/time";
import {
  enrichLine,
  indexSessions,
  repoName,
  type EnrichedLine,
} from "../lib/activityFormat";

const MAX_ROWS = 60;

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

// Routine lifecycle chatter (started/running) is quieted so the eye lands on
// what MATTERS (a success, a block, a failure). We keep every event — honesty
// is the point of this feed — but stop giving them all equal weight.
const SEVERITY_TEXT: Record<LogSeverity, string> = {
  info: "text-ink-muted",
  success: "text-ok",
  warning: "text-warn",
  error: "text-bad",
};

const SEVERITY_DOT: Record<LogSeverity, string> = {
  info: "bg-ink-muted",
  success: "bg-ok",
  warning: "bg-warn",
  error: "bg-bad",
};

/** ONE row = ONE real event, but NAMED and (when it has an owner) tappable.
 *  Top line = what happened. Sub-line = which work it happened to (repo ·
 *  backend), resolved from the live sessions — so "Task running" becomes
 *  "Task running / payments-api · codex ›". System lines with no owner stay
 *  honest and non-clickable; we never fabricate a destination. */
function shortId(id: string): string {
  const tail = id.replace(/^.*[_-]/, "");
  return tail.slice(0, 8) || id.slice(0, 8);
}

function ActivityRow({ e, showTime }: { e: EnrichedLine; showTime: boolean }) {
  const { line, session } = e;

  // Subject = the concrete thing this event is about. Prefer the resolved
  // session (repo · backend, tappable); else name the task/host so the row is
  // still identifiable rather than an anonymous "system".
  const subject = session
    ? `${repoName(session.workspace.path)} · ${session.backend}`
    : line.taskId
      ? `task ${shortId(line.taskId)}`
      : e.host ?? "system";

  const inner = (
    <>
      <span className={`mt-1.5 size-1.5 shrink-0 rounded-full ${SEVERITY_DOT[line.severity]}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <p
            className={`min-w-0 flex-1 truncate leading-snug ${SEVERITY_TEXT[line.severity]} ${
              line.severity === "info" ? "" : "font-medium"
            }`}
          >
            {e.title}
          </p>
          {showTime && clockLabel(line.at) && (
            <span className="flex shrink-0 items-center gap-1 text-[11px] tabular-nums text-ink-muted">
              <Clock className="size-2.5 opacity-50" />
              {clockLabel(line.at)}
            </span>
          )}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-ink-muted">
          <span className={`min-w-0 truncate ${session ? "text-ink-soft" : "font-mono opacity-70"}`}>
            {subject}
          </span>
          {e.href && <ChevronRight className="ml-auto size-3 shrink-0 text-ink-muted/60" />}
        </div>
      </div>
    </>
  );

  if (e.href) {
    return (
      <Link
        to={e.href}
        className="flex items-start gap-2.5 px-4 py-2.5 text-[13px] transition-colors hover:bg-surface-2/40"
      >
        {inner}
      </Link>
    );
  }
  return <div className="flex items-start gap-2.5 px-4 py-2.5 text-[13px]">{inner}</div>;
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
          <CheckCircle2 className="size-3 text-ok" />
          {relAge(target.heartbeatAgeSec)}
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

/** Dormant nodes aren't operational status — they're inventory. One muted,
 *  expandable line keeps them reachable without cluttering the live view. */
function OfflineNodes({ nodes, onPick }: { nodes: Target[]; onPick: (t: Target) => void }) {
  const [open, setOpen] = useState(false);
  if (nodes.length === 0) return null;
  return (
    <div className="mt-2">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded-lg bg-surface-1/50 px-3 py-2 text-[12px] text-ink-muted ring-1 ring-hairline/60 transition-colors hover:bg-surface-2/50 hover:text-ink-soft"
      >
        <span className="size-1.5 rounded-full bg-ink-muted/60" />
        <span className="font-medium">{nodes.length} offline</span>
        <span className="text-ink-muted/70">— {open ? "hide" : "show"}</span>
        <ChevronDown className={`ml-auto size-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="mt-1 space-y-1">
          {nodes.map((t) => (
            <button
              key={t.id}
              onClick={() => onPick(t)}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-[13px] text-ink-soft transition-colors hover:bg-surface-2/40"
            >
              <Cpu className="size-3.5 shrink-0 text-ink-muted/60" />
              <span className="min-w-0 flex-1 truncate">{t.id}</span>
              <span className="shrink-0 text-[11px] text-ink-muted">{relAge(t.heartbeatAgeSec)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function SystemScreen() {
  const { data: targets, isLoading, error } = useTargets();
  const { data: sessions } = useSessions();
  const { lines, connection } = useActivityLog();
  const { canInstall, isIos, install } = useInstallPrompt();
  const [selectedTarget, setSelectedTarget] = useState<Target | null>(null);
  const [jobsExpanded, setJobsExpanded] = useState(true);
  const [jobs, setJobs] = useState({ total: 0, running: 0 });

  const all = targets ?? [];
  const liveNodes = all.filter((t) => t.live);
  const offlineNodes = all.filter((t) => !t.live);

  const enriched = useMemo(() => {
    const idx = indexSessions(sessions ?? []);
    return lines.slice(0, MAX_ROWS).map((line) => enrichLine(line, idx));
  }, [lines, sessions]);

  return (
    <div className="pb-8">
      <CompactTopBar title="System" subtitle="Nodes and live activity" />

      {/* ── Jobs — one collapsible header; hidden entirely when there's none ── */}
      {(jobs.total > 0 || jobsExpanded) && (
        <SectionHeader
          label="Jobs"
          count={jobs.total || undefined}
          onToggle={() => setJobsExpanded((v) => !v)}
          expanded={jobsExpanded}
          action={
            jobs.running > 0 ? (
              <span className="flex items-center gap-1 rounded-full bg-accent-dim/60 px-1.5 py-0.5 text-[10px] font-medium text-accent">
                <span className="size-1 rounded-full bg-accent pulse-dot" />
                {jobs.running} running
              </span>
            ) : undefined
          }
        />
      )}
      <JobsPanel expanded={jobsExpanded} onSummary={setJobs} />

      {/* ── Nodes — live first; dormant ones fold away ── */}
      <SectionHeader
        label="Nodes"
        action={
          all.length > 0 ? (
            <span className="text-[11px] text-ink-muted">
              <span className={liveNodes.length > 0 ? "text-ok" : ""}>{liveNodes.length}</span> online
            </span>
          ) : undefined
        }
      />
      <div className="px-4">
        {isLoading && (
          <div className="card-elev animate-pulse rounded-xl px-4 py-3.5">
            <div className="flex items-center gap-2">
              <div className="size-4 rounded bg-surface-2" />
              <div className="h-4 flex-1 rounded bg-surface-2" />
              <div className="h-5 w-16 rounded-full bg-surface-2" />
            </div>
          </div>
        )}
        {error && <p className="py-4 text-center text-sm text-bad">Couldn't load nodes.</p>}

        <div className="space-y-2.5">
          {liveNodes.map((t) => (
            <TargetCard key={t.id} target={t} onClick={() => setSelectedTarget(t)} />
          ))}
        </div>

        {!isLoading && !error && liveNodes.length === 0 && offlineNodes.length > 0 && (
          <p className="py-3 text-center text-sm text-ink-muted">No nodes online right now.</p>
        )}
        {!isLoading && !error && all.length === 0 && (
          <p className="py-6 text-center text-sm text-ink-muted">No registered nodes.</p>
        )}

        <OfflineNodes nodes={offlineNodes} onPick={setSelectedTarget} />
      </div>

      {/* ── Live activity — one row per piece of WORK, newest-touched first ── */}
      <SectionHeader
        label="Activity"
        count={enriched.length > 0 ? enriched.length : undefined}
        action={
          connection === "reconnecting" ? (
            <span className="flex items-center gap-1 text-[11px] text-warn">
              <Activity className="size-3" />
              Reconnecting…
            </span>
          ) : (
            <span className="flex items-center gap-1 text-[11px] text-ink-muted">
              <span className="size-1.5 rounded-full bg-ok pulse-dot" />
              Live
            </span>
          )
        }
      />
      <div className="card-elev mx-4 divide-y divide-hairline overflow-hidden rounded-xl">
        {enriched.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-ink-muted">
            {connection === "reconnecting"
              ? "Reconnecting — showing last known activity…"
              : "No activity yet."}
          </p>
        ) : (
          enriched.map((e, i) => (
            <ActivityRow
              key={e.line.id}
              e={e}
              showTime={clockLabel(e.line.at) !== clockLabel(enriched[i - 1]?.line.at ?? "")}
            />
          ))
        )}
      </div>

      {/* ── Settings — quiet footnote, not a faux feature card ── */}
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
        <p className="px-1 text-[11px] leading-relaxed text-ink-muted">
          Notifications, approval policy, and security settings arrive in a later phase.
        </p>
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
