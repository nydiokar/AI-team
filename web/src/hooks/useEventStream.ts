/**
 * Live event transport (UI-2 / Move F+U4) — the SSE half of the live gate.
 *
 * Opens an EventSource to `/api/events/stream` (control_api.api_events_stream),
 * which tails the shared event spine and pushes `data: {events:[...], offset:N}`
 * frames. We adapt each raw event through the canonical eventAdapter (so the rest
 * of the app only ever sees dotted GatewayEvents) and keep a bounded rolling log.
 *
 * Two gate requirements live here:
 *   • "live transport"     — EventSource replaces the 3s poll for the event log.
 *   • "reconnect dedupes"  — every backend event has a stable identity (the
 *     adapter-independent raw key); on reconnect the stream re-emits the tail
 *     from the last offset, so the same raw event can arrive twice. We drop
 *     duplicates by that key (spec §9 reconcile, the `reconnect` fixture's case).
 *
 * EventSource can't set Authorization headers, so the token rides as `?token=`
 * exactly as the backend expects (control_api reads `token` query OR Bearer).
 *
 * Sessions/nodes still poll (useLiveData) — they have NO per-event source; this
 * stream is the event log only.
 */
import { useEffect, useRef, useState, useCallback } from "react";
import { adaptEvent } from "../transport/eventAdapter";
import { rawEventKey, dedupeRawEvents } from "../transport/eventDedupe";
import type { GatewayEvent } from "../domain/events";
import type { RawEvent } from "../transport/rawApi";
import { useAuthStore } from "../stores/authStore";
import type { ConnectionState } from "../domain/status";

/** Max events retained in the rolling client log (bounded memory). */
const MAX_EVENTS = 500;

/** A GatewayEvent stamped with the wall-clock time + raw identity it came from. */
export interface StampedEvent {
  /** Stable identity of the SOURCE raw event — the dedupe key across reconnects. */
  rawKey: string;
  at: string;
  event: GatewayEvent;
}

interface StreamState {
  events: StampedEvent[];
  connection: ConnectionState;
}

/**
 * Subscribe to the live event stream. Returns the rolling, de-duplicated,
 * canonical event log plus a transport-level connection state (spec §9.1).
 * Auto-reconnects with backoff; surfaces `reconnecting` while down so the UI can
 * show the "showing last known state" banner.
 */
export function useEventStream(): StreamState {
  const token = useAuthStore((s) => s.token);
  const [events, setEvents] = useState<StampedEvent[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("offline");

  // Dedupe set + ordering, kept in a ref so reconnects don't reset it.
  const seen = useRef<Set<string>>(new Set());
  const esRef = useRef<EventSource | null>(null);
  const retryRef = useRef<number>(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const ingest = useCallback((raws: RawEvent[]) => {
    // reconnect-replay dedupe (pure, tested in eventDedupe.test.ts)
    const novel = dedupeRawEvents(raws, seen.current);
    const fresh: StampedEvent[] = [];
    for (const raw of novel) {
      const adapted = adaptEvent(raw);
      if (!adapted) continue; // swallowed (heartbeat)
      fresh.push({
        rawKey: rawEventKey(raw),
        at: raw.timestamp ?? new Date().toISOString(),
        event: adapted,
      });
    }
    if (fresh.length === 0) return;
    setEvents((prev) => {
      const next = [...prev, ...fresh];
      // Bound memory + keep the dedupe set from growing unbounded with the log.
      if (next.length > MAX_EVENTS) {
        const dropped = next.splice(0, next.length - MAX_EVENTS);
        for (const d of dropped) seen.current.delete(d.rawKey);
      }
      return next;
    });
  }, []);

  useEffect(() => {
    if (!token) {
      setConnection("offline");
      return;
    }
    let closed = false;

    // The backend streams a frame every ~1s (events OR a keep-alive comment), so
    // an open EventSource pokes the radio every second — a real mobile-battery
    // cost. The poll layer already pauses when the tab is hidden
    // (refetchIntervalInBackground defaults false) and the authoritative read
    // models refetch on focus (refetchOnWindowFocus), while THIS log is a rolling
    // display feed, not state authority. So we drop the socket while hidden and
    // reopen on return — a fresh connect replays the recent tail (since=0) and the
    // dedupe set drops what we already have, so no visible event is lost.
    const isHidden = () =>
      typeof document !== "undefined" && document.visibilityState === "hidden";

    const clearTimer = () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };

    const teardown = () => {
      clearTimer();
      esRef.current?.close();
      esRef.current = null;
    };

    const connect = () => {
      if (closed || isHidden() || esRef.current) return;
      const url = `/api/events/stream?token=${encodeURIComponent(token)}`;
      const es = new EventSource(url);
      esRef.current = es;

      es.onopen = () => {
        retryRef.current = 0;
        setConnection("online");
      };
      es.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data) as { events?: RawEvent[] };
          if (Array.isArray(data.events)) ingest(data.events);
        } catch {
          /* keep-alive comment or malformed frame — ignore */
        }
      };
      es.onerror = () => {
        // EventSource auto-retries, but we want explicit backoff + banner state.
        es.close();
        esRef.current = null;
        if (closed || isHidden()) return;
        setConnection("reconnecting");
        const delay = Math.min(1000 * 2 ** retryRef.current, 15000);
        retryRef.current += 1;
        timerRef.current = setTimeout(connect, delay);
      };
    };

    const onVisibility = () => {
      if (closed) return;
      if (isHidden()) {
        // Going background: release the socket entirely.
        teardown();
        setConnection("offline");
      } else {
        // Returning to foreground: reconnect immediately (retry from zero).
        clearTimer();
        retryRef.current = 0;
        connect();
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    connect();

    return () => {
      closed = true;
      document.removeEventListener("visibilitychange", onVisibility);
      teardown();
    };
  }, [token, ingest]);

  return { events, connection };
}
