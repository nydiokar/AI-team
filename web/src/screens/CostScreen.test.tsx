// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { CostBucket, CostExplorer } from "../domain/cost";

const hooks = vi.hoisted(() => ({ explorer: vi.fn(), top: vi.fn(), projects: vi.fn(), alerts: vi.fn(), affiliations: vi.fn() }));
vi.mock("../hooks/useCost", () => ({ useCostExplorer: hooks.explorer, useCostTop: hooks.top, useCostProjects: hooks.projects, useCostAlerts: hooks.alerts }));
vi.mock("../hooks/useWork", () => ({ useSessionAffiliations: hooks.affiliations }));
vi.mock("../components/cost/CaseCostSheet", () => ({ CaseCostSheet: () => <div role="dialog">Case lifetime usage</div> }));
import { CostScreen } from "./CostScreen";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
let root: Root;
const retry = vi.fn();
const bucket = (usd = 25, tokens = 2000, unpriced = 0): CostBucket => ({
  tokens: { input: tokens * .4, output: tokens * .1, cacheRead: tokens * .4, cacheCreation: tokens * .1, total: tokens },
  usd: { known: usd, coveragePct: tokens ? (1 - unpriced / tokens) * 100 : 100, unpricedTokens: unpriced },
});
const explorer = (): CostExplorer => ({
  dimension: "model", granularity: "day", from: "2026-10-24T12:00:00Z", to: "2026-10-26T12:00:00Z", repoPath: null,
  totals: bucket(), unattributed: { ...bucket(7, 100, 20), models: [] },
  series: [{ ...bucket(10, 1000), bucket: "2026-10-24", dim: "sonnet", models: [] }, { ...bucket(15, 1000), bucket: "2026-10-26", dim: "opus", models: [] }],
});
const result = <T,>(data: T | undefined, error: unknown = null) => ({ data, error, isLoading: false, isFetching: false, refetch: retry });
let daily: ReturnType<typeof result<CostExplorer>>;
let project: ReturnType<typeof result<CostExplorer>>;

beforeEach(() => {
  vi.clearAllMocks();
  daily = result(explorer());
  project = result({ ...explorer(), dimension: "project", granularity: "none", series: [{ ...bucket(), bucket: "", dim: "/repo/AI-team", models: [] }] });
  hooks.explorer.mockImplementation((opts: { granularity: string }) => opts.granularity === "day" ? daily : project);
  hooks.top.mockReturnValue(result({ rows: [], totals: bucket() }));
  hooks.projects.mockReturnValue(result({ projects: [{ repoPath: "/repo/AI-team", tokens: bucket().tokens }, { repoPath: "<no repo path>", tokens: bucket().tokens }] }));
  hooks.alerts.mockReturnValue(result(undefined));
  hooks.affiliations.mockReturnValue({ index: new Map(), isLoading: false, error: null });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });
function render() { act(() => root.render(<MemoryRouter><CostScreen /></MemoryRouter>)); }
function button(text: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((el) => el.textContent === text);
  if (!found) throw new Error(`Missing button ${text}`);
  return found;
}

it("renders consistent summary, timeline, model mix and honest global attribution", () => {
  render();
  expect(container.querySelector("h1")?.textContent).toBe("Cost overview.");
  expect(container.querySelector('[aria-label="Cost summary"]')?.textContent).toContain("$25.00");
  expect(container.querySelector('[aria-label="Spend over time"]')?.textContent).toContain("$25.00");
  expect(container.textContent).toContain("Global for this time window, excluded from the totals above");
  expect(container.textContent).toContain("API row limits");
  expect(container.querySelectorAll("table tbody tr")).toHaveLength(3);
  expect(container.querySelector("table")?.textContent).toContain("Unpriced tokens");
});

it("uses the selected project/range for both explorer queries and rankings", () => {
  render();
  act(() => button("AI-team").click());
  expect(hooks.explorer).toHaveBeenLastCalledWith(expect.objectContaining({ repoPath: "/repo/AI-team", range: "7d" }));
  expect(hooks.top).toHaveBeenLastCalledWith(expect.objectContaining({ repoPath: "/repo/AI-team", range: "7d" }));
  act(() => button("30d").click());
  expect(hooks.explorer).toHaveBeenLastCalledWith(expect.objectContaining({ range: "30d" }));
  expect(container.textContent).toContain("not scoped to this project");
  act(() => button("Clear filter").click());
  expect(hooks.top).toHaveBeenLastCalledWith(expect.objectContaining({ repoPath: undefined }));
});

it("switches timeline and ranking metrics independently", () => {
  render();
  const timelineTokens = container.querySelector<HTMLButtonElement>('[aria-label="Timeline metric"] button:last-child')!;
  act(() => timelineTokens.click());
  expect(timelineTokens.getAttribute("aria-pressed")).toBe("true");
  expect(container.querySelector('[aria-label="Spend over time"]')?.textContent).toContain("2.0K");
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="Session ranking"] button:last-child')!.click());
  expect(hooks.top).toHaveBeenLastCalledWith(expect.objectContaining({ by: "tokens" }));
});

it("isolates an unavailable sessions request and retries it without hiding the chart", () => {
  hooks.top.mockReturnValue(result(undefined, new Error("offline")));
  render();
  expect(container.querySelector('[aria-label="Spend over time"]')?.textContent).toContain("$25.00");
  expect(container.querySelector('[aria-label="Top sessions"]')?.textContent).toContain("unavailable");
  act(() => button("Retry").click());
  expect(retry).toHaveBeenCalledOnce();
});

it("retains same-scope cached charts with an explicit failed-refresh notice", () => {
  daily.error = new Error("offline");
  render();
  expect(container.textContent).toContain("Refresh failed. Showing last available data.");
  expect(container.querySelector('[aria-label="Spend over time"]')?.textContent).toContain("$25.00");
});

it("distinguishes no usage from usage with no known price", () => {
  daily.data = { ...explorer(), totals: bucket(0, 0), series: [] };
  render();
  expect(container.textContent).toContain("No recorded usage in this window.");
  expect(container.querySelector('[aria-label="Cost summary"]')?.textContent).not.toContain("100.0%");
  daily.data = { ...explorer(), totals: bucket(0, 1000, 1000), series: [{ ...bucket(0, 1000, 1000), bucket: "2026-10-24", dim: "new-model", models: [] }] };
  render();
  expect(container.textContent).toContain("Usage recorded without a known price.");
  expect(container.querySelector('[aria-label="Cost summary"]')?.textContent).toContain("0.0%");
  expect(container.textContent).toContain("1.0K unpriced tokens");
});

it("omits unfilterable sentinels from project options and preserves a selection missing from a new list", () => {
  render();
  expect(container.querySelector('option[value="<no repo path>"]')).toBeNull();
  act(() => button("AI-team").click());
  hooks.projects.mockReturnValue(result({ projects: [] }));
  render();
  expect(container.querySelector<HTMLSelectElement>('[aria-label="Filter by project"]')?.value).toBe("/repo/AI-team");
});

it("keeps session navigation and case lifetime drilldown separately actionable", () => {
  hooks.top.mockReturnValue(result({ rows: [{ ...bucket(), sessionId: "session-1", repoPath: "/repo/AI-team", backend: "claude", role: "worker", models: [] }] }));
  hooks.affiliations.mockReturnValue({ index: new Map([["session-1", { flowRunId: "case-1", caseTitle: "Ship it" }]]), error: null });
  render();
  expect(container.querySelector('a[href="/sessions/session-1"]')).not.toBeNull();
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="View case lifetime cost: Ship it"]')!.click());
  expect(container.querySelector('[role="dialog"]')?.textContent).toContain("Case lifetime");
});
