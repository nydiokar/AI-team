// @vitest-environment jsdom
/**
 * SystemAlertBanner dismiss: every alert (ongoing or recovered) carries an ×
 * that hides THAT alert for good (per-viewer, persisted); a new alert id still
 * surfaces.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SystemAlert } from "../../domain/systemAlerts";

const alertsMock = vi.hoisted(() => ({ data: [] as SystemAlert[] }));
vi.mock("../../hooks/useSystemAlerts", () => ({ useSystemAlerts: () => alertsMock }));

import { SystemAlertBanner } from "./SystemAlertBanner";
import { useDismissedAlertsStore } from "../../stores/dismissedAlertsStore";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function alert(id: number, resolved: boolean): SystemAlert {
  const now = Date.now();
  return {
    id,
    source: "healthcheck",
    kind: "unresponsive",
    message: "no response",
    detail: "",
    openedAt: new Date(now - 5 * 60_000).toISOString(),
    resolvedAt: resolved ? new Date(now - 60_000).toISOString() : null,
  };
}

let host: HTMLDivElement;
let root: Root;

function render() {
  act(() => root.render(<SystemAlertBanner />));
}

beforeEach(() => {
  useDismissedAlertsStore.setState({ ids: [] });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("SystemAlertBanner dismiss", () => {
  it.each([
    ["recovered", true],
    ["ongoing", false],
  ])("hides a %s alert when its × is clicked", (_label, resolved) => {
    alertsMock.data = [alert(7, resolved)];
    render();
    expect(host.textContent).toContain("Gateway unresponsive");
    const close = host.querySelector<HTMLButtonElement>('button[aria-label="Dismiss alert"]');
    expect(close).not.toBeNull();
    act(() => close!.click());
    expect(host.textContent).toBe("");
    expect(useDismissedAlertsStore.getState().ids).toContain(7);
  });

  it("still surfaces a new alert after an older one was dismissed", () => {
    useDismissedAlertsStore.setState({ ids: [7] });
    alertsMock.data = [alert(8, true)];
    render();
    expect(host.textContent).toContain("recovered after");
  });

  it("bounds the persisted id list", () => {
    const { dismiss } = useDismissedAlertsStore.getState();
    for (let i = 0; i < 120; i++) dismiss(i);
    const ids = useDismissedAlertsStore.getState().ids;
    expect(ids.length).toBeLessThanOrEqual(50);
    expect(ids).toContain(119);
  });
});
