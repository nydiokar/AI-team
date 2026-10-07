// @vitest-environment jsdom
/**
 * [A82 Stage 6 · A99 redesign] Turn-queue component with a mocked network (no
 * backend, no paid model). Covers the operator directives: the collapsed thin
 * indicator (D3), waiting-only rows (D1/D6), render-nothing-when-empty (D2),
 * sender identity (D5), a focused + expanded editor (D7), the two-step withdraw
 * confirm (D8), the relocated Pause/Resume (D9), and recovery resolution
 * including `requeue` (defect F1). Plus the timeline dedup that puts an active
 * turn back in the chat (D1 root-cause fix).
 */
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TurnRequestDetail, TurnRequestPage, TurnRequestSummary } from "../../transport/apiClient";

const apiMock = vi.hoisted(() => ({
  turnRequest: vi.fn(),
  editTurnRequest: vi.fn(),
  withdrawTurnRequest: vi.fn(),
  pauseTurnRequests: vi.fn(),
  resumeTurnRequests: vi.fn(),
  resolveTurnRecovery: vi.fn(),
}));

vi.mock("../../transport/apiClient", async (importOriginal) => {
  const real = await importOriginal<typeof import("../../transport/apiClient")>();
  return { ...real, api: { ...real.api, ...apiMock } };
});

import { ApiError } from "../../transport/apiClient";
import { TurnQueuePanel } from "./TurnQueuePanel";
import { useSentStore } from "../../stores/sentStore";
import { useSessionTimeline } from "../../hooks/useSessionTimeline";
import type { RawTranscriptTurn } from "../../transport/rawApi";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function summary(id: string, over: Partial<TurnRequestSummary> = {}): TurnRequestSummary {
  return {
    id, turn_id: id, session_id: "s1", status: "queued", revision: 1, queue_sequence: 1,
    queue_position: 1, turn_source: "human", turn_kind: "instruction", sender_session_id: null,
    blocked_reason: null, created_at: "2026-10-02T00:00:00Z", activated_at: null, started_at: null,
    preview: `preview ${id}`, ...over,
  };
}

function detail(id: string, over: Partial<TurnRequestDetail> = {}): TurnRequestDetail {
  return { ...summary(id), body: `full body ${id}`, completed_at: null, flow_run_id: null, ...over };
}

function page(turns: TurnRequestSummary[], over: Partial<TurnRequestPage> = {}): TurnRequestPage {
  return {
    turns, count: turns.length, queued: turns.filter((t) => t.status === "queued").length,
    active_turn_id: null, active_status: null, next_cursor: null, enrolled: true, paused: false,
    hold: null, ...over,
  };
}

let container: HTMLDivElement;
let root: Root;
let client: QueryClient;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const fn of Object.values(apiMock)) fn.mockReset();
  useSentStore.setState({ bySession: {} });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(node: ReactNode) {
  act(() => {
    root.render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
  });
}

function button(name: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find(
    (b) => b.getAttribute("aria-label") === name || b.textContent?.trim() === name,
  );
  if (!found) throw new Error(`no button ${name}: ${container.innerHTML}`);
  return found as HTMLButtonElement;
}

async function click(el: HTMLElement) {
  await act(async () => {
    el.click();
  });
}

/** The queue is collapsed by default (D3) — open it to reach the rows. */
async function expand() {
  await click(button("Show turn queue"));
}

function typeInto(el: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const owned = (...ids: string[]) => new Set(ids);

describe("TurnQueuePanel — collapse / thin indicator (D2/D3)", () => {
  it("renders nothing when enrolled but nothing is waiting/paused/attention (D2)", () => {
    render(<TurnQueuePanel sessionId="s1" page={page([])} ownedIds={owned()} />);
    expect(container.querySelector("section")).toBeNull();
    expect(container.querySelector("button")).toBeNull();
  });

  it("default is a thin indicator: waiting count + who sent them (D3/D5/D6)", () => {
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page([
          summary("c", { status: "queued" }),
          summary("e", { status: "queued", turn_source: "agent", sender_session_id: "1f9bce3f5a" }),
        ])}
        ownedIds={owned("c", "e")}
      />,
    );
    // Collapsed: a single control, no heavyweight panel.
    expect(container.querySelector("section")).toBeNull();
    const bar = button("Show turn queue");
    expect(bar.textContent).toContain("2");
    expect(bar.textContent).toContain("waiting");
    expect(bar.textContent).toContain("You");
    expect(bar.textContent).toContain("Agent 1f9bce3f");
  });

  it("shows ONLY waiting turns; an active/finished turn never appears here (D1/D6)", async () => {
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page(
          [
            summary("a", { status: "running", queue_position: 1 }),
            summary("c", { status: "queued", queue_position: 2 }),
          ],
          { active_turn_id: "a", active_status: "running" },
        )}
        // Waiting-only ownership from the screen: the running turn is NOT owned.
        ownedIds={owned("c")}
      />,
    );
    await expand();
    const rows = [...container.querySelectorAll("li[data-turn-id]")];
    expect(rows.map((r) => r.getAttribute("data-turn-id"))).toEqual(["c"]);
    expect(container.textContent).not.toContain("running");
  });
});

describe("TurnQueuePanel — read, edit (D7), withdraw (D8)", () => {
  it("opens a row to the full message, then Edit focuses an expanded editor (D4/D7)", async () => {
    apiMock.turnRequest.mockResolvedValueOnce(detail("c", { revision: 1, body: "the full prompt text" }));
    render(<TurnQueuePanel sessionId="s1" page={page([summary("c")])} ownedIds={owned("c")} />);
    await expand();
    await click(button("Open request #1 from You"));
    expect(container.textContent).toContain("the full prompt text");
    await click(button("Edit request #1"));
    const area = container.querySelector("textarea") as HTMLTextAreaElement;
    expect(area.value).toBe("the full prompt text");
    // D7: the editor is focused and roomy (not the old tiny, unfocused box).
    expect(document.activeElement).toBe(area);
    expect(area.rows).toBeGreaterThanOrEqual(8);
  });

  it("edit sends the expected revision; 409 refetches and keeps the draft", async () => {
    apiMock.turnRequest.mockResolvedValueOnce(detail("c", { revision: 1 }));
    apiMock.editTurnRequest.mockRejectedValueOnce(new ApiError(409, "conflict"));
    apiMock.turnRequest.mockResolvedValueOnce(detail("c", { revision: 2, body: "someone else's text" }));
    apiMock.editTurnRequest.mockResolvedValueOnce(detail("c", { revision: 3 }));
    render(<TurnQueuePanel sessionId="s1" page={page([summary("c")])} ownedIds={owned("c")} />);
    await expand();
    await click(button("Open request #1 from You"));
    await click(button("Edit request #1"));
    const area = container.querySelector("textarea") as HTMLTextAreaElement;
    expect(area.value).toBe("full body c");
    typeInto(area, "my edit");
    await click(button("Save"));
    expect(apiMock.editTurnRequest).toHaveBeenLastCalledWith(expect.anything(), "c", 1, "my edit");
    expect(container.textContent).toContain("now revision 2");
    expect((container.querySelector("textarea") as HTMLTextAreaElement).value).toBe("my edit");
    await click(button("Save"));
    expect(apiMock.editTurnRequest).toHaveBeenLastCalledWith(expect.anything(), "c", 2, "my edit");
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("409 because the request already started: never re-sent over the running prompt", async () => {
    apiMock.turnRequest.mockResolvedValueOnce(detail("c"));
    apiMock.editTurnRequest.mockRejectedValueOnce(new ApiError(409, "consumed"));
    apiMock.turnRequest.mockResolvedValueOnce(detail("c", { status: "running", revision: 1 }));
    render(<TurnQueuePanel sessionId="s1" page={page([summary("c")])} ownedIds={owned("c")} />);
    await expand();
    await click(button("Open request #1 from You"));
    await click(button("Edit request #1"));
    typeInto(container.querySelector("textarea") as HTMLTextAreaElement, "too late");
    await click(button("Save"));
    expect(container.textContent).toContain("Already started");
    const area = container.querySelector("textarea") as HTMLTextAreaElement;
    expect(area.value).toBe("too late");
    expect(area.readOnly).toBe(true);
    expect(button("Save").disabled).toBe(true);
    expect(apiMock.editTurnRequest).toHaveBeenCalledTimes(1);
  });

  it("Escape leaves the editor without saving (keyboard)", async () => {
    apiMock.turnRequest.mockResolvedValueOnce(detail("c"));
    render(<TurnQueuePanel sessionId="s1" page={page([summary("c")])} ownedIds={owned("c")} />);
    await expand();
    await click(button("Open request #1 from You"));
    await click(button("Edit request #1"));
    const area = container.querySelector("textarea") as HTMLTextAreaElement;
    await act(async () => {
      area.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(container.querySelector("textarea")).toBeNull();
    expect(apiMock.editTurnRequest).not.toHaveBeenCalled();
  });

  it("withdraw affects only the selected item, after confirmation, and drops its optimistic bubble (D8)", async () => {
    apiMock.turnRequest.mockResolvedValueOnce(detail("c", { revision: 4 }));
    apiMock.withdrawTurnRequest.mockResolvedValueOnce(summary("c", { status: "withdrawn", revision: 5 }));
    useSentStore.setState({
      bySession: { s1: [
        { id: "m1", sessionId: "s1", text: "c", createdAt: "t", delivery: "acknowledged", taskId: "c" },
        { id: "m2", sessionId: "s1", text: "e", createdAt: "t", delivery: "acknowledged", taskId: "e" },
      ] },
    });
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page([summary("c", { revision: 4, queue_position: 1 }), summary("e", { queue_position: 2 })])}
        ownedIds={owned("c", "e")}
      />,
    );
    await expand();
    await click(button("Open request #1 from You"));
    await click(button("Withdraw request #1"));
    expect(apiMock.withdrawTurnRequest).not.toHaveBeenCalled();
    await click(button("Confirm withdraw"));
    expect(apiMock.withdrawTurnRequest).toHaveBeenCalledTimes(1);
    expect(apiMock.withdrawTurnRequest).toHaveBeenCalledWith(expect.anything(), "c", 4);
    expect(useSentStore.getState().bySession.s1.map((m) => m.taskId)).toEqual(["e"]);
  });
});

describe("TurnQueuePanel — pause (D9), recovery incl. requeue (F1), errors", () => {
  it("pause/resume lives in the expanded header and toggles the hold", async () => {
    apiMock.resumeTurnRequests.mockResolvedValueOnce({ session_id: "s1", paused: false, hold: null });
    render(<TurnQueuePanel sessionId="s1" page={page([], { paused: true })} ownedIds={owned()} />);
    // Collapsed bar still flags the paused state so it's discoverable.
    expect(button("Show turn queue").textContent).toContain("paused");
    await expand();
    expect(container.textContent).toContain("Queue paused");
    await click(button("Resume"));
    expect(apiMock.resumeTurnRequests).toHaveBeenCalledWith(expect.anything(), "s1");
    expect(apiMock.pauseTurnRequests).not.toHaveBeenCalled();
  });

  it("an operator-stop hold with nothing waiting shows Resume, never Pause", async () => {
    apiMock.resumeTurnRequests.mockResolvedValueOnce({ session_id: "s1", paused: false, hold: null });
    render(
      <TurnQueuePanel sessionId="s1" page={page([], { paused: false, hold: "operator_stop" })} ownedIds={owned()} />,
    );
    await expand();
    expect(container.textContent).toContain("Stopped by operator");
    expect(button("Resume")).toBeTruthy();
    expect([...container.querySelectorAll("button")].some((b) => b.textContent?.trim() === "Pause")).toBe(false);
  });

  it("[Stage 8a] failed post-commit effects surface with a drill-down", async () => {
    apiMock.turnRequest.mockResolvedValueOnce(detail("t9", { effects_error: "history write timed out" }));
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page([], { effects_failed: 2, effects_failed_turn_id: "t9" })}
        ownedIds={owned()}
      />,
    );
    await expand();
    expect(container.textContent).toContain("2 finished turns: reply delivery failed");
    await click(button("View error"));
    expect(container.textContent).toContain("history write timed out");
  });

  it("recovery_required resolves to cancel/fail only, gated on an acknowledgement", async () => {
    apiMock.resolveTurnRecovery.mockResolvedValueOnce({ ok: true, task_id: "d", status: "failed" });
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page(
          [summary("d", { status: "recovery_required", blocked_reason: "carrier lost" })],
          { active_turn_id: "d", active_status: "recovery_required" },
        )}
        ownedIds={owned()}
      />,
    );
    await expand();
    await click(button("Resolve…"));
    // requeue is NOT offered after start.
    expect([...container.querySelectorAll("button")].some((b) => b.textContent?.trim() === "Requeue")).toBe(false);
    expect(button("Mark failed").disabled).toBe(true);
    const box = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    await click(box);
    await click(button("Mark failed"));
    expect(apiMock.resolveTurnRecovery).toHaveBeenCalledWith(expect.anything(), "d", "failed", expect.any(String), true);
  });

  it("a never-started claim that stalled offers Requeue with no acknowledgement (F1)", async () => {
    apiMock.resolveTurnRecovery.mockResolvedValueOnce({ ok: true, task_id: "k", status: "queued" });
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page(
          [summary("k", { status: "claimed", blocked_reason: "carrier_offline: worker-a" })],
          { active_turn_id: "k", active_status: "claimed" },
        )}
        ownedIds={owned()}
      />,
    );
    await expand();
    await click(button("Resolve…"));
    // No acknowledgement checkbox for a safe requeue.
    expect(container.querySelector("input[type=checkbox]")).toBeNull();
    await click(button("Requeue"));
    expect(apiMock.resolveTurnRecovery).toHaveBeenCalledWith(expect.anything(), "k", "requeue", expect.any(String), false);
  });

  it("a non-409 failure keeps state and says so (no silent success)", async () => {
    apiMock.pauseTurnRequests.mockRejectedValueOnce(new ApiError(503, "db_unavailable"));
    render(<TurnQueuePanel sessionId="s1" page={page([summary("c")])} ownedIds={owned("c")} />);
    await expand();
    await click(button("Pause"));
    expect(container.querySelector("[role=alert]")?.textContent).toContain("Not saved");
  });
});

describe("useSessionTimeline × queue cards (D1 root-cause fix)", () => {
  function Probe({ turns, queueIds }: { turns: RawTranscriptTurn[]; queueIds: ReadonlySet<string> }) {
    const items = useSessionTimeline("s1", undefined, turns, [], queueIds);
    return (
      <ul>
        {items.map((it) => (
          <li key={it.kind === "message" ? it.message.id : it.at}>
            {it.kind === "message" ? it.message.text : it.kind}
          </li>
        ))}
      </ul>
    );
  }

  const turns: RawTranscriptTurn[] = [
    { task_id: "done", timestamp: "t", success: true, status: "completed", instruction: "old ask", result: "old answer", file_count: 0, usage: null },
    { task_id: "run", timestamp: "t", success: true, status: "running", instruction: "running ask", result: "", file_count: 0, usage: null },
  ];

  it("a WAITING prompt is owned by its card; acknowledged waiting sends reconcile by id", () => {
    useSentStore.setState({
      bySession: { s1: [
        { id: "m1", sessionId: "s1", text: "queued text", createdAt: "t", delivery: "acknowledged", taskId: "q1" },
        { id: "m2", sessionId: "s1", text: "still sending", createdAt: "t", delivery: "sending", taskId: null },
      ] },
    });
    // Waiting-only ownership: only the queued turn id q1 is owned.
    render(<Probe turns={turns} queueIds={new Set(["q1"])} />);
    const text = container.textContent ?? "";
    expect(text).toContain("old ask");
    expect(text).toContain("old answer");
    expect(text).not.toContain("queued text"); // owned by its "Waiting" card
    expect(text).toContain("still sending"); // not yet acknowledged: optimistic bubble
  });

  it("an ACTIVE turn is no longer hidden by the queue — it renders in the chat (D1)", () => {
    useSentStore.setState({ bySession: {} });
    // The running turn is NOT in the waiting-only queueIds, so its instruction
    // bubble appears in the transcript instead of vanishing into the queue.
    render(<Probe turns={turns} queueIds={new Set(["q1"])} />);
    expect(container.textContent).toContain("running ask");
  });
});
