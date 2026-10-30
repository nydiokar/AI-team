// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
const hooks = vi.hoisted(() => ({ jobs: vi.fn() }));
vi.mock("../../hooks/useLiveData", async (original) => ({ ...await original<typeof import("../../hooks/useLiveData")>(), useJobs: hooks.jobs }));
import { JobsPanel } from "./JobsPanel";
import { SessionJobsSection } from "../../screens/SessionDetailScreen";
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const retry = vi.fn();
beforeEach(() => { vi.clearAllMocks(); });

for (const surface of ["system", "session"]) {
  it(`${surface}: distinguishes failure from empty, labels stale cache and recovers`, () => {
    const el = document.createElement("div");
    const root = createRoot(el);
    const summary = vi.fn();
    const render = () => act(() => root.render(<MemoryRouter>{surface === "system" ? <JobsPanel expanded onSummary={summary} /> : <SessionJobsSection sessionId="s" />}</MemoryRouter>));
    try {
      hooks.jobs.mockReturnValue({ data: undefined, isLoading: false, error: new Error("503"), refetch: retry });
      render();
      expect(el.textContent).toContain("Watched jobs unavailable");
      expect(el.textContent).not.toContain("No watched jobs");
      act(() => el.querySelector<HTMLButtonElement>('button')!.click());
      expect(retry).toHaveBeenCalledOnce();
      if (surface === "system") expect(summary).toHaveBeenLastCalledWith({ total: 0, running: 0, unavailable: true });
      hooks.jobs.mockReturnValue({ data: { running: [], recent: [] }, isLoading: false, error: new Error("503"), refetch: retry });
      render();
      expect(el.textContent).toContain("Refresh failed. Showing last available jobs.");
      expect(el.textContent).not.toContain("No watched jobs");
      hooks.jobs.mockReturnValue({ data: { running: [], recent: [{ id: "job", label: "Cached job", status: "done", session_id: null }] }, isLoading: false, error: new Error("503"), refetch: retry });
      render();
      expect(el.textContent).toContain("Cached job");
      expect(el.textContent).toContain("Refresh failed");
      hooks.jobs.mockReturnValue({ data: { running: [], recent: [] }, isLoading: false, error: null, refetch: retry });
      render();
      expect(el.textContent).toContain("No watched jobs");
      expect(el.textContent).not.toContain("unavailable");
      if (surface === "system") expect(summary).toHaveBeenLastCalledWith({ total: 0, running: 0, unavailable: false });
    } finally { act(() => root.unmount()); }
  });
}


it("reports an unavailable collapsed System panel so its parent keeps the header reachable", () => {
  const el = document.createElement("div");
  const root = createRoot(el);
  const summary = vi.fn();
  hooks.jobs.mockReturnValue({ data: undefined, isLoading: false, error: new Error("503"), refetch: retry });
  try {
    act(() => root.render(<JobsPanel expanded={false} onSummary={summary} />));
    expect(summary).toHaveBeenCalledWith({ total: 0, running: 0, unavailable: true });
    expect(el.textContent).toBe("");
  } finally { act(() => root.unmount()); }
});
