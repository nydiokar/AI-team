/**
 * Real session timeline (UI-2) — un-mocks SessionDetailScreen within what the
 * backend actually emits (gap-doc §6: whole-turn, no message/streaming events).
 *
 * Three real sources, merged into the existing TimelineItem[] the SessionTimeline
 * component already renders:
 *   1. Optimistic USER messages   — useSentStore (what you typed; no backend echo).
 *   2. Live NOTICES + TASK-STATE  — this session's events from the SSE stream,
 *      already canonical (eventAdapter): system.notice → notice card,
 *      task.state_changed → task_state card, run.cancelled → notice.
 *   3. Assistant TURN SUMMARY     — the polled session's lastSummary, surfaced as
 *      a single whole-message assistant bubble (no streaming, no per-token).
 *
 * Items are sorted by timestamp so the three streams interleave correctly.
 */
import { useMemo } from "react";
import type { TimelineItem } from "../fixtures/timeline";
import type { Session } from "../domain/models";
import type { StampedEvent } from "./useEventStream";
import { useSentStore } from "../stores/sentStore";

export function useSessionTimeline(
  sessionId: string | undefined,
  session: Session | undefined,
  events: StampedEvent[],
): TimelineItem[] {
  const sent = useSentStore((s) =>
    sessionId ? s.bySession[sessionId] : undefined,
  );

  return useMemo(() => {
    if (!sessionId) return [];
    const items: TimelineItem[] = [];

    // 1 — optimistic user messages
    for (const m of sent ?? []) {
      items.push({
        kind: "message",
        at: m.createdAt,
        message: {
          id: m.id,
          sessionId,
          role: "user",
          text: m.text,
          createdAt: m.createdAt,
        },
      });
    }

    // 2 — this session's live events (notices / task-state / cancellations)
    for (const ev of events) {
      const e = ev.event;
      if (e.type === "system.notice") {
        if (e.notice.sessionId && e.notice.sessionId !== sessionId) continue;
        // task-correlated notices with no session id still belong if they match
        // the session's last task — but without that link we keep session-scoped
        // notices only, to avoid leaking other sessions' operational chatter.
        if (!e.notice.sessionId) continue;
        items.push({ kind: "notice", at: ev.at, notice: e.notice });
      } else if (e.type === "task.state_changed") {
        if (session?.lastTaskId && e.taskId !== session.lastTaskId) continue;
        items.push({
          kind: "task_state",
          at: ev.at,
          taskId: e.taskId,
          state: e.state,
          objective: session?.lastSummary || "Task update",
        });
      } else if (e.type === "run.cancelled") {
        if (session?.lastTaskId && e.runId !== session.lastTaskId) continue;
        items.push({
          kind: "notice",
          at: ev.at,
          notice: {
            id: `cancel-${e.runId}`,
            sessionId,
            taskId: e.runId,
            kind: "run_cancelled",
            text: "Run cancelled",
            severity: "warning",
            timestamp: ev.at,
          },
        });
      }
    }

    // 3 — assistant turn summary (whole-message). Only when idle/done and a
    //     summary exists; while running we let the live task_state card carry it.
    if (
      session?.lastSummary &&
      session.opState !== "running" &&
      session.opState !== "waiting_for_input"
    ) {
      items.push({
        kind: "message",
        at: session.updatedAt,
        message: {
          id: `summary-${session.lastTaskId ?? session.updatedAt}`,
          sessionId,
          role: "assistant",
          text: session.lastSummary,
          createdAt: session.updatedAt,
        },
      });
    }

    items.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
    return items;
  }, [sessionId, session, events, sent]);
}
