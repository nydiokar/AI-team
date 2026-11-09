// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, expect, it, vi } from "vitest";
import { Composer } from "./Composer";
import { SessionTimeline } from "./SessionTimeline";
import { SessionStatusChip } from "../ui/StatusChip";

const mocks = vi.hoisted(() => ({
  stop: { mutate: vi.fn(), isPending: false, isSuccess: false, isError: false,
    variables: "s", data: { cancelled: true, task_id: "first" } },
}));
vi.mock("../../hooks/useSessionActions", () => ({
  useSubmitInstruction: () => ({}),
  useStopSession: () => mocks.stop,
  useUploadFile: () => ({}),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => {
  Object.assign(mocks.stop, { isPending: false, isSuccess: false, isError: false });
  mocks.stop.mutate.mockClear();
});

it("shows request, acknowledgement and failure without claiming the agent has stopped", () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  const render = () => act(() => root.render(<Composer sessionId="s" running />));
  try {
    render();
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="Stop running task"]')!.click());
    expect(mocks.stop.mutate).toHaveBeenCalledWith("s");
    mocks.stop.isPending = true;
    render();
    expect(container.querySelector('[role="status"]')?.textContent).toContain("Requesting stop");
    expect(container.querySelector<HTMLButtonElement>('[aria-label="Stop running task"]')!.disabled).toBe(true);
    Object.assign(mocks.stop, { isPending: false, isSuccess: true });
    render();
    expect(container.textContent).toContain("waiting for the agent to confirm");
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="Retry stop request"]')!.click());
    expect(mocks.stop.mutate).toHaveBeenCalledTimes(2);
    Object.assign(mocks.stop, { isSuccess: false, isError: true });
    render();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("may still be running");
  } finally { act(() => root.unmount()); }
});

it("restores stopping feedback from the server and clears it on terminal confirmation", () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    act(() => root.render(<Composer sessionId="s" running stopping />));
    expect(container.textContent).toContain("Stop requested");
    act(() => root.render(<Composer sessionId="s" running={false} />));
    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(container.querySelector('[aria-label="Upload file"]')).not.toBeNull();
    Object.assign(mocks.stop, { isSuccess: true });
    act(() => root.render(<Composer sessionId="s" running activeTurnId="second" />));
    expect(container.querySelector('[role="status"]')).toBeNull();
    act(() => root.render(<Composer sessionId="other" running />));
    expect(container.querySelector('[role="status"]')).toBeNull();
  } finally { act(() => root.unmount()); }
});

it("keeps stopping visible when older tool activity is still arriving", () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    act(() => root.render(<>
      <SessionStatusChip state="running" stopping />
      <SessionTimeline liveActivity="Using Bash" items={[{
        kind: "task_state", at: "", taskId: "first", state: "running", stopping: true,
        objective: "Stop requested — waiting for the agent to confirm…",
      }]} />
    </>));
    expect(container.textContent).toContain("Stop requested");
    expect(container.textContent).toContain("Stopping");
    expect(container.textContent).not.toContain("Using Bash");
    expect(container.textContent).not.toContain("Running");
  } finally { act(() => root.unmount()); }
});
