/**
 * CaseTimelineView — the case's append-only audit trail (flow_events) in order.
 * Read-only: each row is one authoritative event (type, actor, state
 * transition). The case's linked evidence (sessions/artifacts/…) is surfaced
 * authoritatively in the Ledger section of WorkDetail — it is the SAME
 * flow_links data, so we don't duplicate it here; bulk content stays in the
 * per-entity surfaces (session timelines, artifacts).
 */
import type { CaseTimeline } from "../../domain/work";
import { eventTypeLabel } from "../../lib/workPresentation";

function clock(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function CaseTimelineView({ timeline }: { timeline: CaseTimeline }) {
  if (timeline.events.length === 0) {
    return (
      <p className="rounded-xl bg-surface-1 px-3 py-3 text-[12px] text-ink-muted ring-1 ring-hairline">
        No audit events recorded for this case yet.
      </p>
    );
  }

  return (
    <ol className="relative space-y-3 pl-4">
      {/* Spine */}
      <span className="absolute left-[5px] top-1 bottom-1 w-px bg-hairline" aria-hidden />
      {timeline.events.map((e) => {
        const transition =
          e.fromState || e.toState
            ? `${e.fromState ?? "—"} → ${e.toState ?? "—"}`
            : null;
        return (
          <li key={e.id} className="relative">
            <span className="absolute -left-[13px] top-1.5 size-2 rounded-full bg-accent ring-2 ring-base" />
            <div className="flex items-baseline gap-2">
              <span className="text-[13px] font-medium text-ink-soft">
                {eventTypeLabel(e.eventType)}
              </span>
              {e.actor && (
                <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[10px] text-ink-muted">
                  {e.actor}
                </span>
              )}
              <span className="ml-auto shrink-0 text-[10px] text-ink-muted">
                {clock(e.createdAt)}
              </span>
            </div>
            {transition && (
              <p className="mt-0.5 font-mono text-[11px] text-ink-muted">{transition}</p>
            )}
            {e.entityId && (
              <p className="mt-0.5 truncate font-mono text-[10px] text-ink-muted">
                {e.entityType}:{e.entityId}
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}
