import { describe, it, expect } from "vitest";
import { buildForkDigest, FORK_DIGEST_MAX_CHARS, type MarkedMessage } from "./forkDigest";

describe("buildForkDigest", () => {
  it("renders marked messages verbatim in order with role labels", () => {
    const msgs: MarkedMessage[] = [
      { id: "t1-u", role: "user", text: "fix the loader" },
      { id: "t1-a", role: "assistant", text: "it double-frees on retry" },
    ];
    const digest = buildForkDigest(msgs);
    expect(digest).toBe("You: fix the loader\n\nAgent: it double-frees on retry");
  });

  it("trims each message's whitespace", () => {
    const digest = buildForkDigest([{ id: "x", role: "user", text: "  spaced  " }]);
    expect(digest).toBe("You: spaced");
  });

  it("returns empty string for no selection", () => {
    expect(buildForkDigest([])).toBe("");
  });

  it("clamps to the client cap keeping the most recent tail", () => {
    // The tail (recent) must survive; the front is dropped and marked.
    const head = "OLD".repeat(1000);
    const tail = "RECENT-TAIL";
    const huge = head + "z".repeat(FORK_DIGEST_MAX_CHARS) + tail;
    const digest = buildForkDigest([{ id: "x", role: "user", text: huge }]);
    expect(digest.length).toBeLessThanOrEqual(FORK_DIGEST_MAX_CHARS);
    expect(digest.startsWith("…(earlier context truncated)…")).toBe(true);
    expect(digest.endsWith(tail)).toBe(true); // most recent content preserved
    expect(digest.includes("OLD")).toBe(false); // stale head dropped
  });
});
