/**
 * App-wide single SSE connection (UI-2). One EventSource for the whole app —
 * mounting useEventStream per screen would open a socket each. The provider holds
 * the rolling event log + connection state; screens select what they need
 * (SessionDetail filters by session; a connection banner reads `connection`).
 */
import { createContext, useContext, type ReactNode } from "react";
import { useEventStream, type StampedEvent } from "./useEventStream";
import type { ConnectionState } from "../domain/status";

interface EventStreamValue {
  events: StampedEvent[];
  connection: ConnectionState;
}

const EventStreamContext = createContext<EventStreamValue>({
  events: [],
  connection: "offline",
});

export function EventStreamProvider({ children }: { children: ReactNode }) {
  const value = useEventStream();
  return (
    <EventStreamContext.Provider value={value}>
      {children}
    </EventStreamContext.Provider>
  );
}

export function useEventStreamContext(): EventStreamValue {
  return useContext(EventStreamContext);
}
