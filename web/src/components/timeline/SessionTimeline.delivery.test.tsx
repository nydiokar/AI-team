// @vitest-environment jsdom
/**
 * Per-message delivery indicator: a just-sent user bubble must tell the operator
 * their message is being processed (sending → sent → failed) so the screen never
 * looks frozen after a send. Transcript bubbles (no delivery) stay clean.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SessionTimeline } from "./SessionTimeline";
import type { TimelineItem, MessageDelivery } from "../../fixtures/timeline";

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function userItem(delivery?: MessageDelivery): TimelineItem {
  return {
    kind: "message",
    at: "2026-10-14T17:00:00Z",
    message: {
      id: "opt-1",
      sessionId: "s1",
      role: "user",
      text: "do the thing",
      createdAt: "2026-10-14T17:00:00Z",
    },
    ...(delivery ? { delivery } : {}),
  };
}

function render(items: TimelineItem[]) {
  act(() => root.render(<SessionTimeline items={items} />));
}

describe("SessionTimeline per-message delivery", () => {
  it("shows 'Sending…' while the send is in flight", () => {
    render([userItem("sending")]);
    expect(container.textContent).toContain("Sending…");
  });

  it("shows 'Sent' once acknowledged", () => {
    render([userItem("acknowledged")]);
    expect(container.textContent).toContain("Sent");
  });

  it("shows 'Not delivered' on rejection", () => {
    render([userItem("rejected")]);
    expect(container.textContent).toContain("Not delivered");
  });

  it("renders no delivery status for a transcript bubble (no delivery)", () => {
    render([userItem(undefined)]);
    expect(container.textContent).not.toContain("Sending…");
    expect(container.textContent).not.toContain("Sent");
    expect(container.textContent).not.toContain("Not delivered");
  });
});
