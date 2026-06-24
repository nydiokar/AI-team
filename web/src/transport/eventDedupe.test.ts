import { describe, it, expect } from "vitest";
import { rawEventKey, dedupeRawEvents } from "./eventDedupe";
import type { RawEvent } from "./rawApi";

const ev = (over: Partial<RawEvent>): RawEvent => ({
  event: "task_received",
  timestamp: "2026-06-24T10:00:00Z",
  ...over,
});

describe("eventDedupe — reconnect dedupe (UI-2 gate)", () => {
  it("gives byte-identical events the same key", () => {
    const a = ev({ task_id: "t1" });
    const b = ev({ task_id: "t1" });
    expect(rawEventKey(a)).toBe(rawEventKey(b));
  });

  it("distinguishes events differing in name / correlation / time", () => {
    const base = ev({ task_id: "t1" });
    expect(rawEventKey(base)).not.toBe(rawEventKey(ev({ task_id: "t2" })));
    expect(rawEventKey(base)).not.toBe(rawEventKey({ ...base, event: "validated" }));
    expect(rawEventKey(base)).not.toBe(rawEventKey({ ...base, timestamp: "2026-06-24T10:00:01Z" }));
  });

  it("drops the replayed tail on reconnect, keeps genuinely new events", () => {
    const seen = new Set<string>();
    // first stream batch
    const first = [ev({ task_id: "t1" }), ev({ task_id: "t1", event: "validated" })];
    expect(dedupeRawEvents(first, seen)).toHaveLength(2);

    // reconnect replays the SAME tail plus one new event → only the new survives
    const replay = [
      ev({ task_id: "t1" }), // dup
      ev({ task_id: "t1", event: "validated" }), // dup
      ev({ task_id: "t1", event: "summarized", timestamp: "2026-06-24T10:00:05Z" }), // new
    ];
    const fresh = dedupeRawEvents(replay, seen);
    expect(fresh).toHaveLength(1);
    expect(fresh[0].event).toBe("summarized");
  });
});
