/**
 * Sessions-page grouping.
 *
 * The kept/rest split is deliberately NOT derived here from a `keepPinned`
 * flag. Both halves arrive already filtered by the server
 * (`GET /api/sessions?keep_pinned=true|false`), for one reason: the unfiltered
 * list is a `LIMIT` window ordered by `updated_at`, so deriving the Kept group
 * from it silently dropped any kept session that hadn't been touched recently.
 * The Kept section then under-counted the server (e.g. 4 shown vs 5 pinned)
 * while a server-filtered kept list showed the truth.
 *
 * Two rules follow, and they are the whole point of this module:
 *   1. `kept` is rendered ONLY from the server-filtered kept list, so stale
 *      kept sessions do not fall outside the unpinned list's LIMIT window.
 *   2. `unpinned` is never scanned for `keepPinned` — the server already removed
 *      kept rows, so a kept session cannot render twice either.
 */
import type { Session } from "../domain/models";

export interface SessionGroups {
  /** Kept sessions (server-filtered). */
  kept: Session[];
  /** Open + a human must look at it. */
  attention: Session[];
  /** Open + running/idle. */
  open: Session[];
  closed: Session[];
}

export function matchesSessionQuery(session: Session, query: string): boolean {
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

/**
 * @param kept      server-filtered `keep_pinned=true` list
 * @param unpinned  server-filtered `keep_pinned=false` list
 */
export function groupSessions({
  kept,
  unpinned,
  query,
}: {
  kept: Session[];
  unpinned: Session[];
  query: string;
}): SessionGroups {
  const rest = unpinned.filter((s) => matchesSessionQuery(s, query));
  return {
    kept: kept.filter((s) => matchesSessionQuery(s, query)),
    attention: rest.filter((s) => s.lifecycle === "open" && s.needsAttention),
    open: rest.filter((s) => s.lifecycle === "open" && !s.needsAttention),
    closed: rest.filter((s) => s.lifecycle === "closed"),
  };
}
