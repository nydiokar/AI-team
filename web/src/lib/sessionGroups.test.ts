import { describe, expect, it } from "vitest";
import { groupSessions, matchesSessionQuery } from "./sessionGroups";
import type { Session } from "../domain/models";

function session(over: Partial<Session> & { id: string }): Session {
  return {
    backend: "claude",
    workspace: { path: "/repo", targetId: "node-1" },
    backendSessionId: null,
    lastBackendSessionId: null,
    lifecycle: "closed",
    opState: "idle",
    needsAttention: false,
    model: null,
    effort: null,
    defaultModel: null,
    lastTaskId: null,
    lastSummary: "",
    lastFilesModified: [],
    originChannel: "web",
    originKind: "manual",
    updatedAt: "2026-09-01T00:00:00Z",
    continuedFrom: null,
    keepPinned: false,
    keepNote: "",
    reason: null,
    ...over,
  } as Session;
}

describe("groupSessions", () => {
  it("shows every server-filtered kept session, however stale its updated_at", () => {
    // The regression: this kept session sits far outside any LIMIT window of
    // the unpinned list (rank 311 by updated_at in the real DB). Deriving the
    // Kept group from that window is what made the section show 4 of 5.
    const stale = session({ id: "kept-stale", keepPinned: true, keepNote: "resume here" });
    const groups = groupSessions({ kept: [stale], unpinned: [], query: "" });

    expect(groups.kept.map((s) => s.id)).toEqual(["kept-stale"]);
    expect(groups.attention).toEqual([]);
    expect(groups.open).toEqual([]);
    expect(groups.closed).toEqual([]);
  });

  it("never renders a kept session twice", () => {
    // The server hands back disjoint halves. A kept row present in `unpinned`
    // would be a server bug; groupSessions must not also surface it in `kept`.
    const groups = groupSessions({
      kept: [session({ id: "k1", keepPinned: true })],
      unpinned: [session({ id: "u1" })],
      query: "",
    });

    const ids = [...groups.kept, ...groups.attention, ...groups.open, ...groups.closed].map(
      (s) => s.id,
    );
    expect(ids).toHaveLength(new Set(ids).size);
    expect(groups.kept.map((s) => s.id)).toEqual(["k1"]);
  });

  it("does not scan the unpinned list for keepPinned", () => {
    // `unpinned` is authoritative: a row in it stays in the normal groups even
    // if its flag says kept. Keeps the pin a single server-side pathway.
    const groups = groupSessions({
      kept: [],
      unpinned: [session({ id: "flagged", keepPinned: true })],
      query: "",
    });

    expect(groups.kept).toEqual([]);
    expect(groups.closed.map((s) => s.id)).toEqual(["flagged"]);
  });

  it("keeps kept sessions separate from the normal state groups", () => {
    const groups = groupSessions({
      kept: [
        session({ id: "k-busy", lifecycle: "open", needsAttention: true }),
        session({ id: "k-idle", lifecycle: "open" }),
        session({ id: "k-closed" }),
      ],
      unpinned: [
        session({ id: "u-busy", lifecycle: "open", needsAttention: true }),
        session({ id: "u-idle", lifecycle: "open" }),
        session({ id: "u-closed" }),
      ],
      query: "",
    });

    expect(groups.kept.map((s) => s.id)).toEqual(["k-busy", "k-idle", "k-closed"]);
    expect(groups.attention.map((s) => s.id)).toEqual(["u-busy"]);
    expect(groups.open.map((s) => s.id)).toEqual(["u-idle"]);
    expect(groups.closed.map((s) => s.id)).toEqual(["u-closed"]);
  });

  it("keeps the collapsed Kept section findable by its note", () => {
    const groups = groupSessions({
      kept: [session({ id: "k1", keepPinned: true, keepNote: "queue messaging" })],
      unpinned: [],
      query: "queue mess",
    });

    expect(groups.kept.map((s) => s.id)).toEqual(["k1"]);
  });
});

describe("matchesSessionQuery", () => {
  const s = session({ id: "abc123", keepNote: "queue messaging" });

  it("matches on the keep note and ignores case", () => {
    expect(matchesSessionQuery(s, "QUEUE")).toBe(true);
    expect(matchesSessionQuery(s, "queue messaging")).toBe(true);
  });

  it("matches everything on an empty query", () => {
    expect(matchesSessionQuery(s, "   ")).toBe(true);
  });

  it("rejects a non-match", () => {
    expect(matchesSessionQuery(s, "zzz")).toBe(false);
  });
});
