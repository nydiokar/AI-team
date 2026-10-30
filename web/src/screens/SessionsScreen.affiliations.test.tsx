// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
const hooks = vi.hoisted(() => ({ affiliations: vi.fn() }));
vi.mock("../hooks/useWork", () => ({ useSessionAffiliations: hooks.affiliations }));
vi.mock("../hooks/useLiveData", () => ({ useSessions: () => ({ data: [], isLoading: false, error: null }) }));
vi.mock("../components/sessions/NewSessionSheet", () => ({ NewSessionSheet: () => null }));
vi.mock("../components/sessions/SessionRow", () => ({ SessionRow: () => null }));
import { SessionsScreen } from "./SessionsScreen";
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("discloses unknown affiliation reads separately from successful empty membership", () => {
  const el = document.createElement("div");
  const root = createRoot(el);
  const render = () => act(() => root.render(<SessionsScreen />));
  try {
    hooks.affiliations.mockReturnValue({ index: new Map(), isKnown: false, isLoading: true });
    render();
    expect(el.textContent).toContain("Loading case links");
    expect(el.textContent).not.toContain("Standalone");
    hooks.affiliations.mockReturnValue({ index: new Map(), isKnown: false, isLoading: false, error: new Error("offline") });
    render();
    expect(el.textContent).toContain("Case links are unavailable");
    hooks.affiliations.mockReturnValue({ index: new Map(), isKnown: true, isLoading: false });
    render();
    expect(el.textContent).not.toContain("unavailable");
    expect(el.textContent).not.toContain("Loading case links");
  } finally { act(() => root.unmount()); }
});
