import { afterEach, expect, it, vi } from "vitest";
import { api } from "./apiClient";

afterEach(() => vi.restoreAllMocks());

const reads: { name: string; request: (signal?: AbortSignal) => Promise<unknown> }[] = [
  { name: "explorer", request: (signal) => api.costExplorer("test", {}, signal) },
  { name: "top", request: (signal) => api.costTop("test", {}, signal) },
  { name: "projects", request: (signal) => api.costProjects("test", {}, signal) },
  { name: "alerts", request: (signal) => api.costAlerts("test", signal) },
  { name: "case usage", request: (signal) => api.caseUsage("test", "case-1", signal) },
];

it.each(reads)("bounds $name reads and preserves caller cancellation", async ({ request }) => {
  const deadline = new AbortController();
  const caller = new AbortController();
  const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal);
  const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", { status: 200 }));
  await request(caller.signal);
  expect(timeout).toHaveBeenCalledWith(15000);
  const passedSignal = fetcher.mock.calls[0][1]?.signal;
  expect(passedSignal?.aborted).toBe(false);
  caller.abort();
  expect(passedSignal?.aborted).toBe(true);

  await request();
  deadline.abort();
  expect(fetcher.mock.calls[1][1]?.signal?.aborted).toBe(true);
});

it("terminates a hung alerts request when its deadline aborts", async () => {
  const deadline = new AbortController();
  vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal);
  vi.spyOn(globalThis, "fetch").mockImplementation((_url, options) => new Promise((_resolve, reject) => {
    options?.signal?.addEventListener("abort", () => reject(options.signal?.reason), { once: true });
  }));
  const rejected = expect(api.costAlerts("test")).rejects.toThrow("deadline");
  deadline.abort(new DOMException("deadline", "TimeoutError"));
  await rejected;
});
