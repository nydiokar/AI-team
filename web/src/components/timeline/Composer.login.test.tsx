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
it("submits session instructions without opening account management", () => {
  useDraftStore.getState().setDraft("login-session", "/login");
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    act(() => root.render(<Composer sessionId="login-session" running={false} />));
    act(() => container.querySelector<HTMLButtonElement>('button[aria-label="Send"]')!.click());
    expect(mocks.submit).toHaveBeenCalledOnce();
    expect(mocks.submit.mock.calls[0][0]).toMatchObject({ sessionId: "login-session", description: "/login" });
    expect(useDraftStore.getState().bySession["login-session"]).toBeUndefined();
  } finally { act(() => root.unmount()); }
});
