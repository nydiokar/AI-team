// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { SessionCostPanel } from "./SessionDetailScreen";

const { read } = vi.hoisted(() => ({ read: vi.fn() }));
vi.mock("../hooks/useLiveData", () => ({ useSessionUsage: read }));

const container = document.createElement("div");
const root = createRoot(container);
afterEach(() => act(() => root.render(null)));

function render(state: string, total = 0, isError = false) {
  read.mockReturnValue({
    data: {
      usage_state: state,
      tokens: { total, input: total, output: 0, cache_read: 0, cache_creation: 0 },
      cost: { known: false, reason: "unknown_model" },
    },
    isError, isLoading: false, refetch: vi.fn(),
  });
  act(() => root.render(<SessionCostPanel sessionId="s" />));
}

it("keeps pending and unavailable usage visible without invented totals", () => {
  render("pending");
  expect(container.textContent).toContain("Usage pending");
  expect(container.textContent).not.toContain("Total tokens");
  render("unavailable");
  expect(container.textContent).toContain("Usage unavailable");
});

it("renders recorded zero and labels interim totals", () => {
  render("recorded");
  expect(container.textContent).toContain("Total tokens0");
  render("partial", 123);
  expect(container.textContent).toContain("Recorded so far");
  expect(container.textContent).toContain("Total tokens123");
});

it("shows read errors and keeps stale known usage explicitly labeled", () => {
  render("recorded", 123, true);
  expect(container.textContent).toContain("could not be refreshed");
  expect(container.textContent).toContain("Last recorded totals");
  expect(container.querySelector("button")?.textContent).toBe("Retry");
});
