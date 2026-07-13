/**
 * App-wide single SSE connection (UI-2). One EventSource for the whole app —
 * mounting useEventStream per screen would open a socket each. The provider holds
 * the rolling event log + connection state; screens select what they need
 * (SessionDetail filters by session; a connection banner reads `connection`).
 *
 * The two slices live in SEPARATE contexts on purpose: `events` changes on every
 * SSE frame, while `connection` changes rarely. Keeping them apart means a
 * connection-only consumer does not re-render on every event, and the connection
 * value is a bare primitive with stable identity between frames.
 */
import { createContext, useContext, type ReactNode } from "react";
import { useEventStream, type StampedEvent } from "./useEventStream";
import type { ConnectionState } from "../domain/status";

const EventsContext = createContext<StampedEvent[]>([]);
const ConnectionContext = createContext<ConnectionState>("offline");

export function EventStreamProvider({ children }: { children: ReactNode }) {
  const { events, connection } = useEventStream();
  return (
    <ConnectionContext.Provider value={connection}>
      <EventsContext.Provider value={events}>{children}</EventsContext.Provider>
    </ConnectionContext.Provider>
  );
}

/** The rolling, de-duplicated live event log. Re-renders on every frame. */
export function useLiveEvents(): StampedEvent[] {
  return useContext(EventsContext);
}

/** Transport connection state only — re-renders only when it actually changes. */
export function useConnectionState(): ConnectionState {
  return useContext(ConnectionContext);
}
