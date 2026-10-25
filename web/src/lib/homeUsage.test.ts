import { describe, expect, it } from "vitest";
import { summarizeProviderUsage, windowLabel, NEAR_LIMIT_PERCENT } from "./homeUsage";
import type { RawQuotaWindowsResponse } from "../transport/rawApi";

function resp(partial: Partial<RawQuotaWindowsResponse>): RawQuotaWindowsResponse {
  return {
    enabled: true,
    mode: "observe_only",
    adapters: [],
    buckets: [],
    latest_snapshots: [],
    window_states: [],
    ...partial,
  };
}

const bucket = (bucket_id: string, seconds: number | null) => ({
  provider: "claude",
  principal_hash: "abcdef1234567890",
  bucket_id,
  bucket_name: bucket_id,
  window_semantics: "SLIDING",
  telemetry_quality: "ok",
  window_duration_seconds: seconds,
  first_seen_at: "",
  last_seen_at: "",
});

const state = (bucket_id: string, used: number | null) => ({
  provider: "claude",
  principal_hash: "abcdef1234567890",
  bucket_id,
  telemetry_state: used == null ? "unavailable" : "ok",
  telemetry_quality: "ok",
  window_semantics: "SLIDING",
  classification_status: "classified",
  used_percent: used,
  observed_at: null,
  window_start_at: null,
  window_start_inferred_at: null,
  window_start_source: "",
  window_end_at: "2026-10-22T20:00:00Z",
  active_session_state: "false",
  window_known: true,
  automation_ready: false,
  blockers: [],
  observed_reset_count: 0,
  current_reset_observed_since: null,
  last_reset_change_at: null,
  reset_boundary_evidence: [],
});

describe("windowLabel", () => {
  it("snaps the common provider windows to crisp labels", () => {
    expect(windowLabel(18000)).toBe("5h");
    expect(windowLabel(604800)).toBe("7d");
    expect(windowLabel(3600)).toBe("1h");
    expect(windowLabel(172800)).toBe("2d");
    expect(windowLabel(null)).toBe("window");
  });
});

describe("summarizeProviderUsage", () => {
  it("returns nothing when quota observation is disabled", () => {
    expect(summarizeProviderUsage(resp({ enabled: false }))).toEqual([]);
    expect(summarizeProviderUsage(undefined)).toEqual([]);
  });

  it("joins window_states to bucket durations and orders shortest-first", () => {
    const out = summarizeProviderUsage(
      resp({
        buckets: [bucket("b5h", 18000), bucket("b7d", 604800)],
        window_states: [state("b7d", 20), state("b5h", 60)],
      }),
    );
    expect(out).toHaveLength(1);
    expect(out[0].provider).toBe("claude");
    expect(out[0].accountHint).toBe("abcdef12"); // never a secret — prefix only
    expect(out[0].windows.map((w) => w.label)).toEqual(["5h", "7d"]);
    expect(out[0].windows.map((w) => w.usedPercent)).toEqual([60, 20]);
  });

  it("flags near-limit at the threshold and never fabricates a 0% when unknown", () => {
    const out = summarizeProviderUsage(
      resp({
        buckets: [bucket("b5h", 18000), bucket("b7d", 604800)],
        window_states: [state("b5h", NEAR_LIMIT_PERCENT), state("b7d", null)],
      }),
    );
    const [fiveH, sevenD] = out[0].windows;
    expect(fiveH.nearLimit).toBe(true);
    expect(sevenD.usedPercent).toBeNull(); // honest "unknown", not 0%
    expect(sevenD.nearLimit).toBe(false);
    expect(sevenD.unknownReason).toBe("unavailable");
  });

  it("clamps out-of-range percentages", () => {
    const out = summarizeProviderUsage(
      resp({ buckets: [bucket("b", 18000)], window_states: [state("b", 140)] }),
    );
    expect(out[0].windows[0].usedPercent).toBe(100);
  });

  it("falls back to latest_snapshots when window_states is absent", () => {
    const out = summarizeProviderUsage(
      resp({
        window_states: [],
        latest_snapshots: [
          {
            snapshot_id: "s1",
            provider: "codex",
            principal_hash: "0011223344",
            bucket_id: "b",
            observed_at: "",
            telemetry_quality: "ok",
            used_percent: 42,
            reset_at: null,
            limit_reached: 0,
            window_duration_seconds: 18000,
            raw_status: "ok",
            unavailable_reason: "",
            created_at: "",
          },
        ],
      }),
    );
    expect(out).toHaveLength(1);
    expect(out[0].provider).toBe("codex");
    expect(out[0].windows[0]).toMatchObject({ label: "5h", usedPercent: 42 });
  });
});

it("keeps overall weekly usage separate from scoped limits and summarizes accounts deterministically", () => {
  const out = summarizeProviderUsage(resp({
    buckets: [bucket("five_hour", 18000), bucket("seven_day", 604800), bucket("weekly_scoped:sonnet", 604800)],
    window_states: [state("weekly_scoped:sonnet", 99), state("seven_day", 20), state("five_hour", 30), { ...state("five_hour", 60), principal_hash: "another-account" }],
  }));
  expect(out[0].windows.map((w) => w.usedPercent)).toEqual([60, 20]);
});
