/**
 * Reconnect-dedupe identity (UI-2 gate: "reconnect dedupes") — extracted as a
 * pure function so it is unit-testable without the EventSource/DOM harness (same
 * reason the backend extracted `event_stream_frames`).
 *
 * The backend assigns no id to an event, so we derive a stable one from the
 * fields that make a turn-event unique. On reconnect the SSE stream re-emits the
 * tail from the last offset, so a byte-identical event can arrive twice; both map
 * to the same key and the second is dropped. Distinct events never collide
 * (different name / correlation / timestamp).
 */
import type { RawEvent } from "./rawApi";

export function rawEventKey(ev: RawEvent): string {
  return [
    ev.event,
    ev.timestamp ?? "",
    (ev.task_id as string) ?? "",
    (ev.session_id as string) ?? "",
    (ev.node_id as string) ?? "",
  ].join("|");
}

/**
 * Fold a batch of raw events against a running `seen` set, returning only the
 * not-yet-seen ones (in order). Mutates `seen`. This is the core of the stream
 * hook's ingest; tested directly.
 */
export function dedupeRawEvents(raws: RawEvent[], seen: Set<string>): RawEvent[] {
  const fresh: RawEvent[] = [];
  for (const ev of raws) {
    const key = rawEventKey(ev);
    if (seen.has(key)) continue;
    seen.add(key);
    fresh.push(ev);
  }
  return fresh;
}
