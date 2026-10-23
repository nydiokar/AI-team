// @vitest-environment jsdom
/**
 * HomeScreen smoke tests for the redesigned landing view: accounts/usage is the
 * hero (terminal meters, honest unknown), attention is a compact approvals pill
 * that links to the real /approvals decisions view (NOT the Work tab), nodes
 * show ONLINE only with offline folded to a count, and there are no duplicate
 * tab-shortcut buttons.
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
  cost: { data: undefined as unknown },
}));

vi.mock("../hooks/useLiveData", () => ({
  useTargets: () => hooks.targets,
  useQuotaWindows: () => hooks.quota,
  useSessions: () => hooks.sessions,
  useApprovals: () => hooks.approvals,
}));
vi.mock("../hooks/useCost", () => ({ useCostExplorer: () => hooks.cost }));
vi.mock("../components/sessions/NewSessionSheet", () => ({
  NewSessionSheet: () => createElement("div", null, "new-session-sheet"),
}));
vi.mock("../components/home/BackendAccountSheet", () => ({
  BackendAccountSheet: ({ backend }: { backend: string }) => createElement("div", { role: "dialog" }, `${backend} accounts`),
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
  hooks.cost = { data: undefined };
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

const quotaWith = (fiveH: number | null, sevenD: number | null) => ({
  enabled: true,
  mode: "observe_only",
  adapters: [],
  buckets: [
    { bucket_id: "b5h", window_duration_seconds: 18000, provider: "claude", principal_hash: "x", bucket_name: "", window_semantics: "", telemetry_quality: "", first_seen_at: "", last_seen_at: "" },
    { bucket_id: "b7d", window_duration_seconds: 604800, provider: "claude", principal_hash: "x", bucket_name: "", window_semantics: "", telemetry_quality: "", first_seen_at: "", last_seen_at: "" },
  ],
  latest_snapshots: [],
  window_states: [
    { provider: "claude", principal_hash: "x", bucket_id: "b5h", telemetry_state: "ok", telemetry_quality: "", window_semantics: "", classification_status: "", used_percent: fiveH, observed_at: null, window_start_at: null, window_start_inferred_at: null, window_start_source: "", window_end_at: null, active_session_state: "false", window_known: true, automation_ready: false, blockers: [], observed_reset_count: 0, current_reset_observed_since: null, last_reset_change_at: null, reset_boundary_evidence: [] },
    { provider: "claude", principal_hash: "x", bucket_id: "b7d", telemetry_state: sevenD == null ? "unavailable" : "ok", telemetry_quality: "", window_semantics: "", classification_status: "", used_percent: sevenD, observed_at: null, window_start_at: null, window_start_inferred_at: null, window_start_source: "", window_end_at: null, active_session_state: "false", window_known: true, automation_ready: false, blockers: [], observed_reset_count: 0, current_reset_observed_since: null, last_reset_change_at: null, reset_boundary_evidence: [] },
  ],
});

it("always shows the brand and a New action, and no tab-shortcut buttons", () => {
  render();
  expect(container.textContent).toContain("AI-Team");
  expect(container.textContent).toContain("New");
  // The dead shortcut buttons from the first version are gone.
  expect(container.textContent).not.toContain("Dispatch work");
  expect(container.textContent).not.toContain("Everything healthy");
});

it("opens the selected provider account sheet from accessible Home buttons without telemetry", () => {
  render();
  const claude = container.querySelector<HTMLButtonElement>('button[aria-label="Manage claude accounts"]')!;
  expect(claude.type).toBe("button");
  act(() => claude.click());
  expect(container.querySelector('[role="dialog"]')?.textContent).toBe("claude accounts");
  act(() => container.querySelector<HTMLButtonElement>('button[aria-label="Manage codex accounts"]')!.click());
  expect(container.querySelector('[role="dialog"]')?.textContent).toBe("codex accounts");
});

it("renders accounts/usage meters and an honest n/a when unknown", () => {
  hooks.quota = { data: quotaWith(31, null) };
  render();
  expect(container.textContent).toContain("claude");
  expect(container.textContent).toContain("31%");
  expect(container.textContent).toContain("n/a"); // 7d unknown — never a fake 0%
});

it("flags a near-limit window", () => {
  hooks.quota = { data: quotaWith(31, 81) };
  render();
  expect(container.textContent).toContain("81%!");
});

it("approvals pill links to the real /approvals decisions view, not Work", () => {
  hooks.approvals = { data: [{ id: "a1" }, { id: "a2" }] };
  render();
  const link = container.querySelector('a[href="/approvals"]');
  expect(link).not.toBeNull();
  expect(link?.textContent).toContain("2");
  expect(container.querySelector('a[href="/work"]')).toBeNull();
});

it("shows only online nodes and folds offline into a count", () => {
  hooks.targets = {
    data: [node("kanebra", "online"), node("horse-canary", "offline"), node("lp-1", "offline")],
    isLoading: false,
    isError: false,
  };
  render();
  expect(container.textContent).toContain("kanebra");
  expect(container.textContent).not.toContain("horse-canary");
  expect(container.textContent).toContain("2 offline");
});
