// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { useDraftStore } from "../../stores/draftStore";
import { Composer } from "./Composer";
const mocks = vi.hoisted(() => ({ submit: vi.fn() }));
vi.mock("../../hooks/useSessionActions", () => ({
  useSubmitInstruction: () => ({ mutate: mocks.submit, isPending: false }),
  useStopSession: () => ({}), useUploadFile: () => ({}),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
it("opens account login without sending /login to the agent", () => {
  useDraftStore.getState().setDraft("login-session", "/login");
  const onLogin = vi.fn();
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    act(() => root.render(<Composer sessionId="login-session" running={false} onLogin={onLogin} />));
    act(() => container.querySelector<HTMLButtonElement>('button[aria-label="Send"]')!.click());
    expect(onLogin).toHaveBeenCalledOnce();
    expect(mocks.submit).not.toHaveBeenCalled();
    expect(useDraftStore.getState().bySession["login-session"]).toBeUndefined();
  } finally { act(() => root.unmount()); }
});
