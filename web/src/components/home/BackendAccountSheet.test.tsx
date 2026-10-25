// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Target } from "../../domain/models";
const mocks = vi.hoisted(() => ({ status: vi.fn(), auth: vi.fn(), copy: vi.fn() }));
vi.mock("../../lib/clipboard", () => ({ copyTextToClipboard: mocks.copy }));
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
  mocks.copy.mockResolvedValue(undefined);
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
  mocks.auth.mockImplementation((_t, _id, _b, action) => Promise.resolve({ login_id: "a".repeat(32), status: "running", output: action === "poll" ? "https://auth.openai.com/codex/device\nABCD-12345 https://evil.example/login" : "", offset: 100 }));
  render("codex"); await flush(); await click("Log in"); await flush();
  expect(document.querySelector("input")).toBeNull();
  expect(document.querySelectorAll('a[href="https://auth.openai.com/codex/device"]')).toHaveLength(1);
  expect(document.querySelector('a[href="https://evil.example/login"]')).toBeNull();
  expect(document.body.textContent).toContain("ABCD-12345");
  await click("Cancel login"); await flush();
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(mocks.auth.mock.calls.filter((c) => c[3] === "poll")).toHaveLength(1);
});
it("allows only exact provider auth hosts", () => {
  expect(safeAuthUrl("https://claude.com/cai/oauth/authorize", "claude")).toBe(true);
  expect(safeAuthUrl("https://claude.ai/oauth/authorize", "claude")).toBe(true);
  for (const value of ["http://claude.ai/login", "https://claude.ai.evil.example/", "https://evil@claude.ai/", "https://claude.ai:444/", "javascript:alert(1)", "https://auth.openai.com/"]) expect(safeAuthUrl(value, "claude")).toBe(false);
});

async function enterCode(value: string) {
  await act(async () => {
    const input = document.querySelector("input")!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const submitCode = async () => {
  await act(async () => { document.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
};
it("offers copy and open actions for the provider link, including streamed output", async () => {
  let polls = 0;
  const url = "https://claude.com/cai/oauth/authorize?state=test&code_challenge=abc";
  mocks.auth.mockImplementation((_t, _id, _b, action) => Promise.resolve({
    login_id: "a".repeat(32), status: "running",
    output: action === "poll" ? (++polls === 1 ? "\x1b[32mOpen https://claude.com/cai/" : "oauth/authorize?state=test&code_challenge=abc\x1b[0m\nPaste code:") : "", offset: polls * 100,
  }));
  render(); await flush(); await click("Log in"); await flush();
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  const link = document.querySelector<HTMLAnchorElement>('a[aria-label="Open link in a new tab"]')!;
  expect(link.href).toBe(url);
  expect(link.target).toBe("_blank");
  expect(link.textContent).toContain("Open link");
  await click("Copy link");
  expect(mocks.copy).toHaveBeenCalledWith(url);
  expect(document.body.textContent).toContain("Copied");
  expect(document.body.textContent).not.toContain(url);
});
it("keeps Claude code entry available for older workers and submits once to the selected node", async () => {
  let finish: () => void = () => undefined;
  mocks.auth.mockImplementation((_t, _id, _b, action) => action === "input"
    ? new Promise((resolve) => { finish = () => resolve({ ok: true }); })
    : Promise.resolve({ login_id: "a".repeat(32), status: "running" }));
  render(); await flush(); await click("Log in"); await flush();
  expect(document.querySelector("input")).not.toBeNull();
  const send = document.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  expect(send.disabled).toBe(true);
  await enterCode("   "); await submitCode();
  expect(mocks.auth.mock.calls.some((c) => c[3] === "input")).toBe(false);
  await enterCode(" code#state "); await submitCode(); await submitCode();
  expect(mocks.auth.mock.calls.filter((c) => c[3] === "input")).toEqual([
    ["test", "kanebra", "claude", "input", { login_id: "a".repeat(32), input: "code#state\n" }],
  ]);
  expect(document.body.textContent).toContain("Sending");
  await act(async () => { finish(); });
  expect(document.querySelector("input")!.value).toBe("");
  expect(document.body.textContent).toContain("Code sent");
  expect(document.body.textContent).not.toContain("Signed in");
});
it("preserves a failed code for retry and clears the error after sending", async () => {
  render(); await flush(); await click("Log in"); await flush();
  await enterCode("retry-code");
  mocks.auth.mockRejectedValueOnce(new Error("offline"));
  await submitCode();
  expect(document.querySelector("input")!.value).toBe("retry-code");
  expect(document.querySelector('[role="alert"]')!.textContent).toContain("Could not send");
  await submitCode();
  expect(document.querySelector('[role="alert"]')).toBeNull();
  expect(document.body.textContent).toContain("Code sent");
});
it("reports clipboard failure without interrupting code entry", async () => {
  mocks.copy.mockRejectedValue(new Error("denied"));
  mocks.auth.mockResolvedValue({ login_id: "a".repeat(32), status: "running", output: "https://claude.ai/oauth/authorize" });
  render(); await flush(); await click("Log in"); await flush(); await click("Copy link");
  expect(document.body.textContent).toContain("Could not copy");
  expect(document.querySelector("input")).not.toBeNull();
});
it("uses explicit worker input capabilities for device and code flows", async () => {
  mocks.auth.mockResolvedValue({ login_id: "a".repeat(32), status: "running", input_supported: true });
  render("codex"); await flush(); await click("Log in"); await flush();
  expect(document.querySelector("input")).not.toBeNull();
});
it("reports an unsuccessful exit and removes expired login controls", async () => {
  mocks.auth.mockImplementation((_t, _id, _b, action) => Promise.resolve({
    login_id: "a".repeat(32), status: action === "poll" ? "exited" : "running", authenticated: false,
    output: "https://claude.ai/oauth/authorize", offset: 40,
  }));
  render(); await flush(); await click("Log in"); await flush();
  expect(document.querySelector("input")).toBeNull();
  expect(document.querySelector("a")).toBeNull();
  expect(document.body.textContent).toContain("Login was not completed");
  expect(document.body.textContent).toContain("Signed out");
});
