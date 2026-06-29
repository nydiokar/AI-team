import { describe, it, expect } from "vitest";
import { compactTokens, turnDuration, contextTokens } from "./SessionTurns";
import type { RawTurn } from "../../transport/rawApi";

describe("compactTokens", () => {
  it("renders null/undefined as an em-dash placeholder", () => {
    expect(compactTokens(null)).toBe("—");
    expect(compactTokens(undefined)).toBe("—");
  });
  it("keeps sub-1k counts exact", () => {
    expect(compactTokens(0)).toBe("0");
    expect(compactTokens(980)).toBe("980");
  });
  it("abbreviates thousands (1 decimal under 10k, whole above)", () => {
    expect(compactTokens(9800)).toBe("9.8k");
    expect(compactTokens(48217)).toBe("48k");
    // The real codex context observed live (644776) → 645k, not a fabricated %.
    expect(compactTokens(644776)).toBe("645k");
  });
  it("abbreviates millions", () => {
    expect(compactTokens(2_400_000)).toBe("2.4M");
  });
});

describe("contextTokens — #35 headline preference", () => {
  it("prefers peak, then exit, then raw context_tokens", () => {
    expect(contextTokens({ peak_context_tokens: 5, turn_exit_context_tokens: 9, context_tokens: 1 })).toBe(5);
    expect(contextTokens({ turn_exit_context_tokens: 9, context_tokens: 1 })).toBe(9);
    expect(contextTokens({ context_tokens: 1 })).toBe(1);
  });
  it("is null when no context signal exists (unavailable coverage)", () => {
    expect(contextTokens({ metric_quality: "unavailable" })).toBeNull();
  });
  it("falls through peak=null to the real context count (codex aggregate_only case)", () => {
    // Live shape: peak/exit null but context_tokens populated → must surface it.
    expect(
      contextTokens({ peak_context_tokens: null, turn_exit_context_tokens: null, context_tokens: 644776 }),
    ).toBe(644776);
  });
});

function turn(partial: Partial<RawTurn>): RawTurn {
  return {
    turn_id: "t", session_id: "s", task_id: "task", backend: "codex",
    requested_model: null, observed_models: [], started_at: null, ended_at: null,
    final_status: "success", timeout_status: "none", final_exit_code: null,
    metrics: {}, coverage: {}, data_quality: [], ...partial,
  };
}

describe("turnDuration", () => {
  it("prefers the projection's measured wall_time_ms", () => {
    expect(turnDuration(turn({ metrics: { wall_time_ms: 8979 } }))).toBe("9.0s");
    expect(turnDuration(turn({ metrics: { wall_time_ms: 8 } }))).toBe("8ms");
    expect(turnDuration(turn({ metrics: { wall_time_ms: 200936 } }))).toBe("3m 21s");
  });
  it("falls back to the start/end stamps when wall time is absent", () => {
    expect(
      turnDuration(turn({ started_at: "2026-06-29T00:00:00Z", ended_at: "2026-06-29T00:00:01.5Z" })),
    ).toBe("1.5s");
  });
  it("is an em-dash when neither source is usable", () => {
    expect(turnDuration(turn({}))).toBe("—");
  });
});
