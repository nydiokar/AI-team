/**
 * Sessions screen — LIVE (UI-1 gate). Binds /api/sessions. Grouping = Needs
 * attention / Open / Closed, derived from is_active + needs_input (gap-doc §3),
 * with target filtering. Attention first, only when non-empty. Closed collapsed.
 */
import { useMemo } from "react";
import { ChevronDown, Inbox } from "lucide-react";
import { motion } from "framer-motion";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { TargetSelector } from "../components/shell/TargetSelector";
import { SectionHeader } from "../components/ui/SectionHeader";
import { SessionRow } from "../components/sessions/SessionRow";
import { useSessions } from "../hooks/useLiveData";
import { useUiStore } from "../stores/uiStore";
import type { Session } from "../domain/models";
import { cn } from "../lib/cn";

function CardList({ sessions }: { sessions: Session[] }) {
  return (
    <div className="space-y-3 px-4">
      {sessions.map((s, i) => (
        <motion.div
          key={s.id}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, delay: Math.min(i * 0.03, 0.2) }}
        >
          <SessionRow session={s} />
        </motion.div>
      ))}
    </div>
  );
}

export function SessionsScreen() {
  const { data, isLoading, error } = useSessions();
  const targetFilter = useUiStore((s) => s.targetFilter);
  const closedExpanded = useUiStore((s) => s.closedExpanded);
  const toggleClosed = useUiStore((s) => s.toggleClosed);

  const groups = useMemo(() => {
    const all = (data ?? []).filter(
      (s) => !targetFilter || s.workspace.targetId === targetFilter,
    );
    return {
      attention: all.filter((s) => s.lifecycle === "open" && s.needsAttention),
      open: all.filter((s) => s.lifecycle === "open" && !s.needsAttention),
      closed: all.filter((s) => s.lifecycle === "closed"),
    };
  }, [data, targetFilter]);

  const empty = !isLoading && !error && (data ?? []).length === 0;

  return (
    <div className="pb-8">
      <CompactTopBar title="Sessions" subtitle="Live · persistent context" />
      <TargetSelector />

      {isLoading && (
        <p className="px-4 py-10 text-center text-sm text-ink-muted">Loading sessions…</p>
      )}
      {error && (
        <p className="px-4 py-10 text-center text-sm text-bad">Couldn't load sessions.</p>
      )}

      {groups.attention.length > 0 && (
        <>
          <SectionHeader label="Needs attention" count={groups.attention.length} accent="warn" />
          <CardList sessions={groups.attention} />
        </>
      )}

      {groups.open.length > 0 && (
        <>
          <SectionHeader label="Open" count={groups.open.length} />
          <CardList sessions={groups.open} />
        </>
      )}

      {groups.closed.length > 0 && (
        <>
          <SectionHeader
            label="Closed"
            count={groups.closed.length}
            action={
              <button
                onClick={toggleClosed}
                aria-expanded={closedExpanded}
                aria-label={closedExpanded ? "Collapse closed sessions" : "Expand closed sessions"}
                className="flex items-center gap-1 text-[11px] text-ink-muted hover:text-ink-soft"
              >
                {closedExpanded ? "Hide" : "Show"}
                <ChevronDown
                  className={cn("size-3.5 transition-transform", closedExpanded && "rotate-180")}
                />
              </button>
            }
          />
          {closedExpanded && <CardList sessions={groups.closed} />}
        </>
      )}

      {empty && (
        <div className="flex flex-col items-center gap-2 px-4 py-16 text-center">
          <Inbox className="size-8 text-ink-muted" />
          <p className="text-sm text-ink-soft">No sessions yet.</p>
          <p className="text-xs text-ink-muted">Start one from Telegram and it appears here.</p>
        </div>
      )}
    </div>
  );
}
