import { useMemo, useState } from "react";
import { ChevronDown, Inbox, Pin, Plus, Search, X } from "lucide-react";
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

function sessionMatches(session: Session, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [
    session.id,
    session.backend,
    session.workspace.path,
    session.workspace.targetId,
    session.lastSummary,
    session.keepNote,
    session.model ?? "",
    session.defaultModel ?? "",
  ].some((value) => value.toLowerCase().includes(q));
}

export function SessionsScreen() {
  const [closedExpanded, setClosedExpanded] = useState(false);
  const [keptExpanded, setKeptExpanded] = useState(true);
  const [newOpen, setNewOpen] = useState(false);
  const [keptOnly, setKeptOnly] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const { data, isLoading, error } = useSessions(keptOnly ? true : undefined);
  // Authoritative session→case affiliation labels (empty until the Work
  // substrate records links; never inferred). Absent ⇒ session shows standalone.
  const { index: affiliations } = useSessionAffiliations();

  const groups = useMemo(() => {
    const all = (data ?? []).filter((s) => (!keptOnly || s.keepPinned) && sessionMatches(s, query));
    // Kept sessions surface in their own top section (messaging-app pinned
    // convention) and are pulled out of the normal groups so they never appear
    // twice. In the exhaustive kept-only view everything is kept, so there's no
    // separate section — the whole list is the kept set.
    const kept = keptOnly ? [] : all.filter((s) => s.keepPinned);
    const rest = keptOnly ? all : all.filter((s) => !s.keepPinned);
    return {
      kept,
      attention: rest.filter((s) => s.lifecycle === "open" && s.needsAttention),
      open: rest.filter((s) => s.lifecycle === "open" && !s.needsAttention),
      closed: rest.filter((s) => s.lifecycle === "closed"),
    };
  }, [data, keptOnly, query]);

  const empty = !isLoading && !error && (data ?? []).length === 0;
  const filteredEmpty =
    !isLoading &&
    !error &&
    !empty &&
    groups.kept.length + groups.attention.length + groups.open.length + groups.closed.length === 0;

  return (
    <div className="pb-8">
      <CompactTopBar
        title="Sessions"
        subtitle="Live · persistent context"
        right={
          <div className="flex items-center gap-1">
            {/* Search is on-demand — a taller hit target only when you want it,
                not a permanent row. */}
            <button
              onClick={() => {
                setSearchOpen((v) => {
                  if (v) setQuery("");
                  return !v;
                });
              }}
              aria-label={searchOpen ? "Close search" : "Search sessions"}
              aria-pressed={searchOpen}
              className={cn(
                "flex size-9 items-center justify-center rounded-full transition-colors",
                searchOpen ? "bg-surface-2 text-ink" : "text-ink-muted hover:bg-surface-2 hover:text-ink",
              )}
            >
              {searchOpen ? <X className="size-5" /> : <Search className="size-5" />}
            </button>
            {/* Kept-only filter, folded into an icon toggle. */}
            <button
              onClick={() => setKeptOnly((v) => !v)}
              aria-label={keptOnly ? "Show all sessions" : "Show kept only"}
              aria-pressed={keptOnly}
              title={keptOnly ? "Showing kept only" : "Kept only"}
              className={cn(
                "flex size-9 items-center justify-center rounded-full transition-colors",
                keptOnly ? "bg-accent-dim/60 text-accent ring-1 ring-accent/30" : "text-ink-muted hover:bg-surface-2 hover:text-ink",
              )}
            >
              <Pin className={cn("size-[18px]", keptOnly && "fill-current")} />
            </button>
            <button
              onClick={() => setNewOpen(true)}
              className="flex size-9 items-center justify-center rounded-full bg-accent-dim/60 text-accent ring-1 ring-accent/30 hover:bg-accent-dim"
              aria-label="New session"
            >
              <Plus className="size-5" />
            </button>
          </div>
        }
      />

      {newOpen && <NewSessionSheet onClose={() => setNewOpen(false)} />}

      {searchOpen && !isLoading && !error && !empty && (
        <div className="px-4 pt-3">
          <div className="flex items-center gap-2 rounded-xl border border-hairline bg-surface-1 px-3 py-2">
            <Search className="size-4 shrink-0 text-ink-muted" />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.currentTarget.value)}
              placeholder={keptOnly ? "Search kept notes and sessions" : "Search sessions"}
              className="min-w-0 flex-1 bg-transparent text-[14px] text-ink outline-none placeholder:text-ink-muted"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                aria-label="Clear search"
                className="shrink-0 text-ink-muted hover:text-ink"
              >
                <X className="size-4" />
              </button>
            )}
          </div>
        </div>
      )}

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
          {groups.kept.length > 0 && (
            <>
              <SectionHeader
                label="Kept"
                count={groups.kept.length}
                action={
                  <button
                    onClick={() => setKeptExpanded((v) => !v)}
                    aria-expanded={keptExpanded}
                    className="flex items-center gap-1 text-[11px] text-ink-muted hover:text-ink-soft"
                  >
                    {keptExpanded ? "Hide" : "Show"}
                    <ChevronDown
                      className={cn("size-3.5 transition-transform", keptExpanded && "rotate-180")}
                    />
                  </button>
                }
              />
              {keptExpanded && (
                <CardList sessions={groups.kept} affiliations={affiliations} />
              )}
            </>
          )}

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
              {(closedExpanded || keptOnly) && (
                <CardList sessions={groups.closed} affiliations={affiliations} />
              )}
            </>
          )}
        </>
      )}

      {filteredEmpty && (
        <div className="flex flex-col items-center gap-3 px-4 py-16 text-center">
          <div className="flex size-14 items-center justify-center rounded-2xl bg-surface-1 ring-1 ring-hairline">
            <Search className="size-7 text-ink-muted" />
          </div>
          <div>
            <p className="text-[15px] font-medium text-ink-soft">No matching sessions</p>
            <p className="mt-1 text-sm text-ink-muted">Try another search or clear the kept-only filter.</p>
          </div>
        </div>
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
