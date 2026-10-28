import { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, ChevronDown, ChevronRight, Cpu } from "lucide-react";
import type { Target, NodeActivityState } from "../../domain/models";
import { cn } from "../../lib/cn";
import { NodeAvatar } from "./NodeAvatar";
import { BackendLogo } from "./BackendLogo";

const states: NodeActivityState[] = ["running", "queued", "waiting", "failed"];
const labels: Record<NodeActivityState, string> = { running: "Running", queued: "Queued", waiting: "Waiting", failed: "Failed", idle: "Idle" };
const colors: Record<NodeActivityState, string> = { running: "text-running", queued: "text-sky-400", waiting: "text-warn", failed: "text-bad", idle: "text-ink-muted" };
const bars: Record<NodeActivityState, string> = { running: "bg-running", queued: "bg-sky-400", waiting: "bg-warn", failed: "bg-bad", idle: "bg-ink-muted/25" };
const priority: Record<NodeActivityState, number> = { failed: 0, running: 1, queued: 2, waiting: 3, idle: 4 };
function ageLabel(age: number): string {
  if (age < 90) return `${Math.round(age)}s`;
  if (age < 3600) return `${Math.round(age / 60)}m`;
  return `${Math.round(age / 3600)}h`;
}

function NodeCard({ node }: { node: Target }) {
  const [selected, setSelected] = useState<NodeActivityState | null>(null);
  const activity = node.activity;
  const counts = activity?.counts;
  const total = counts ? Object.values(counts).reduce((a, b) => a + b, 0) : 0;
  const sessions = (activity?.sessions ?? []).filter((s) => selected ? s.state === selected : s.state !== "idle").sort((a, b) => priority[a.state] - priority[b.state]).slice(0, 3);
  const headline = node.health !== "online" ? "Offline · last known activity" : !counts ? "Session activity unavailable" : counts.failed > 0 ? `${counts.failed} session${counts.failed === 1 ? "" : "s"} to check` : counts.running > 0 ? "Work is in motion" : counts.queued > 0 ? "Work is lined up" : counts.waiting > 0 ? "Ready for your next move" : "Room for something new";
  const occupied = node.slotsUsed;
  return <article aria-label={`${node.id} activity`} className="overflow-hidden rounded-3xl border border-hairline bg-surface-1 shadow-sm">
    <div className="flex items-center gap-3 px-4 pb-4 pt-4">
      <NodeAvatar id={node.id} health={node.health} />
      <div className="min-w-0 flex-1">
        <h3 className="truncate text-[15px] font-semibold tracking-tight text-ink">{node.id}</h3>
        <p className={cn("mt-0.5 text-xs", counts?.failed ? "text-bad" : "text-ink-muted")}>{headline}</p>
      </div>
      <span className="shrink-0 text-right text-[10px] text-ink-muted"><span className="block text-[22px] font-medium leading-none tabular-nums text-ink-soft">{counts ? total : "—"}</span><span className="mt-1 block">open sessions</span></span>
    </div>
    {counts && <>
      <div className="mx-4 flex h-1.5 gap-0.5 overflow-hidden rounded-full bg-surface-3" aria-hidden>
        {[...states, "idle" as const].filter((state) => counts[state] > 0).map((state) => <div key={state} className={bars[state]} style={{ flex: counts[state] }} />)}
      </div>
      <div className="grid grid-cols-4 gap-1 px-3 pb-3 pt-3">
        {states.map((state) => <button key={state} type="button" aria-label={`${counts[state]} ${labels[state].toLowerCase()} sessions on ${node.id}`} aria-pressed={selected === state} onClick={() => setSelected(selected === state ? null : state)} className={cn("min-h-14 rounded-xl px-1 py-1.5 text-left transition-colors hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-accent", selected === state && "bg-surface-2")}>
          <span className={cn("block px-2 text-[21px] font-semibold leading-tight tabular-nums", counts[state] > 0 ? colors[state] : "text-ink-muted")}>{counts[state]}</span>
          <span className="mt-1 flex items-center gap-1 px-2 text-[10px] text-ink-muted">{labels[state]}{counts[state] > 0 && <ChevronRight className="size-2.5" />}</span>
        </button>)}
      </div>
      {sessions.length > 0 ? <div className="border-t border-hairline bg-surface-2/30 px-4 py-1.5">
        {sessions.map((session) => <Link key={session.id} to={`/sessions/${session.id}`} className="group flex min-h-12 items-center gap-2.5 rounded-lg py-2 focus-visible:outline-2 focus-visible:outline-accent">
          <BackendLogo backend={session.backend} size={6} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-xs text-ink-soft">{session.summary || session.repoPath.split("/").filter(Boolean).at(-1) || session.id}</div>
            <div className="mt-0.5 flex gap-1.5 text-[10px] text-ink-muted"><span className={colors[session.state]}>{labels[session.state]}</span><span>· {session.id.slice(0, 8)}</span></div>
          </div>
          <ArrowUpRight className="size-3.5 shrink-0 text-ink-muted group-hover:text-accent" />
        </Link>)}
        {selected && counts[selected] > sessions.length && <p className="pb-2 text-[10px] text-ink-muted">Latest {sessions.length} of {counts[selected]} {labels[selected].toLowerCase()} sessions</p>}
      </div> : selected ? <p className="border-t border-hairline px-4 py-3 text-xs text-ink-muted">No {labels[selected].toLowerCase()} sessions.</p> : null}
    </>}
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline px-4 py-2 text-[10px] text-ink-muted">
      <span className="flex items-center gap-1.5"><Cpu className="size-3" />{occupied != null && node.slotsTotal != null ? `${occupied}/${node.slotsTotal} slots occupied` : `${node.maxConcurrent} session slots`}
        {counts && counts.idle > 0 && <button type="button" aria-pressed={selected === "idle"} onClick={() => setSelected(selected === "idle" ? null : "idle")} className="min-h-8 underline decoration-hairline underline-offset-4">· {counts.idle} idle</button>}
      </span>
      {node.heartbeatAgeSec != null && <span>Updated {ageLabel(node.heartbeatAgeSec)} ago</span>}
    </div>
  </article>;
}

export function NodeActivityCards({ nodes, loading, error }: { nodes: Target[]; loading: boolean; error: boolean }) {
  const [showOffline, setShowOffline] = useState(false);
  const online = nodes.filter((n) => n.health === "online");
  const offline = nodes.filter((n) => n.health !== "online");
  return <section aria-label="Active nodes" className="px-4 pt-5">
    <div className="mb-3 flex items-center justify-between">
      <div><h2 className="text-sm font-semibold tracking-tight text-ink">Your nodes</h2><p className="mt-0.5 text-[11px] text-ink-muted">A live view of work across your machines</p></div>
      {offline.length > 0 && <button type="button" aria-expanded={showOffline} onClick={() => setShowOffline(!showOffline)} className="flex min-h-11 items-center gap-1 text-[11px] text-ink-muted">{offline.length} offline<ChevronDown className={cn("size-3 transition-transform", showOffline && "rotate-180")} /></button>}
    </div>
    {error ? <p role="alert" className="rounded-2xl border border-bad/30 bg-surface-1 p-4 text-sm text-bad">Node activity could not be refreshed. Retrying automatically.</p> : loading ? <div className="h-44 animate-pulse rounded-3xl border border-hairline bg-surface-1" aria-label="Loading node activity" /> : online.length === 0 ? <p className="rounded-2xl border border-hairline bg-surface-1 p-5 text-sm text-ink-muted">No nodes online.</p> : null}
    <div className="grid gap-3 md:grid-cols-2">{online.map((node) => <NodeCard key={node.id} node={node} />)}</div>
    {showOffline && <div className="mt-3 grid gap-3 md:grid-cols-2">{offline.map((node) => <NodeCard key={node.id} node={node} />)}</div>}
  </section>;
}
