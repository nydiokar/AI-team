/**
 * Live agent activity label — what is the agent doing RIGHT NOW?
 *
 * Subscribes to the app-wide SSE stream (no new connection) and returns the
 * most recent `task.activity` label for the given session + task pair. Returns
 * null when no activity has been seen yet (pill falls back to "Working…").
 *
 * Scoped to (sessionId, taskId) so activity events from prior turns in the
 * same session are automatically ignored once lastTaskId advances.
 */
import { useMemo } from "react";
import { useEventStreamContext } from "./eventStreamContext";

export function useTaskActivity(
  sessionId: string | undefined,
  taskId: string | undefined,
): string | null {
  const { events } = useEventStreamContext();

  return useMemo(() => {
    if (!sessionId || !taskId) return null;
    // Walk newest-first — stop at the first matching event.
    for (let i = events.length - 1; i >= 0; i--) {
      const { event } = events[i];
      if (
        event.type === "task.activity" &&
        event.sessionId === sessionId &&
        event.taskId === taskId
      ) {
        return event.label;
      }
    }
    return null;
  }, [events, sessionId, taskId]);
}
