// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { useSessions } from "./useLiveData";
vi.mock("../stores/authStore", () => ({ useAuthStore: (pick: (s: { token: string }) => unknown) => pick({ token: "test" }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("keeps failed cached reads distinguishable and recovers with prefix invalidation", async () => {
  const qc = new QueryClient();
  qc.setQueryDefaults(["sessions"], { retryDelay: 0 });
  let latest!: ReturnType<typeof useSessions>;
  function Probe() { latest = useSessions(); return <span>{latest.status}{latest.error?.message}</span>; }
  const response = (read_state?: string) => new Response(JSON.stringify({ sessions: [], ...(read_state ? { read_state } : {}) }));
  const fetch = vi.fn().mockResolvedValue(response());
  vi.stubGlobal("fetch", fetch);
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => { root.render(<QueryClientProvider client={qc}><Probe /></QueryClientProvider>); await new Promise(r => setTimeout(r, 20)); });
    await act(async () => { await new Promise(r => setTimeout(r, 20)); });
    expect(latest.data).toEqual([]);
    for (const state of ["fallback", "partial"]) {
      fetch.mockImplementation(async () => response(state));
      await act(async () => { await qc.invalidateQueries({ queryKey: ["sessions"] }); await new Promise(r => setTimeout(r, 20)); });
      expect(latest.error?.message).toBe(`sessions_${state}`);
      expect(latest.data).toEqual([]);
    }
    fetch.mockImplementation(async () => response());
    await act(async () => { await qc.invalidateQueries({ queryKey: ["sessions"] }); await new Promise(r => setTimeout(r, 20)); });
    expect(latest.error).toBeNull();
    expect(latest.isSuccess).toBe(true);
  } finally { await act(async () => root.unmount()); qc.clear(); vi.unstubAllGlobals(); }
});
