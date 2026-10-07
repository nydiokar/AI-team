/**
 * [A82 Stage 6] UI01–08 (pure half): queue card rules, ledger-derived session
 * state, live invalidation scope, telemetry separation, pending/terminal dedup.
 * Component interaction lives in components/timeline/TurnQueuePanel.test.tsx.
 */
import { describe, expect, it, vi } from "vitest";
import type { QueryClient } from "@tanstack/react-query";
import {
  blockedReasonLabel,
  effectsFailedLabel,
  isEditableTurn,
  pauseReasonLabel,
  previewWords,
  queueOpState,
  queueOwnedIds,
  recoveryOptions,
  senderSummary,
  sessionTurnQueueKey,
  toSessionTurnQueue,
  transcriptFinishedIds,
  turnCardLabel,
  turnPrincipalLabel,
  turnSourceLabel,
  waitingTurnIds,
  TURN_QUEUE_REFETCH_MS,
  TURN_TERMINAL_STATUSES,
} from "./turnQueue";
import { SAFETY_NET_MS } from "./refreshPolicy";
import {
  collectLiveInvalidationTargets,
  invalidateAllLive,
  invalidateLiveTargets,
} from "./liveInvalidation";
import { deriveOpState, toSession } from "../transport/sessionAdapter";
import { deriveTaskState } from "../transport/taskAdapter";
import type { RawSessionView, RawTranscriptTurn } from "../transport/rawApi";

const base: RawSessionView = {
  session_id: "s1",
  backend: "claude",
  repo_path: "/tmp/repo",
  status: "idle",
  machine_id: "node_a",
  backend_session_id: "",
  model: null,
  effort: null,
  default_model: null,
  last_task_id: "",
  last_summary: "",
  last_files_modified: [],
  needs_input: false,
  is_active: true,
  origin_channel: "web",
  origin_kind: "user",
  updated_at: "2026-10-02T00:00:00Z",
};

const queue = (active_status: string | null, queued = 0) => ({
  queued,
  active_turn_id: active_status ? "t-active" : null,
  active_status,
  paused: false,
  hold: null,
});

describe("[A101] pauseReasonLabel — projected hold reasons get human copy", () => {
  it("maps each gate hold to a bounded label", () => {
    expect(pauseReasonLabel("quota")).toContain("quota");
    expect(pauseReasonLabel("transient")).toContain("retrying automatically");
    expect(pauseReasonLabel("retry")).toContain("retry");
    expect(pauseReasonLabel("manager_rebound")).toContain("new Manager");
    expect(pauseReasonLabel("carrier_offline")).toContain("offline");
    expect(pauseReasonLabel("backoff")).toContain("backing off");
    expect(pauseReasonLabel("legacy_draining")).toContain("finishing");
    expect(pauseReasonLabel("lineage")).toContain("lineage");
    expect(pauseReasonLabel("operator_pause")).toBe("Queue paused");
  });

  it("returns null for absent or unknown reasons (caller falls back)", () => {
    expect(pauseReasonLabel(null)).toBeNull();
    expect(pauseReasonLabel(undefined)).toBeNull();
    expect(pauseReasonLabel("")).toBeNull();
    expect(pauseReasonLabel("martian")).toBeNull();
  });
});

describe("UI01 queue card labels — same id moves Waiting → Starting → Working", () => {
  it("is conservative: pending/claimed are not consumption", () => {
    expect(turnCardLabel("queued")).toBe("Waiting");
    expect(turnCardLabel("pending")).toBe("Starting");
    expect(turnCardLabel("claimed")).toBe("Starting");
    expect(turnCardLabel("running")).toBe("Working");
    expect(turnCardLabel("recovery_required")).toBe("Recovery required");
    expect(turnCardLabel("completed")).toBe("Finished");
  });

  it("only a waiting human instruction is editable/withdrawable", () => {
    const t = { status: "queued", turn_source: "human", turn_kind: "instruction" };
    expect(isEditableTurn(t)).toBe(true);
    expect(isEditableTurn({ ...t, status: "pending" })).toBe(false);
    expect(isEditableTurn({ ...t, turn_source: "agent" })).toBe(false);
    expect(isEditableTurn({ ...t, turn_source: "system" })).toBe(false);
    expect(isEditableTurn({ ...t, turn_kind: "compaction" })).toBe(false);
  });

  it("labels server-derived source and bounded blocked reasons", () => {
    expect(turnSourceLabel({ turn_source: "human", turn_kind: "instruction", sender_session_id: null })).toBe("You");
    expect(turnSourceLabel({ turn_source: "agent", turn_kind: "instruction", sender_session_id: "abcdef123456" })).toBe("Agent abcdef12");
    expect(blockedReasonLabel("managed_result_oversize: artifact=/x; node=n")).toBe(
      "Result too large — held for operator review",
    );
    expect(blockedReasonLabel(null)).toBeNull();
    expect(blockedReasonLabel("weird_reason: x")).toBe("Weird reason");
  });

  // [A99 D5] Sender identity is required and must never be a bare
  // "system"/"continuation": always a principal, plus what the turn is when it
  // is not a plain operator instruction.
  it("names a principal AND the turn kind — never a bare system/continuation", () => {
    const S = (turn_source: string | null, turn_kind: string | null, sender: string | null = null) =>
      turnSourceLabel({ turn_source, turn_kind, sender_session_id: sender });
    expect(S("operator", "instruction")).toBe("You");
    expect(S("human", "compaction")).toBe("You · Compaction");
    expect(S("agent", "instruction", "1f9bce3f5a87")).toBe("Agent 1f9bce3f");
    expect(S("agent", "retry", "1f9bce3f5a87")).toBe("Agent 1f9bce3f · Retry");
    expect(S("system", "continuation")).toBe("System · Continuation");
    expect(S(null, null)).toBe("System");
    // The principal alone (tight spaces) drops the kind qualifier.
    expect(turnPrincipalLabel({ turn_source: "system", sender_session_id: null })).toBe("System");
    expect(turnPrincipalLabel({ turn_source: "agent", sender_session_id: "abcdef12ff" })).toBe("Agent abcdef12");
  });
});

describe("[A99] queue adapters — waiting-only, senders, preview, recovery", () => {
  const t = (id: string, status: string, over: Record<string, unknown> = {}) => ({
    id, status, turn_source: "human", sender_session_id: null, preview: "", ...over,
  });

  it("waitingTurnIds keeps only queued turns (active/finished belong to chat)", () => {
    const ids = waitingTurnIds([
      t("a", "queued"), t("b", "running"), t("c", "queued"),
      t("d", "recovery_required"), t("e", "completed"), t("f", "pending"),
    ]);
    expect(ids).toEqual(["a", "c"]);
  });

  it("senderSummary dedupes principals, keeps order, and caps with +N", () => {
    expect(senderSummary([])).toBe("");
    expect(senderSummary([t("a", "queued")])).toBe("You");
    expect(
      senderSummary([
        t("a", "queued"),
        t("b", "queued", { turn_source: "agent", sender_session_id: "1f9bce3f5a" }),
        t("c", "queued"),
      ]),
    ).toBe("You, Agent 1f9bce3f");
    expect(
      senderSummary([
        t("a", "queued"),
        t("b", "queued", { turn_source: "agent", sender_session_id: "aaaaaaaa11" }),
        t("c", "queued", { turn_source: "agent", sender_session_id: "bbbbbbbb22" }),
      ]),
    ).toBe("You, Agent aaaaaaaa +1");
  });

  it("previewWords returns a 1–2 word teaser, never the whole prompt", () => {
    expect(previewWords("Refactor the admission path and add a test")).toBe("Refactor the…");
    expect(previewWords("Deploy", 2)).toBe("Deploy");
    expect(previewWords("one two three", 1)).toBe("one…");
    expect(previewWords("")).toBe("");
    expect(previewWords(null)).toBe("");
  });

  it("recoveryOptions mirrors the backend: claimed⇒requeue(no ack), running/held⇒cancel|fail(ack)", () => {
    const claimed = recoveryOptions("claimed");
    expect(claimed.map((o) => o.decision)).toEqual(["requeue"]);
    expect(claimed[0].requiresAck).toBe(false);

    for (const s of ["running", "recovery_required"]) {
      const opts = recoveryOptions(s);
      expect(opts.map((o) => o.decision)).toEqual(["cancelled", "failed"]);
      expect(opts.every((o) => o.requiresAck)).toBe(true);
      // requeue is never offered after a turn has started (double-execution risk).
      expect(opts.some((o) => o.decision === "requeue")).toBe(false);
    }
    expect(recoveryOptions("queued")).toEqual([]);
    expect(recoveryOptions("completed")).toEqual([]);
  });
});

describe("UI02 session state from the ledger overlay (queued is not BUSY)", () => {
  it("persisted queued count never marks a session running", () => {
    expect(deriveOpState({ ...base, turn_queue: queue(null, 3) })).toBe("idle");
    expect(toSession({ ...base, turn_queue: queue(null, 3) }).opState).toBe("idle");
  });

  it("an activated-but-unclaimed (pending) head is Starting, never running", () => {
    // [S6-F2] A pinned carrier that went offline keeps its pending head: the
    // session must not read "running" forever; the card carries the reason.
    expect(queueOpState(toSessionTurnQueue(queue("pending")))).toBeNull();
    expect(deriveOpState({ ...base, turn_queue: queue("pending") })).toBe("idle");
    expect(deriveOpState({ ...base, status: "busy", turn_queue: queue("pending") })).toBe("idle");
    expect(blockedReasonLabel("carrier_offline: worker-a")).toBe(
      "Carrier offline — waits for it to return",
    );
  });

  it("an in-flight slot holder is running; a held turn needs attention", () => {
    for (const s of ["claimed", "running"]) {
      expect(deriveOpState({ ...base, turn_queue: queue(s) })).toBe("running");
    }
    const held = toSession({ ...base, turn_queue: queue("recovery_required") });
    expect(held.opState).toBe("failed_attention");
    expect(held.needsAttention).toBe(true);
  });

  it("a stale persisted BUSY on an enrolled session is not running", () => {
    expect(deriveOpState({ ...base, status: "busy", turn_queue: queue(null, 1) })).toBe("idle");
  });

  it("a later rejected admission does not flip a running session idle", () => {
    // The UI only re-reads server truth; the overlay still has the active turn.
    const s = toSession({ ...base, status: "idle", turn_queue: queue("running", 0) });
    expect(s.opState).toBe("running");
    expect(s.turnQueue?.activeTurnId).toBe("t-active");
  });

  it("legacy (unenrolled) sessions keep the persisted-status mapping", () => {
    expect(deriveOpState({ ...base, status: "busy" })).toBe("running");
    expect(toSession({ ...base }).turnQueue).toBeNull();
    expect(toSessionTurnQueue(undefined)).toBeNull();
    expect(queueOpState(null)).toBeNull();
  });
});

describe("UI03 task truth mirrors the managed statuses", () => {
  it("maps queued/running/withdrawn/recovery_required", () => {
    expect(deriveTaskState("queued")).toBe("queued");
    expect(deriveTaskState("running")).toBe("running");
    expect(deriveTaskState("withdrawn")).toBe("cancelled");
    expect(deriveTaskState("recovery_required")).toBe("connection_unknown");
  });
});

describe("UI04 distinct query key + shared safety net (no new 3 s poll)", () => {
  it("uses the session-turn-queue key, never the telemetry key", () => {
    expect(sessionTurnQueueKey("s1")).toEqual(["session-turn-queue", "s1"]);
    expect(sessionTurnQueueKey("s1")[0]).not.toBe("session-turns");
  });

  it("falls back only on the A81 safety net", () => {
    expect(TURN_QUEUE_REFETCH_MS).toBe(SAFETY_NET_MS);
    expect(TURN_QUEUE_REFETCH_MS).toBeGreaterThanOrEqual(60_000);
  });
});

describe("UI05 SSE post-commit invalidation + reconnect resync", () => {
  it("a non-terminal queue change refreshes ONLY the queue cards and the session list", () => {
    const target = collectLiveInvalidationTargets([
      { event: "turn_queue_changed", timestamp: "t", session_id: "s1", turn_id: "t1", change: "activated", status: "pending" },
      { event: "turn_queue_changed", timestamp: "t", session_id: "s1", change: "paused" },
    ]);
    expect([...(target.queueSessions ?? [])]).toEqual(["s1"]);
    expect([...target.sessions]).toEqual([]);
    expect(target.tasks).toBe(false);
    const invalidateQueries = vi.fn();
    invalidateLiveTargets({ invalidateQueries } as unknown as QueryClient, target);
    const keys = invalidateQueries.mock.calls.map((c) => JSON.stringify(c[0].queryKey));
    expect(keys).toContain(JSON.stringify(["session-turn-queue", "s1"]));
    expect(keys).toContain(JSON.stringify(["sessions"]));
    expect(keys).not.toContain(JSON.stringify(["session-messages", "s1"]));
    expect(keys).not.toContain(JSON.stringify(["session-turns", "s1"]));
  });

  it("a terminal outcome refreshes the full session (transcript takes over the card)", () => {
    const target = collectLiveInvalidationTargets([
      { event: "turn_queue_changed", timestamp: "t", session_id: "s1", turn_id: "t1", change: "completed", status: "completed" },
    ]);
    expect([...target.sessions]).toEqual(["s1"]);
    expect(target.tasks).toBe(true);
    const invalidateQueries = vi.fn();
    invalidateLiveTargets({ invalidateQueries } as unknown as QueryClient, target);
    const keys = invalidateQueries.mock.calls.map((c) => JSON.stringify(c[0].queryKey));
    expect(keys).toContain(JSON.stringify(["session-messages", "s1"]));
    expect(keys).toContain(JSON.stringify(["session-turn-queue", "s1"]));
    expect(keys.filter((k) => k === JSON.stringify(["session-turn-queue", "s1"]))).toHaveLength(1);
  });

  it("a withdrawal refreshes the task lists but never the transcript", () => {
    const target = collectLiveInvalidationTargets([
      { event: "turn_queue_changed", timestamp: "t", session_id: "s1", turn_id: "t1", change: "withdrawn" },
    ]);
    expect([...target.sessions]).toEqual([]);
    expect(target.tasks).toBe(true);
  });

  it("reconnect resync covers the queue key", () => {
    const invalidateQueries = vi.fn();
    invalidateAllLive({ invalidateQueries } as unknown as QueryClient);
    const keys = invalidateQueries.mock.calls.map((c) => JSON.stringify(c[0].queryKey));
    expect(keys).toContain(JSON.stringify(["session-turn-queue"]));
    expect(keys).toContain(JSON.stringify(["session-turns"]));
  });
});

describe("UI06 pending/terminal dedup by durable id", () => {
  const turn = (task_id: string, extra: Partial<RawTranscriptTurn>): RawTranscriptTurn => ({
    task_id, timestamp: "t", success: true, instruction: "x", result: "", file_count: 0, usage: null, ...extra,
  });

  it("a finished exchange wins; an in-flight one stays owned by its card", () => {
    const finished = transcriptFinishedIds([
      turn("a", { status: "completed", result: "done" }),
      turn("b", { status: "running" }),
      turn("c", { status: "cancelled" }),
    ]);
    expect([...finished].sort()).toEqual(["a", "c"]);
    expect([...queueOwnedIds(["a", "b", "d"], finished)].sort()).toEqual(["b", "d"]);
  });

  it("terminal set matches the backend (withdrawn is terminal)", () => {
    expect(TURN_TERMINAL_STATUSES.has("withdrawn")).toBe(true);
    expect(TURN_TERMINAL_STATUSES.has("recovery_required")).toBe(false);
  });
});

describe("[A82 Stage 8a] cutover labels", () => {
  it("a head waiting on a pre-cutover legacy turn says so", () => {
    expect(blockedReasonLabel("legacy_work_draining: task_1234")).toBe(
      "Waiting for a pre-cutover turn to finish — starts shortly after",
    );
  });

  it("failed post-commit effects get a queue-level label; none ⇒ null", () => {
    expect(effectsFailedLabel(0)).toBeNull();
    expect(effectsFailedLabel(undefined)).toBeNull();
    expect(effectsFailedLabel(1)).toMatch(/^1 finished turn: reply delivery failed/);
    expect(effectsFailedLabel(3)).toMatch(/^3 finished turns/);
  });
});
