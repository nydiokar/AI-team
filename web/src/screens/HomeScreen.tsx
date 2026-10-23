/**
 * HomeScreen — the app's landing view. It is NOT a menu of shortcuts to the
 * other tabs (issue #235 feedback): it does its own job. Top to bottom —
 *   1. a compact brand row with the only two real entry actions (new session,
 *      resume latest) inline beside the logo;
 *   2. ACCOUNTS & USAGE as the hero: a terminal-styled panel with recognizable
 *      backend marks and honest 5h/7d meters (the thing you actually come to
 *      check);
 *   3. a tiny attention strip — approvals (→ a real decisions list) and
 *      sessions needing you — as two small pills, nothing more;
 *   4. ONLINE nodes only, compact, with live job counts (offline folded away);
 *   5. a subtle spend sparkline.
 * Everything binds to existing live read models; no new polling, no new backend.
 */
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
import { BackendLogo, normalizeBackend } from "../components/home/BackendLogo";
import { BackendAccountSheet, type AccountBackend } from "../components/home/BackendAccountSheet";
import { cn } from "../lib/cn";
import type { Target } from "../domain/models";
import { summarizeProviderUsage, type UsageWindow } from "../lib/homeUsage";
import { dailySpend, totalKnownUsd, sparklinePoints } from "../lib/homeCost";

function BlockMeter({ window: w }: { window: UsageWindow }) {
  const known = w.usedPercent != null;
  const filled = known ? Math.round((w.usedPercent as number) / 10) : 0;
  const color = w.nearLimit ? "text-warn" : "text-accent";
  return (
    <div className="flex items-center gap-2 font-mono text-[12px] leading-5">
      <span className="w-6 shrink-0 text-ink-muted">{w.label}</span>
      <span aria-hidden className={cn("tracking-tight", known ? color : "text-ink-muted/50")}>
        {known ? "█".repeat(filled) + "░".repeat(10 - filled) : "░".repeat(10)}
      </span>
      <span
        className={cn(
          "shrink-0 tabular-nums",
          !known ? "text-ink-muted" : w.nearLimit ? "font-semibold text-warn" : "text-ink-soft",
        )}
      >
        {known ? `${Math.round(w.usedPercent as number)}%${w.nearLimit ? "!" : ""}` : "n/a"}
      </span>
    </div>
  );
}

function AccountMark({ backend, onOpen, size = 7 }: { backend: string; onOpen: (backend: AccountBackend) => void; size?: 6 | 7 }) {
  return backend === "claude" || backend === "codex"
    ? <button type="button" aria-label={`Manage ${backend} accounts`} onClick={() => onOpen(backend)} className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg focus-visible:outline-2 focus-visible:outline-accent"><BackendLogo backend={backend} size={size} /></button>
    : <BackendLogo backend={backend} size={size} />;
}

function AccountsTerminal({ onOpen }: { onOpen: (backend: AccountBackend) => void }) {
  const quota = useQuotaWindows();
  const providers = summarizeProviderUsage(quota.data);
  const enabled = Boolean(quota.data?.enabled);
  return (
    <section aria-label="Accounts and usage" className="px-4 pt-3">
      <div className="overflow-hidden rounded-2xl border border-hairline bg-[#0a0d12]">
        <div className="flex items-center gap-1.5 border-b border-hairline/70 px-3 py-2">
          <span className="size-2 rounded-full bg-bad/70" />
          <span className="size-2 rounded-full bg-warn/70" />
          <span className="size-2 rounded-full bg-ok/70" />
          <span className="ml-2 font-mono text-[11px] text-ink-muted">accounts · usage</span>
          <div className="ml-auto flex">
            <AccountMark backend="claude" onOpen={onOpen} />
            <AccountMark backend="codex" onOpen={onOpen} />
          </div>
        </div>
        <div className="divide-y divide-hairline/50">
          {!enabled || providers.length === 0 ? (
            <div className="px-3 py-4 font-mono text-[12px] text-ink-muted">
              {enabled ? "$ no usage reported yet" : "$ usage telemetry off"}
            </div>
          ) : (
            providers.map((p) => (
              <div key={p.provider} className="flex items-start gap-3 px-3 py-3">
                <AccountMark backend={p.provider} onOpen={onOpen} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="font-mono text-[13px] font-semibold capitalize text-ink">
                      {p.provider}
                    </span>
                    {p.accountHint ? (
                      <span className="truncate font-mono text-[10px] text-ink-muted">
                        {p.accountHint}
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-1.5 space-y-0.5">
                    {p.windows.map((w) => (
                      <BlockMeter key={w.label + w.durationSeconds} window={w} />
                    ))}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </section>
  );
}

function AttentionStrip({ approvals, sessions }: { approvals: number; sessions: number }) {
  if (approvals === 0 && sessions === 0) return null;
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
      {sessions > 0 ? (
        <Link
          to="/sessions"
          className="inline-flex min-h-touch items-center gap-1.5 rounded-full border border-hairline bg-surface-2 px-3 text-[13px] font-medium text-ink-soft"
        >
          <span className="tabular-nums">{sessions}</span> need attention
        </Link>
      ) : null}
    </div>
  );
}

function OnlineNodeRow({ node, onOpen }: { node: Target; onOpen: (backend: AccountBackend) => void }) {
  const running = node.activeTasks.length;
  const backends = [...new Set(node.backends.map(normalizeBackend))].filter((b) => b !== "unknown");
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-hairline bg-surface-1 px-3.5 py-3">
      <span className="size-2 shrink-0 rounded-full bg-ok pulse-dot" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-ink">{node.id}</div>
        <div className="text-[11px] text-ink-muted">
          {running > 0 ? `${running} running` : "idle"}
          {node.slotsTotal != null ? ` · ${node.slotsUsed ?? 0}/${node.slotsTotal} slots` : ""}
        </div>
      </div>
      <div className="flex shrink-0 -space-x-1.5">
        {backends.map((b) => (
          <AccountMark key={b} backend={b} size={6} onOpen={onOpen} />
        ))}
      </div>
    </div>
  );
}

function NodesSection({ nodes, loading, onOpen }: { nodes: Target[]; loading: boolean; onOpen: (backend: AccountBackend) => void }) {
  const online = nodes.filter((n) => n.health === "online");
  const offline = nodes.length - online.length;
  return (
    <section aria-label="Active nodes" className="px-4 pt-5">
      <div className="mb-2 flex items-baseline justify-between">
        <h2 className="text-[13px] font-semibold tracking-tight text-ink-soft">Active nodes</h2>
        {offline > 0 ? (
          <span className="text-[11px] text-ink-muted">{offline} offline</span>
        ) : null}
      </div>
      <div className="space-y-2">
        {loading ? (
          <div className="rounded-2xl border border-hairline bg-surface-1 px-4 py-5 text-center text-[13px] text-ink-muted">
            Loading…
          </div>
        ) : online.length === 0 ? (
          <div className="rounded-2xl border border-hairline bg-surface-1 px-4 py-5 text-center text-[13px] text-ink-muted">
            No nodes online.
          </div>
        ) : (
          online.map((n) => <OnlineNodeRow key={n.id} node={n} onOpen={onOpen} />)
        )}
      </div>
    </section>
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
  const targets = useTargets();
  const sessions = useSessions();
  const approvals = useApprovals("pending");
  const [newOpen, setNewOpen] = useState(false);
  const [accountBackend, setAccountBackend] = useState<AccountBackend | null>(null);

  const nodes = targets.data ?? [];
  const attentionSessions = (sessions.data ?? []).filter((s) => s.needsAttention).length;
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

      <AccountsTerminal onOpen={setAccountBackend} />

      <AttentionStrip approvals={approvals.data?.length ?? 0} sessions={attentionSessions} />

      <NodesSection nodes={nodes} loading={targets.isLoading} onOpen={setAccountBackend} />

      <SpendSparkline />

      {newOpen ? <NewSessionSheet onClose={() => setNewOpen(false)} /> : null}
      {accountBackend ? <BackendAccountSheet key={accountBackend} backend={accountBackend} nodes={nodes} onClose={() => setAccountBackend(null)} /> : null}
    </div>
  );
}
