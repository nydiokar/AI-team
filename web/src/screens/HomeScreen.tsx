/** Home: backend quota summaries, node availability, attention and spend. */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Plus, RotateCcw, ChevronRight } from "lucide-react";
import {
  useTargets,
  useQuotaWindows,
  useSessions,
  useApprovals,
} from "../hooks/useLiveData";
import { useCostExplorer } from "../hooks/useCost";
import { NewSessionSheet } from "../components/sessions/NewSessionSheet";

import { BackendAccountSheet, type AccountBackend } from "../components/home/BackendAccountSheet";
import { cn } from "../lib/cn";
import { NodeActivityCards } from "../components/home/NodeActivityCards";
import { BackendLogo } from "../components/home/BackendLogo";
import { summarizeProviderUsage, type UsageWindow } from "../lib/homeUsage";
import { dailySpend, totalKnownUsd, sparklinePoints } from "../lib/homeCost";

function UsageMeter({ window: w, label }: { window?: UsageWindow; label: string }) {
  const percent = w?.usedPercent;
  const known = percent != null;
  return <div className="min-w-0" title={w?.resetAt ? `Resets ${new Date(w.resetAt).toLocaleString()}` : w?.unknownReason ?? "Usage unavailable"}>
    <div className="mb-1.5 flex items-center justify-between gap-1 text-[11px]">
      <span className="text-ink-muted">{label}</span>
      <span className={cn("tabular-nums", w?.nearLimit ? "font-semibold text-warn" : "text-ink-soft")}>{known ? `${percent > 0 && percent < 1 ? "<1" : Math.round(percent)}%` : "—"}</span>
    </div>
    <div role={known ? "meter" : undefined} aria-label={`${label} quota used`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined} className="h-1.5 overflow-hidden rounded-full bg-surface-3">
      {known && <div className={cn("h-full rounded-full", w?.nearLimit ? "bg-warn" : "bg-accent")} style={{ width: `${percent}%` }} />}
    </div>
  </div>;
}

function BackendAccounts({ onOpen }: { onOpen: (backend: AccountBackend) => void }) {
  const quota = useQuotaWindows();
  const providers = summarizeProviderUsage(quota.data);
  const backends = [...new Set(["claude", "codex", "opencode", ...providers.map((p) => p.provider)])];
  return <section aria-label="Accounts and usage" className="px-4 pt-5">
    <div className="mb-2 flex items-baseline justify-between">
      <h2 className="text-[13px] font-semibold text-ink-soft">Backends</h2>
      <span className="text-[11px] text-ink-muted" title="Highest observed overall usage across accounts">Quota used</span>
    </div>
    <div className="divide-y divide-hairline overflow-hidden rounded-2xl border border-hairline bg-surface-1">
      {backends.map((backend) => {
        const usage = providers.find((p) => p.provider === backend);
        const manageable = backend === "claude" || backend === "codex";
        return <div key={backend} className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)] items-center gap-3 px-3.5 py-4">
          {manageable ? <button type="button" aria-label={`Manage ${backend} accounts`} onClick={() => onOpen(backend)} className="flex min-h-11 min-w-0 items-center gap-2 text-left focus-visible:outline-2 focus-visible:outline-accent">
            <BackendLogo backend={backend} size={6} />
            <span className="min-w-0"><span className="block text-xs font-semibold text-ink">{backend === "claude" ? "Claude Code" : "Codex"}</span>
            <span className="flex items-center gap-0.5 text-[10px] text-ink-muted">Accounts<ChevronRight className="size-3" /></span></span>
          </button> : <span className="flex min-w-0 items-center gap-2"><BackendLogo backend={backend} size={6} /><span className="text-xs font-semibold text-ink">{backend === "opencode" ? "OpenCode" : backend}</span></span>}
          <UsageMeter label="5 hours" window={usage?.windows.find((w) => w.label === "5h")} />
          <UsageMeter label="7 days" window={usage?.windows.find((w) => w.label === "7d")} />
        </div>;
      })}
    </div>
    <p className="mt-2 text-[10px] leading-relaxed text-ink-muted">Plan limits, separate from session context. Codex reflects the latest session observation (up to 15 min). OpenCode limits depend on its provider.</p>
  </section>;
}

function AttentionStrip({ approvals }: { approvals: number }) {
  if (approvals === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 px-4 pt-3">
      {approvals > 0 ? (
        <Link
          to="/approvals"
          className="inline-flex min-h-touch items-center gap-1.5 rounded-full border border-warn/40 bg-warm-dim/50 px-3 text-[13px] font-medium text-warn"
        >
          <span className="tabular-nums">{approvals}</span> approval{approvals > 1 ? "s" : ""} to review
          <ChevronRight className="size-3.5" />
        </Link>
      ) : null}

    </div>
  );
}

function SpendSparkline() {
  const to = useMemo(() => new Date(), []);
  const from = useMemo(() => new Date(Date.now() - 7 * 86400_000), []);
  const explorer = useCostExplorer({
    dimension: "backend",
    granularity: "day",
    from: from.toISOString(),
    to: to.toISOString(),
  });
  const days = dailySpend(explorer.data);
  const total = totalKnownUsd(explorer.data);
  const points = sparklinePoints(days.map((d) => d.usd), 120, 28);
  const hasData = days.length > 0 && total > 0;
  return (
    <section className="px-4 pb-8 pt-5">
      <Link
        to="/cost"
        className="flex items-center gap-3 rounded-2xl border border-hairline bg-surface-1 px-4 py-3"
      >
        <div className="min-w-0">
          <div className="text-[11px] text-ink-muted">Spend · 7d</div>
          <div className="text-[17px] font-semibold tabular-nums text-ink">
            ${total.toFixed(2)}
          </div>
        </div>
        <div className="flex-1" />
        {hasData ? (
          <svg viewBox="0 0 120 28" className="h-7 w-[120px] shrink-0" preserveAspectRatio="none">
            <polyline
              points={points}
              fill="none"
              stroke="var(--color-accent)"
              strokeWidth="1.5"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </svg>
        ) : (
          <span className="text-[11px] text-ink-muted">no priced spend yet</span>
        )}
        <ChevronRight className="size-4 shrink-0 text-ink-muted" />
      </Link>
    </section>
  );
}

export function HomeScreen() {
  const targets = useTargets(true);
  const sessions = useSessions();
  const approvals = useApprovals("pending");
  const [newOpen, setNewOpen] = useState(false);
  const [accountBackend, setAccountBackend] = useState<AccountBackend | null>(null);

  const nodes = targets.data ?? [];
  const latestSessionId = (sessions.data ?? [])[0]?.id ?? null;

  return (
    <div>
      <header className="flex items-center gap-3 px-4 pb-1 pt-5">
        <img
          src="/icons/icon-192.png"
          alt=""
          aria-hidden
          className="size-9 shrink-0 rounded-xl border border-hairline"
        />
        <div className="min-w-0 flex-1">
          <h1 className="text-[16px] font-semibold leading-tight tracking-tight text-ink">AI-Team</h1>
          <p className="text-[11px] text-ink-muted">Gateway control</p>
        </div>
        <button
          type="button"
          onClick={() => setNewOpen(true)}
          className="inline-flex min-h-touch items-center gap-1.5 rounded-full bg-accent-dim px-3.5 text-[13px] font-semibold text-accent"
        >
          <Plus className="size-4" strokeWidth={2.2} /> New
        </button>
        {latestSessionId ? (
          <Link
            to={`/sessions/${latestSessionId}`}
            aria-label="Resume latest session"
            className="inline-flex size-11 shrink-0 items-center justify-center rounded-full border border-hairline text-ink-soft"
          >
            <RotateCcw className="size-[18px]" strokeWidth={1.9} />
          </Link>
        ) : null}
      </header>

      <AttentionStrip approvals={approvals.data?.length ?? 0} />

      <NodeActivityCards nodes={nodes} loading={targets.isLoading} error={targets.isError} />

      <BackendAccounts onOpen={setAccountBackend} />

      <SpendSparkline />

      {newOpen ? <NewSessionSheet onClose={() => setNewOpen(false)} /> : null}
      {accountBackend ? <BackendAccountSheet key={accountBackend} backend={accountBackend} nodes={nodes} onClose={() => setAccountBackend(null)} /> : null}
    </div>
  );
}
