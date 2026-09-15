/**
 * Refresh policy (A81) — the app is EVENT-DRIVEN: the single app-wide SSE stream
 * (`useEventStream`) invalidates the react-query keys on real events, and a
 * reconnect-resync closes any dropped-event window. These intervals are only the
 * SAFETY NET for the case where an event is missed AND no reconnect fires.
 *
 * ROLLBACK: set SAFETY_NET_MS back to 3_000 to restore pre-A81 aggressive polling
 * on every event-covered hook. Nothing else needs to change — the poll
 * infrastructure is intact; only the interval moved.
 *
 * Query keys with NO covering event (nodes, cache-heartbeats, mesh-health,
 * quota-windows, cost-*, system-alerts, models) keep their OWN, already-gentle
 * polls in their hooks and deliberately do NOT use this constant.
 */
export const SAFETY_NET_MS = 60_000;
