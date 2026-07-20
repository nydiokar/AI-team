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

  it("clamps to the client cap and marks truncation", () => {
    const huge = "z".repeat(FORK_DIGEST_MAX_CHARS + 500);
    const digest = buildForkDigest([{ id: "x", role: "user", text: huge }]);
    expect(digest.length).toBeLessThanOrEqual(FORK_DIGEST_MAX_CHARS + "\n…(truncated)".length);
    expect(digest.endsWith("…(truncated)")).toBe(true);
  });
});
