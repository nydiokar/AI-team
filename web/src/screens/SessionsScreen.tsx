import { useMemo, useState } from "react";
import { ChevronDown, Inbox, Plus } from "lucide-react";
import { motion } from "framer-motion";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SectionHeader } from "../components/ui/SectionHeader";
import { SessionRow } from "../components/sessions/SessionRow";
import { NewSessionSheet } from "../components/sessions/NewSessionSheet";
import { useSessions } from "../hooks/useLiveData";
import { useSessionAffiliations } from "../hooks/useWork";
import type { Session } from "../domain/models";
import type { SessionAffiliation } from "../domain/work";
import { cn } from "../lib/cn";

function SkeletonCard() {
  return (
    <div className="card-elev animate-pulse rounded-2xl px-4 py-4">
      <div className="flex items-center gap-2.5">
        <div className="h-4 w-32 rounded-md bg-surface-2" />
        <div className="ml-auto h-5 w-16 rounded-full bg-surface-2" />
      </div>
      <div className="mt-2 flex items-center gap-2">
        <div className="h-3.5 w-14 rounded-md bg-surface-2" />
        <div className="h-3 w-20 rounded bg-surface-2" />
      </div>
      <div className="mt-2.5 h-3.5 w-3/4 rounded bg-surface-2" />
    </div>
  );
}

function CardList({
  sessions,
  affiliations,
}: {
  sessions: Session[];
  affiliations: Map<string, SessionAffiliation>;
}) {
  return (
    <div className="desktop-card-list px-4">
      {sessions.map((s, i) => (
        <motion.div
          key={s.id}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, delay: Math.min(i * 0.03, 0.2) }}
        >
          <SessionRow session={s} affiliation={affiliations.get(s.id)} />
        </motion.div>
      ))}
    </div>
  );
}

export function SessionsScreen() {
  const { data, isLoading, error } = useSessions();
  // Authoritative session→case affiliation labels (empty until the Work
  // substrate records links; never inferred). Absent ⇒ session shows standalone.
  const { index: affiliations } = useSessionAffiliations();
  const [closedExpanded, setClosedExpanded] = useState(false);
  const [newOpen, setNewOpen] = useState(false);

  const groups = useMemo(() => {
    const all = data ?? [];
    return {
      attention: all.filter((s) => s.lifecycle === "open" && s.needsAttention),
      open: all.filter((s) => s.lifecycle === "open" && !s.needsAttention),
      closed: all.filter((s) => s.lifecycle === "closed"),
    };
  }, [data]);

  const empty = !isLoading && !error && (data ?? []).length === 0;

  return (
    <div className="pb-8">
      <CompactTopBar
        title="Sessions"
        subtitle="Live · persistent context"
        right={
          <button
            onClick={() => setNewOpen(true)}
            className="flex size-9 items-center justify-center rounded-full bg-accent-dim/60 text-accent ring-1 ring-accent/30 hover:bg-accent-dim"
            aria-label="New session"
          >
            <Plus className="size-5" />
          </button>
        }
      />

      {newOpen && <NewSessionSheet onClose={() => setNewOpen(false)} />}

      {/* Loading skeletons */}
      {isLoading && (
        <div className="space-y-3 px-4 pt-4">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {error && (
        <p className="px-4 py-10 text-center text-sm text-bad">Couldn't load sessions.</p>
      )}

      {!isLoading && !error && (
        <>
          {groups.attention.length > 0 && (
            <>
              <SectionHeader label="Needs attention" count={groups.attention.length} accent="warn" />
              <CardList sessions={groups.attention} affiliations={affiliations} />
            </>
          )}

          {groups.open.length > 0 && (
            <>
              <SectionHeader label="Active" count={groups.open.length} />
              <CardList sessions={groups.open} affiliations={affiliations} />
            </>
          )}

          {groups.closed.length > 0 && (
            <>
              <SectionHeader
                label="Closed"
                count={groups.closed.length}
                action={
                  <button
                    onClick={() => setClosedExpanded((v) => !v)}
                    aria-expanded={closedExpanded}
                    className="flex items-center gap-1 text-[11px] text-ink-muted hover:text-ink-soft"
                  >
                    {closedExpanded ? "Hide" : "Show"}
                    <ChevronDown
                      className={cn("size-3.5 transition-transform", closedExpanded && "rotate-180")}
                    />
                  </button>
                }
              />
              {closedExpanded && (
                <CardList sessions={groups.closed} affiliations={affiliations} />
              )}
            </>
          )}
        </>
      )}

      {empty && (
        <div className="flex flex-col items-center gap-3 px-4 py-20 text-center">
          <div className="flex size-14 items-center justify-center rounded-2xl bg-surface-1 ring-1 ring-hairline">
            <Inbox className="size-7 text-ink-muted" />
          </div>
          <div>
            <p className="text-[15px] font-medium text-ink-soft">No sessions yet</p>
            <p className="mt-1 text-sm text-ink-muted">Start a session to run your first task.</p>
          </div>
          <button
            onClick={() => setNewOpen(true)}
            className="mt-1 rounded-lg bg-accent-dim px-4 py-2 text-sm font-medium text-accent ring-1 ring-accent/30 hover:bg-accent-dim/80"
          >
            + New session
          </button>
        </div>
      )}
    </div>
  );
}
