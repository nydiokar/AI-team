// @vitest-environment jsdom
/**
 * HomeScreen smoke tests: brand + quick actions always render; nodes render
 * with their health; usage degrades honestly (no fake 0%); the attention glance
 * flips between "healthy" and a real actionable list.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { createElement } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const hooks = vi.hoisted(() => ({
  targets: { data: [] as unknown[], isLoading: false, isError: false },
  quota: { data: undefined as unknown },
  sessions: { data: [] as unknown[] },
  approvals: { data: [] as unknown[] },
}));

vi.mock("../hooks/useLiveData", () => ({
  useTargets: () => hooks.targets,
  useQuotaWindows: () => hooks.quota,
  useSessions: () => hooks.sessions,
  useApprovals: () => hooks.approvals,
}));

import { HomeScreen } from "./HomeScreen";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  hooks.targets = { data: [], isLoading: false, isError: false };
  hooks.quota = { data: undefined };
  hooks.sessions = { data: [] };
  hooks.approvals = { data: [] };
});

function render() {
  act(() => {
    root.render(createElement(MemoryRouter, null, createElement(HomeScreen)));
  });
}

const node = (id: string, health: string) => ({
  id,
  health,
  live: health === "online",
  heartbeatAgeSec: 12,
  backends: ["claude", "codex"],
  tailscaleIp: "",
  maxConcurrent: 8,
  slotsUsed: 1,
  slotsTotal: 8,
  activeTasks: [],
});

it("always shows the brand and a New session quick action", () => {
  render();
  expect(container.textContent).toContain("AI-Team");
  expect(container.textContent).toContain("New session");
});

it("shows a calm healthy glance when nothing needs the operator", () => {
  hooks.targets = { data: [node("kanebra-worker", "online")], isLoading: false, isError: false };
  render();
  expect(container.textContent).toContain("Everything healthy");
  expect(container.textContent).toContain("kanebra-worker");
  expect(container.textContent).toContain("Online");
});

it("surfaces an actionable attention list and counts offline nodes", () => {
  hooks.targets = { data: [node("horse", "offline")], isLoading: false, isError: false };
  hooks.approvals = { data: [{ id: "a1" }, { id: "a2" }] };
  hooks.sessions = { data: [{ id: "s1", needsAttention: true }] };
  render();
  expect(container.textContent).toContain("Needs attention");
  expect(container.textContent).toContain("2 approvals waiting");
  expect(container.textContent).toContain("1 session need"); // singular-ish copy
  expect(container.textContent).toContain("1 node offline");
});

it("renders usage meters and never fabricates a 0% when unknown", () => {
  hooks.quota = {
    data: {
      enabled: true,
      mode: "observe_only",
      adapters: [],
      buckets: [
        { bucket_id: "b5h", window_duration_seconds: 18000, provider: "claude", principal_hash: "x", bucket_name: "", window_semantics: "", telemetry_quality: "", first_seen_at: "", last_seen_at: "" },
        { bucket_id: "b7d", window_duration_seconds: 604800, provider: "claude", principal_hash: "x", bucket_name: "", window_semantics: "", telemetry_quality: "", first_seen_at: "", last_seen_at: "" },
      ],
      latest_snapshots: [],
      window_states: [
        { provider: "claude", principal_hash: "x", bucket_id: "b5h", telemetry_state: "ok", telemetry_quality: "", window_semantics: "", classification_status: "", used_percent: 90, observed_at: null, window_start_at: null, window_start_inferred_at: null, window_start_source: "", window_end_at: null, active_session_state: "false", window_known: true, automation_ready: false, blockers: [], observed_reset_count: 0, current_reset_observed_since: null, last_reset_change_at: null, reset_boundary_evidence: [] },
        { provider: "claude", principal_hash: "x", bucket_id: "b7d", telemetry_state: "unavailable", telemetry_quality: "", window_semantics: "", classification_status: "", used_percent: null, observed_at: null, window_start_at: null, window_start_inferred_at: null, window_start_source: "", window_end_at: null, active_session_state: "false", window_known: true, automation_ready: false, blockers: [], observed_reset_count: 0, current_reset_observed_since: null, last_reset_change_at: null, reset_boundary_evidence: [] },
      ],
    },
  };
  render();
  expect(container.textContent).toContain("90%");
  expect(container.textContent).toContain("near"); // near-limit cue, not colour-only
  expect(container.textContent).toContain("unknown"); // 7d honest unknown, not "0%"
});

it("says usage is off when the quota adapter is disabled", () => {
  hooks.quota = { data: { enabled: false, mode: "observe_only", adapters: [], buckets: [], latest_snapshots: [], window_states: [] } };
  render();
  expect(container.textContent).toContain("Usage telemetry is off");
});
