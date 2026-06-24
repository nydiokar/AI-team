/**
 * Live activity log (UI-5) — a SELECTOR over the app's existing SSE stream.
 *
 * Reads the one app-wide event stream (useEventStreamContext, opened in UI-2) and
 * projects it to newest-first LogLines. It does NOT open an EventSource — a second
 * one would double-connect, the precise thing eventStreamContext exists to prevent.
 * Sessions/nodes still poll; this is the operational event feed only.
 */
import { useMemo } from "react";
import { useEventStreamContext } from "./eventStreamContext";
import { toLogLines, type LogLine, type LogSeverity } from "../transport/eventLog";
import type { ConnectionState } from "../domain/status";

export interface ActivityFilter {
  sessionId?: string;
  /** Only show lines at or above attention (warning|error). */
  attentionOnly?: boolean;
}

const ATTENTION: ReadonlySet<LogSeverity> = new Set<LogSeverity>(["warning", "error"]);

export interface ActivityLog {
  lines: LogLine[];
  connection: ConnectionState;
}

export function useActivityLog(filter: ActivityFilter = {}): ActivityLog {
  const { events, connection } = useEventStreamContext();
  const { sessionId, attentionOnly } = filter;

  const lines = useMemo(() => {
    let out = toLogLines(events);
    if (sessionId) out = out.filter((l) => l.sessionId === sessionId);
    if (attentionOnly) out = out.filter((l) => ATTENTION.has(l.severity));
    // The stream is appended oldest→newest; the log reads newest-first.
    return out.reverse();
  }, [events, sessionId, attentionOnly]);

  return { lines, connection };
}
