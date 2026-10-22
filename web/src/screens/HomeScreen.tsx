/**
 * HomeScreen — the app's mobile-first landing view. Answers, at a glance,
 * "what can I do right now, and is everything healthy?" (issue #235).
 *
 * Design rationale (see PR): one vertical, thumb-reachable column — brand,
 * quick actions, an attention glance (the single "do I need to act?" line),
 * per-node health, and per-account usage meters. Everything binds to the
 * EXISTING live read models (useTargets / useQuotaWindows / useSessions /
 * useApprovals) through their covering poll+SSE channels — no new polling.
 *
 * Honest scoping: usage/quota is account-scoped (principal_hash), not
 * node-scoped — the gateway has no node→account binding yet — so Nodes (health)
 * and Accounts (usage) are deliberately separate sections rather than faking a
 * per-node account row. True per-node login/credential state ("needs re-login")
 * is the unified BackendAuth read model (docs/BACKEND_AUTH_UNIFIED_DESIGN.md),
 * a follow-on slice; here we surface it honestly as "not yet monitored".
 */
import { Link } from "react-router-dom";
import {
  Plus,
  Briefcase,
  Activity,
  HandCoins,
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
} from "lucide-react";
import { useTargets, useQuotaWindows, useSessions, useApprovals } from "../hooks/useLiveData";
import { HealthChip } from "../components/ui/StatusChip";
import { SectionHeader } from "../components/ui/SectionHeader";
import { cn } from "../lib/cn";
import type { Target } from "../domain/models";
import {
  summarizeProviderUsage,
  type ProviderUsage,
  type UsageWindow,
} from "../lib/homeUsage";

function heartbeatAge(sec: number | null): string {
  if (sec == null) return "never";
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  return `${Math.round(sec / 3600)}h ago`;
}

// A tinted monogram badge standing in for a provider/account icon. Accessible by
// construction: the letter + the adjacent text label carry the meaning, never
// colour alone (issue #235 accessibility; acceptance "status not by colour only").
const PROVIDER_TINT: Record<string, string> = {
  claude: "bg-accent-dim text-accent",
  codex: "bg-ok/15 text-ok",
  opencode: "bg-warm-dim/70 text-warn",
};

function ProviderBadge({ provider }: { provider: string }) {
  const tint = PROVIDER_TINT[provider.toLowerCase()] ?? "bg-surface-3 text-ink-soft";
  return (
    <span
      aria-hidden
      className={cn(
        "flex size-7 shrink-0 items-center justify-center rounded-lg text-[13px] font-semibold uppercase",
        tint,
      )}
    >
      {provider.slice(0, 1)}
    </span>
  );
}

function HomeHeader() {
  return (
    <header className="flex items-center gap-3 px-4 pb-1 pt-6">
      <img
        src="/icons/icon-192.png"
        alt=""
        aria-hidden
        className="size-9 rounded-xl border border-hairline"
      />
      <div className="min-w-0">
        <h1 className="text-[17px] font-semibold tracking-tight text-ink">AI-Team</h1>
        <p className="text-[11px] text-ink-muted">Gateway control</p>
      </div>
    </header>
  );
}

function ActionTile({
  to,
  label,
  Icon,
  primary,
}: {
  to: string;
  label: string;
  Icon: typeof Plus;
  primary?: boolean;
}) {
  return (
    <Link
      to={to}
      className={cn(
        "flex min-h-touch items-center gap-2.5 rounded-2xl border px-4 py-3 text-sm font-medium transition-colors",
        primary
          ? "border-accent/40 bg-accent-dim text-accent"
          : "border-hairline bg-surface-1 text-ink-soft hover:bg-surface-2 hover:text-ink",
      )}
    >
      <Icon className="size-[18px] shrink-0" strokeWidth={1.9} />
      <span className="truncate">{label}</span>
    </Link>
  );
}

function QuickActions({ latestSessionId }: { latestSessionId: string | null }) {
  return (
    <section aria-label="Quick actions">
      <SectionHeader label="Quick actions" />
      <div className="grid grid-cols-2 gap-2 px-4">
        <ActionTile to="/sessions" label="New session" Icon={Plus} primary />
        <ActionTile
          to={latestSessionId ? `/sessions/${latestSessionId}` : "/sessions"}
          label="Resume latest"
          Icon={Activity}
        />
        <ActionTile to="/work" label="Dispatch work" Icon={Briefcase} />
        <ActionTile to="/cost" label="Costs" Icon={HandCoins} />
      </div>
    </section>
  );
}

function AttentionGlance({
  offlineNodes,
  approvals,
  attentionSessions,
}: {
  offlineNodes: number;
  approvals: number;
  attentionSessions: number;
}) {
  const items: Array<{ label: string; to: string }> = [];
  if (approvals > 0)
    items.push({ label: `${approvals} approval${approvals > 1 ? "s" : ""} waiting`, to: "/work" });
  if (attentionSessions > 0)
    items.push({
      label: `${attentionSessions} session${attentionSessions > 1 ? "s" : ""} need attention`,
      to: "/sessions",
    });
  if (offlineNodes > 0)
    items.push({ label: `${offlineNodes} node${offlineNodes > 1 ? "s" : ""} offline`, to: "/system" });

  if (items.length === 0) {
    return (
      <div className="mx-4 mt-3 flex items-center gap-2 rounded-2xl border border-hairline bg-surface-1 px-4 py-3">
        <CheckCircle2 className="size-[18px] shrink-0 text-ok" strokeWidth={2} />
        <span className="text-sm text-ink-soft">Everything healthy — nothing needs you.</span>
      </div>
    );
  }
  return (
    <div className="mx-4 mt-3 overflow-hidden rounded-2xl border border-warn/30 bg-warm-dim/40">
      <div className="flex items-center gap-2 px-4 pt-3 text-warn">
        <AlertTriangle className="size-[18px] shrink-0" strokeWidth={2} />
        <span className="text-[13px] font-semibold">Needs attention</span>
      </div>
      <ul className="px-2 pb-2 pt-1">
        {items.map((it) => (
          <li key={it.label}>
            <Link
              to={it.to}
              className="flex min-h-touch items-center justify-between rounded-xl px-2.5 py-2 text-sm text-ink hover:bg-surface-2"
            >
              <span>{it.label}</span>
              <ChevronRight className="size-4 shrink-0 text-ink-muted" />
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

function NodeCard({ node }: { node: Target }) {
  const slots =
    node.slotsTotal != null ? `${node.slotsUsed ?? 0}/${node.slotsTotal} slots` : null;
  return (
    <div className="rounded-2xl border border-hairline bg-surface-1 p-4">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{node.id}</span>
        <HealthChip health={node.health} />
      </div>
      <div className="mt-1 text-[11px] text-ink-muted">
        heartbeat {heartbeatAge(node.heartbeatAgeSec)}
        {slots ? ` · ${slots}` : ""}
        {node.activeTasks.length > 0 ? ` · ${node.activeTasks.length} active` : ""}
      </div>
      {node.backends.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {node.backends.map((b) => (
            <span
              key={b}
              className="inline-flex items-center gap-1.5 rounded-lg bg-surface-2 py-1 pl-1.5 pr-2.5 text-[11px] text-ink-soft"
            >
              <ProviderBadge provider={b} />
              {b}
            </span>
          ))}
        </div>
      ) : (
        <div className="mt-3 text-[11px] text-ink-muted">no backends reported</div>
      )}
    </div>
  );
}

function Meter({ window: w }: { window: UsageWindow }) {
  const pct = w.usedPercent;
  const known = pct != null;
  return (
    <div className="flex items-center gap-2.5">
      <span className="w-8 shrink-0 text-[11px] font-medium text-ink-muted">{w.label}</span>
      <div
        className="h-2 flex-1 overflow-hidden rounded-full bg-surface-3"
        role="progressbar"
        aria-valuenow={known ? Math.round(pct) : undefined}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${w.label} usage`}
      >
        {known ? (
          <div
            className={cn("h-full rounded-full", w.nearLimit ? "bg-warn" : "bg-accent")}
            style={{ width: `${pct}%` }}
          />
        ) : null}
      </div>
      <span
        className={cn(
          "w-20 shrink-0 text-right text-[11px] tabular-nums",
          !known ? "text-ink-muted" : w.nearLimit ? "font-semibold text-warn" : "text-ink-soft",
        )}
      >
        {known ? `${Math.round(pct)}%${w.nearLimit ? " · near" : ""}` : "unknown"}
      </span>
    </div>
  );
}

function UsageSection({
  providers,
  enabled,
}: {
  providers: ProviderUsage[];
  enabled: boolean;
}) {
  return (
    <section aria-label="Accounts and usage">
      <SectionHeader label="Accounts & usage" count={providers.length || undefined} />
      <div className="space-y-2 px-4">
        {!enabled || providers.length === 0 ? (
          <div className="rounded-2xl border border-hairline bg-surface-1 px-4 py-3 text-[13px] text-ink-muted">
            {enabled
              ? "No usage telemetry reported yet by any provider."
              : "Usage telemetry is off — meters are unavailable."}
          </div>
        ) : (
          providers.map((p) => (
            <div key={p.provider} className="rounded-2xl border border-hairline bg-surface-1 p-4">
              <div className="flex items-center gap-2">
                <ProviderBadge provider={p.provider} />
                <span className="flex-1 truncate text-sm font-medium capitalize text-ink">
                  {p.provider}
                </span>
                {p.accountHint ? (
                  <span className="font-mono text-[10px] text-ink-muted">{p.accountHint}</span>
                ) : null}
              </div>
              <div className="mt-3 space-y-2">
                {p.windows.map((w) => (
                  <Meter key={w.label + w.durationSeconds} window={w} />
                ))}
              </div>
            </div>
          ))
        )}
        <p className="px-1 pt-1 text-[10px] leading-relaxed text-ink-muted">
          Login / credential health per node is not yet monitored — coming with the unified
          backend-auth read model.
        </p>
      </div>
    </section>
  );
}

export function HomeScreen() {
  const targets = useTargets();
  const quota = useQuotaWindows();
  const sessions = useSessions();
  const approvals = useApprovals("pending");

  const nodes = targets.data ?? [];
  const offlineNodes = nodes.filter((n) => n.health === "offline").length;
  const attentionSessions = (sessions.data ?? []).filter((s) => s.needsAttention).length;
  const latestSessionId = (sessions.data ?? [])[0]?.id ?? null;
  const providers = summarizeProviderUsage(quota.data);
  const quotaEnabled = Boolean(quota.data?.enabled);

  return (
    <div className="pb-8">
      <HomeHeader />

      <QuickActions latestSessionId={latestSessionId} />

      <AttentionGlance
        offlineNodes={offlineNodes}
        approvals={approvals.data?.length ?? 0}
        attentionSessions={attentionSessions}
      />

      <section aria-label="Nodes">
        <SectionHeader label="Nodes" count={nodes.length || undefined} />
        <div className="space-y-2 px-4">
          {targets.isLoading ? (
            <div className="rounded-2xl border border-hairline bg-surface-1 px-4 py-6 text-center text-[13px] text-ink-muted">
              Loading nodes…
            </div>
          ) : targets.isError ? (
            <div className="rounded-2xl border border-bad/30 bg-bad/5 px-4 py-3 text-[13px] text-bad">
              Couldn't load nodes — retrying.
            </div>
          ) : nodes.length === 0 ? (
            <div className="rounded-2xl border border-hairline bg-surface-1 px-4 py-6 text-center text-[13px] text-ink-muted">
              No nodes registered yet.
            </div>
          ) : (
            nodes.map((n) => <NodeCard key={n.id} node={n} />)
          )}
        </div>
      </section>

      <UsageSection providers={providers} enabled={quotaEnabled} />
    </div>
  );
}
