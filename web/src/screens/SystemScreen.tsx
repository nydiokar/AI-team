import { useEffect, useRef, useState } from "react";
import { Download, Activity } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { Button } from "../components/ui/Button";
import { PushSetting } from "../components/system/PushSetting";
import { useMeshHealth } from "../hooks/useLiveData";
import type { RawMeshHealthResponse } from "../transport/rawApi";
import { relAge } from "../lib/time";

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

function sampleAge(sampledAt: string | undefined): string {
  if (!sampledAt) return "no samples";
  const t = new Date(sampledAt).getTime();
  if (!Number.isFinite(t)) return "sample time unknown";
  return relAge(Math.max(0, Math.round((Date.now() - t) / 1000)));
}

function MeshStat({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string;
  tone?: "default" | "warn" | "bad";
}) {
  const text = tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-ink";
  return (
    <div className="min-w-0 px-1 py-1">
      <div className={`truncate text-[15px] font-semibold tabular-nums ${text}`}>{value}</div>
      <div className="mt-0.5 truncate text-[11px] text-ink-muted">{label}</div>
    </div>
  );
}

function MeshHealthPanel({
  data,
  isLoading,
  error,
}: {
  data: RawMeshHealthResponse | undefined;
  isLoading: boolean;
  error: unknown;
}) {
  const latest = data?.history.recent[0];
  const current = data?.current;
  const load = current?.mesh_load;
  const slotsUsed = load?.slots_used ?? latest?.slots_used ?? 0;
  const slotsTotal = load?.slots_total ?? latest?.slots_total ?? 0;
  const nodesOnline = current?.nodes_online ?? latest?.nodes_online ?? 0;
  const nodesTotal = current?.nodes_total ?? latest?.nodes_total ?? 0;
  const sessionsBusy = current?.sessions_busy ?? 0;
  const sessionsTotal = current?.sessions_total ?? 0;
  const pending = current?.tasks_pending ?? latest?.tasks_pending ?? 0;
  const stalePending = current?.tasks_stale_pending ?? 0;
  const claimed = current?.tasks_claimed ?? latest?.tasks_claimed ?? 0;
  const completed = current?.tasks_completed ?? 0;
  const failed = current?.tasks_failed ?? 0;
  const staleBusy = load?.stale_busy_sessions ?? latest?.stale_busy_sessions ?? 0;
  const staleNodes = load?.stale_live_state_nodes ?? latest?.stale_live_state_nodes ?? [];
  const nodesNoLiveState = load?.nodes_without_live_state ?? latest?.nodes_without_live_state ?? 0;
  const reconcilePending = data?.reconcile.pending ?? 0;
  const reconcileInvalid = data?.reconcile.invalid ?? 0;
  const oldestPendingAt = data?.reconcile.oldest_pending_at ?? null;

  if (isLoading && !data) {
    return (
      <div className="card-elev mx-4 animate-pulse rounded-xl px-4 py-3.5">
        <div className="h-4 w-28 rounded bg-surface-2" />
        <div className="mt-3 grid grid-cols-2 gap-2">
          <div className="h-14 rounded-lg bg-surface-2" />
          <div className="h-14 rounded-lg bg-surface-2" />
          <div className="h-14 rounded-lg bg-surface-2" />
          <div className="h-14 rounded-lg bg-surface-2" />
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="card-elev mx-4 rounded-xl px-4 py-3.5">
        <div className="flex items-center gap-2 text-[13px] text-warn">
          <Activity className="size-3.5" />
          {error ? "Couldn't load mesh health." : "No mesh health data."}
        </div>
      </div>
    );
  }

  return (
    <div className="card-elev mx-4 rounded-xl px-4 py-3.5">
      <div className="flex items-center gap-2 text-[12px] text-ink-muted">
        <Activity className="size-3.5" />
        <span className="min-w-0 flex-1 truncate">latest sample {sampleAge(latest?.sampled_at)}</span>
        {reconcilePending > 0 || reconcileInvalid > 0 ? (
          <span className="rounded-full bg-warm-dim/70 px-2 py-0.5 text-[11px] font-medium text-warn">
            reconcile attention
          </span>
        ) : (
          <span className="rounded-full bg-ok/12 px-2 py-0.5 text-[11px] font-medium text-ok">
            mirror clean
          </span>
        )}
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2">
        <MeshStat
          label="sessions active"
          value={sessionsTotal > 0 ? `${sessionsBusy}/${sessionsTotal}` : String(sessionsBusy)}
          tone={sessionsBusy > 0 ? "default" : "default"}
        />
        <MeshStat label="slots used" value={`${slotsUsed}/${slotsTotal}`} />
        <MeshStat
          label="nodes online"
          value={`${nodesOnline}/${nodesTotal}`}
          tone={nodesOnline === 0 && nodesTotal > 0 ? "bad" : "default"}
        />
        <MeshStat
          label="completed / failed"
          value={`${completed}/${failed}`}
          tone={failed > 0 ? "bad" : "default"}
        />
        <MeshStat
          label="pending / claimed"
          value={`${pending}/${claimed}`}
          tone={stalePending > 0 ? "bad" : pending > 0 ? "warn" : "default"}
        />
        <MeshStat label="stale pending" value={String(stalePending)} tone={stalePending > 0 ? "bad" : "default"} />
        <MeshStat label="stale busy" value={String(staleBusy)} tone={staleBusy > 0 ? "bad" : "default"} />
      </div>

      {(staleNodes.length > 0 || nodesNoLiveState > 0 || reconcilePending > 0 || reconcileInvalid > 0) && (
        <div className="mt-3 space-y-1.5 border-t border-hairline/60 pt-3 text-[12px] text-ink-soft">
          {nodesNoLiveState > 0 && (
            <div className="text-warn">
              ⚠ {nodesNoLiveState} online node{nodesNoLiveState > 1 ? "s" : ""} not reporting live state (slots/tasks unknown)
            </div>
          )}
          {staleNodes.length > 0 && (
            <div className="truncate text-bad">stale live state: {staleNodes.join(", ")}</div>
          )}
          {(reconcilePending > 0 || reconcileInvalid > 0) && (
            <div className="text-warn">
              reconcile: {reconcilePending} pending
              {oldestPendingAt ? ` (oldest ${sampleAge(oldestPendingAt)})` : ""}
              {reconcileInvalid > 0 ? `, ${reconcileInvalid} invalid` : ""}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function SystemScreen() {
  const { data: meshHealth, isLoading: meshHealthLoading, error: meshHealthError } = useMeshHealth();
  const { canInstall, isIos, install } = useInstallPrompt();

  return (
    <div className="pb-8">
      <CompactTopBar title="System" subtitle="Infrastructure health" />

      <SectionHeader label="Mesh" />
      <MeshHealthPanel data={meshHealth} isLoading={meshHealthLoading} error={meshHealthError} />

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
        <PushSetting />
      </div>
    </div>
  );
}
