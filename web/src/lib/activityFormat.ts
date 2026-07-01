/**
 * Activity-feed humanization — HONEST and one-to-one. Every raw event is its
 * own row; we never fold, count, or collapse. What we DO add is identity: the
 * raw stream says "task running" with no idea *which* task, so we join each
 * event against the sessions the app already polls. The result names the work
 * (repo + backend) and yields a real link — turning anonymous status lines into
 * tappable, attributable rows. Events with no resolvable owner (a node
 * connecting) stay honest, non-clickable system lines.
 */
import type { LogLine } from "../transport/eventLog";
import type { Session } from "../domain/models";

export interface EnrichedLine {
  line: LogLine;
  /** Human one-liner, host stripped (e.g. "Codex finished"). */
  title: string;
  /** Humanized kind tag (e.g. "Artifact written"). */
  kind: string;
  /** Hostname the event carried, if any. */
  host: string | null;
  /** Owning session, resolved via sessionId or taskId→lastTaskId. */
  session: Session | null;
  /** Where a tap goes — session route, or null for pure system lines. */
  href: string | null;
}

const HOST_RE = /\s*@([\w.-]+)\s*$/;

function titleCase(s: string): string {
  const c = s.replace(/[_.]+/g, " ").trim();
  return c.charAt(0).toUpperCase() + c.slice(1);
}

/** Last path segment of a repo path, e.g. "/home/me/payments-api" → "payments-api". */
export function repoName(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/** Build a fast lookup so each row resolves in O(1). */
export function indexSessions(sessions: Session[]) {
  const byId = new Map<string, Session>();
  const byTask = new Map<string, Session>();
  for (const s of sessions) {
    byId.set(s.id, s);
    if (s.lastTaskId) byTask.set(s.lastTaskId, s);
  }
  return { byId, byTask };
}

export function enrichLine(
  line: LogLine,
  idx: ReturnType<typeof indexSessions>,
): EnrichedLine {
  let title = line.text.trim();
  let host: string | null = null;
  const m = title.match(HOST_RE);
  if (m) {
    host = m[1];
    title = title.replace(HOST_RE, "").trim();
  }

  const session =
    (line.sessionId ? idx.byId.get(line.sessionId) : undefined) ??
    (line.taskId ? idx.byTask.get(line.taskId) : undefined) ??
    null;

  return {
    line,
    title: titleCase(title),
    kind: titleCase(line.kind),
    host,
    session,
    // Route to the tab where this event's content lives, so the tap is coherent
    // with what the user then sees (an artifact event → Files, not empty Chat).
    href: session ? `/sessions/${session.id}${tabSuffix(line.kind)}` : null,
  };
}

export function isSystemActivity(line: EnrichedLine): boolean {
  return line.session == null;
}

/** File/artifact events live in the Files tab; everything else is conversational. */
function tabSuffix(kind: string): string {
  return /artifact|file/i.test(kind) ? "?tab=files" : "";
}
