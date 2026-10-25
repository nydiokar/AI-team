// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { SessionAccountPanel } from "./SessionAccountPanel";
const mocks = vi.hoisted(() => ({ status: vi.fn(), sheet: vi.fn() }));
vi.mock("../../hooks/useBackendAccountStatus", () => ({ useBackendAccountStatus: mocks.status }));
vi.mock("../../hooks/useLiveData", () => ({ useTargets: () => ({ data: [
  { id: "kanebra", live: true, backends: ["claude"] },
  { id: "Horse", live: true, backends: ["claude"] },
] }) }));
vi.mock("../../stores/authStore", () => ({ useAuthStore: (select: (s: { token: string }) => string) => select({ token: "test" }) }));
vi.mock("../home/BackendAccountSheet", () => ({ BackendAccountSheet: (props: object) => { mocks.sheet(props); return <div>Login panel</div>; } }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
it("shows canonical signed-out state and opens login only for the session carrier", () => {
  mocks.status.mockReturnValue({ data: { state: "signed_out" } });
  const rootElement = document.createElement("div");
  const root = createRoot(rootElement);
  const onOpen = vi.fn();
  try {
    act(() => root.render(<SessionAccountPanel backend="claude" nodeId="kanebra" open={false} onOpen={onOpen} onClose={vi.fn()} />));
    expect(rootElement.textContent).toContain("Claude · kanebra · Signed out — sign in to work");
    expect(mocks.status).toHaveBeenCalledWith("test", "kanebra", "claude", true);
    act(() => rootElement.querySelector("button")!.click());
    expect(onOpen).toHaveBeenCalledOnce();
    act(() => root.render(<SessionAccountPanel backend="claude" nodeId="kanebra" open onOpen={onOpen} onClose={vi.fn()} />));
    expect(mocks.sheet.mock.calls.at(-1)![0].nodes.map((n: { id: string }) => n.id)).toEqual(["kanebra"]);
  } finally { act(() => root.unmount()); }
});
