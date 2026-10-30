// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { api, ApiError } from "../transport/apiClient";
import { useSessionAffiliations } from "./useWork";
import { SessionAffiliationLink } from "../components/work/SessionAffiliationLabel";

const auth = vi.hoisted(() => ({ token: "test" }));
vi.mock("../stores/authStore", () => ({ useAuthStore: (pick: (s: typeof auth) => unknown) => pick(auth) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("keeps membership unknown until success, rejects failed cached refresh and recovers via prefix invalidation", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
  // A legacy cached value must not satisfy the versioned compact query.
  qc.setQueryData(["work-affiliations"], new Map());
  let resolve!: (v: { affiliations: []; total: number }) => void;
  const fetch = vi.spyOn(api, "workAffiliations").mockImplementation(() => new Promise((r) => { resolve = r; }));
  let latest!: ReturnType<typeof useSessionAffiliations>;
  function Probe() {
    latest = useSessionAffiliations();
    return <><SessionAffiliationLink affiliation={latest.index.get("s")} isKnown={latest.isKnown} isLoading={latest.isLoading} /><span>{latest.index.size}</span></>;
  }
  const el = document.createElement("div");
  const root = createRoot(el);
  try {
    await act(async () => { root.render(<QueryClientProvider client={qc}><MemoryRouter><Probe /></MemoryRouter></QueryClientProvider>); });
    expect(el.textContent).toContain("Loading case link");
    expect(latest.isKnown).toBe(false);
    await act(async () => { resolve({ affiliations: [], total: 0 }); await new Promise((r) => setTimeout(r, 10)); });
    expect(latest.isKnown).toBe(true);
    expect(el.textContent).toContain("Standalone");
    fetch.mockResolvedValue({ affiliations: [{ session_id: "s", flow_run_id: "c", role: "worker", case_title: "Case title", case_status: "closed" }], total: 1 });
    await act(async () => { await qc.invalidateQueries({ queryKey: ["work-affiliations"] }); await new Promise((r) => setTimeout(r, 10)); });
    expect(el.textContent).toContain("of closed case");
    // Cached positive and cached empty responses must both become unknown on error.
    fetch.mockRejectedValue(new ApiError(401, "expired"));
    await act(async () => { await qc.invalidateQueries({ queryKey: ["work-affiliations"] }); await new Promise((r) => setTimeout(r, 10)); });
    expect(latest.isKnown).toBe(false);
    expect(latest.index.size).toBe(0);
    expect(el.textContent).toContain("Case link unavailable");
    expect(el.textContent).not.toContain("Standalone");
    fetch.mockResolvedValue({ affiliations: [], total: 0 });
    await act(async () => { await qc.invalidateQueries({ queryKey: ["work-affiliations"] }); await new Promise((r) => setTimeout(r, 10)); });
    expect(latest.isKnown).toBe(true);
    fetch.mockRejectedValue(new ApiError(401, "expired"));
    await act(async () => { await qc.invalidateQueries({ queryKey: ["work-affiliations"] }); await new Promise((r) => setTimeout(r, 10)); });
    expect(latest.isKnown).toBe(false);
    expect(el.textContent).not.toContain("Standalone");
  } finally {
    await act(async () => root.unmount());
    qc.clear();
    fetch.mockRestore();
  }
});
