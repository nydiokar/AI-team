// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Target } from "../../domain/models";
const mocks = vi.hoisted(() => ({ status: vi.fn(), auth: vi.fn() }));
vi.mock("../../transport/apiClient", () => ({ api: { backendAuthStatus: mocks.status, backendAuth: mocks.auth } }));
vi.mock("../../stores/authStore", () => ({ useAuthStore: (select: (s: { token: string }) => string) => select({ token: "test" }) }));
import { BackendAccountSheet, safeAuthUrl } from "./BackendAccountSheet";
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let container: HTMLDivElement;
const node = (id: string, live = true, backends = ["claude", "codex"]) => ({ id, live, backends }) as Target;
const flush = async () => { await act(async () => { await Promise.resolve(); }); };
const click = async (text: string) => { await act(async () => { Array.from(document.querySelectorAll("button")).find((b) => b.textContent === text)!.click(); }); };
beforeEach(() => {
  vi.useFakeTimers();
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  mocks.status.mockResolvedValue({ state: "signed_out" });
  mocks.auth.mockResolvedValue({ login_id: "a".repeat(32), status: "running", offset: 0 });
});
afterEach(() => {
  act(() => root.unmount()); container.remove(); vi.useRealTimers(); vi.resetAllMocks();
});
function render(backend: "claude" | "codex" = "claude", nodes = [node("kanebra")]) {
  act(() => root.render(createElement(BackendAccountSheet, { backend, nodes, onClose: vi.fn() })));
}
it("checks every eligible node independently and isolates status failures", async () => {
  mocks.status.mockImplementation((_t, id) => id === "broken" ? Promise.reject(new Error()) : Promise.resolve({ state: "signed_in" }));
  render("claude", [node("kanebra"), node("broken"), node("offline", false), node("other", true, ["codex"])]);
  expect(document.body.textContent).toContain("Checking account");
  await flush();
  expect(mocks.status).toHaveBeenCalledTimes(2);
  expect(document.body.textContent).toContain("Signed in");
  expect(document.body.textContent).toContain("Account status unavailable");
  expect(document.body.textContent).not.toContain("offline");
});
it("limits status fan-out and eventually checks all nodes", async () => {
  const pending: Array<() => void> = [];
  let running = 0;
  let peak = 0;
  mocks.status.mockImplementation(() => {
    running++;
    peak = Math.max(peak, running);
    return new Promise((resolve) => pending.push(() => {
      running--;
      resolve({ state: "signed_out" });
    }));
  });
  render("claude", Array.from({ length: 25 }, (_, i) => node(`node-${i}`)));
  await flush();
  expect(mocks.status).toHaveBeenCalledTimes(4);
  for (let i = 0; i < 25; i++) {
    await act(async () => { pending.shift()!(); });
  }
  expect(mocks.status).toHaveBeenCalledTimes(25);
  expect(peak).toBe(4);
  expect(document.querySelectorAll('[role="status"]')).toHaveLength(25);
  expect(document.body.textContent).not.toContain("unavailable");
});
it("aborts in-flight status reads and discards queued reads on close", async () => {
  const pending: Array<() => void> = [];
  mocks.status.mockImplementation(() => new Promise((resolve) => pending.push(() => resolve({ state: "signed_out" }))));
  render("claude", Array.from({ length: 20 }, (_, i) => node(`node-${i}`)));
  await flush();
  expect(mocks.status).toHaveBeenCalledTimes(4);
  act(() => root.render(null));
  expect(mocks.status.mock.calls.every((call) => call[3].aborted)).toBe(true);
  await act(async () => { pending.forEach((resolve) => resolve()); });
  expect(mocks.status).toHaveBeenCalledTimes(4);
});
it("uses the worker's input capability and aborts polling when cancelled", async () => {
  mocks.auth.mockResolvedValue({ login_id: "a".repeat(32), status: "running", input_supported: true });
  render(); await flush(); await click("Log in"); await flush();
  expect(document.querySelector('input[aria-label="Claude login response"]')).not.toBeNull();
  const poll = mocks.auth.mock.calls.find((call) => call[3] === "poll")!;
  expect(poll[5].aborted).toBe(false);
  await click("Cancel login");
  expect(poll[5].aborted).toBe(true);
});
it("stops polling after completion and does not cancel a successful login on unmount", async () => {
  mocks.auth.mockImplementation((_t, _id, _b, action) => Promise.resolve(action === "poll" ? { login_id: "a".repeat(32), status: "exited", authenticated: true, output: "done", offset: 4 } : { login_id: "a".repeat(32), status: "running" }));
  render(); await flush(); await click("Log in"); await flush();
  expect(document.body.textContent).toContain("Signed in");
  const calls = mocks.auth.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(mocks.auth).toHaveBeenCalledTimes(calls);
  act(() => root.render(null));
  expect(mocks.auth.mock.calls.some((c) => c[3] === "cancel")).toBe(false);
});
it("waits for each poll and cancels a running login on unmount", async () => {
  let finish: (value: object) => void = () => undefined;
  mocks.auth.mockImplementation((_t, _id, _b, action) => action === "poll" ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve({ login_id: "a".repeat(32), status: "running" }));
  render(); await flush(); await click("Log in"); await flush();
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(mocks.auth.mock.calls.filter((c) => c[3] === "poll")).toHaveLength(1);
  act(() => root.render(null));
  await act(async () => { finish({ status: "running", output: "stale", offset: 5 }); });
  expect(mocks.auth.mock.calls.filter((c) => c[3] === "cancel")).toHaveLength(1);
});
it("supports device auth without a code-paste input and cancels explicitly", async () => {
  mocks.auth.mockImplementation((_t, _id, _b, action) => Promise.resolve({ login_id: "a".repeat(32), status: "running", output: action === "poll" ? "https://auth.openai.com/codex/device https://evil.example/login" : "", offset: 100 }));
  render("codex"); await flush(); await click("Log in"); await flush();
  expect(document.querySelector("input")).toBeNull();
  expect(document.querySelectorAll('a[href="https://auth.openai.com/codex/device"]')).toHaveLength(1);
  expect(document.querySelector('a[href="https://evil.example/login"]')).toBeNull();
  await click("Cancel login"); await flush();
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(mocks.auth.mock.calls.filter((c) => c[3] === "poll")).toHaveLength(1);
});
it("allows only exact provider auth hosts", () => {
  expect(safeAuthUrl("https://claude.com/cai/oauth/authorize", "claude")).toBe(true);
  expect(safeAuthUrl("https://claude.ai/oauth/authorize", "claude")).toBe(true);
  for (const value of ["http://claude.ai/login", "https://claude.com.evil.example/", "https://claude.ai.evil.example/", "https://evil@claude.ai/", "https://claude.ai:444/", "javascript:alert(1)", "https://auth.openai.com/"]) expect(safeAuthUrl(value, "claude")).toBe(false);
});
