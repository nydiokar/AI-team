import { expect, it, vi } from "vitest";
import { api } from "./apiClient";

it("explicitly requests compact affiliations with a client deadline", async () => {
  const body = { affiliations: [{ session_id: "s", flow_run_id: "c", role: "worker", case_title: "Title", case_status: null }], total: 1 };
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(body)));
  vi.stubGlobal("fetch", fetch);
  try {
    expect(await api.workAffiliations("test-token")).toEqual(body);
    expect(fetch).toHaveBeenCalledWith("/api/work/affiliations/sessions?format=compact", {
      signal: expect.any(AbortSignal), headers: { Authorization: "Bearer test-token" },
    });
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(false);
  } finally { vi.unstubAllGlobals(); }
});
