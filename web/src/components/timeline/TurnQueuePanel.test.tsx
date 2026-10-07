// @vitest-environment jsdom
/**
 * [A82 Stage 6] UI01–08 (interaction half): queue cards with a mocked network
 * (no backend, no paid model). Edit carries the expected revision and, on 409,
 * refetches the item without overwriting a started prompt; withdraw targets
 * only the selected item; pause/resume; recovery resolution needs an explicit
 * acknowledgement; keyboard/accessibility; timeline dedup by durable id.
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

function typeInto(el: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const owned = (...ids: string[]) => new Set(ids);

describe("TurnQueuePanel", () => {
  it("renders cards by durable id with conservative labels and only owned ids", () => {
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page([
          summary("a", { status: "running", queue_position: 1 }),
          summary("b", { status: "pending", queue_position: 2 }),
          summary("c", { status: "queued", queue_position: 3 }),
          summary("d", { status: "recovery_required", blocked_reason: "managed_result_oversize: artifact=/x" }),
        ])}
        ownedIds={owned("a", "b", "c", "d")}
      />,
    );
    const cards = [...container.querySelectorAll("li[data-turn-id]")];
    expect(cards.map((c) => c.getAttribute("data-turn-id"))).toEqual(["a", "b", "c", "d"]);
    expect(cards[0].textContent).toContain("Working");
    expect(cards[1].textContent).toContain("Starting");
    expect(cards[2].textContent).toContain("Waiting");
    expect(cards[3].textContent).toContain("Recovery required");
    expect(cards[3].textContent).toContain("Result too large");
    // Only the waiting human item offers edit/withdraw.
    expect(cards[0].textContent).not.toContain("Edit");
    expect(cards[2].textContent).toContain("Edit");
    expect(container.querySelector("section")?.getAttribute("aria-label")).toBe("Turn queue");
    // A finished exchange owns its id: the card yields (no duplicate).
    render(<TurnQueuePanel sessionId="s1" page={page([summary("a", { status: "running" })])} ownedIds={owned()} />);
    expect(container.querySelector("li[data-turn-id]")).toBeNull();
  });

  it("edit sends the expected revision; 409 refetches and keeps the draft", async () => {
    apiMock.turnRequest.mockResolvedValueOnce(detail("c", { revision: 1 }));
    apiMock.editTurnRequest.mockRejectedValueOnce(new ApiError(409, "conflict"));
    apiMock.turnRequest.mockResolvedValueOnce(detail("c", { revision: 2, body: "someone else's text" }));
    apiMock.editTurnRequest.mockResolvedValueOnce(detail("c", { revision: 3 }));
    render(<TurnQueuePanel sessionId="s1" page={page([summary("c")])} ownedIds={owned("c")} />);
    await click(button("Edit request #1"));
    const area = container.querySelector("textarea") as HTMLTextAreaElement;
    expect(area.value).toBe("full body c"); // full intent from the one-item read
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
    await click(button("Edit request #1"));
    typeInto(container.querySelector("textarea") as HTMLTextAreaElement, "too late");
    await click(button("Save"));
    expect(container.textContent).toContain("Already started");
    const area = container.querySelector("textarea") as HTMLTextAreaElement;
    expect(area.value).toBe("too late"); // the operator's text is kept
    expect(area.readOnly).toBe(true);
    expect(button("Save").disabled).toBe(true);
    expect(apiMock.editTurnRequest).toHaveBeenCalledTimes(1);
  });

  it("Escape cancels an edit (keyboard)", async () => {
    apiMock.turnRequest.mockResolvedValueOnce(detail("c"));
    render(<TurnQueuePanel sessionId="s1" page={page([summary("c")])} ownedIds={owned("c")} />);
    await click(button("Edit request #1"));
    const area = container.querySelector("textarea") as HTMLTextAreaElement;
    await act(async () => {
      area.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(container.querySelector("textarea")).toBeNull();
    expect(apiMock.editTurnRequest).not.toHaveBeenCalled();
  });

  it("withdraw affects only the selected item, after confirmation, and drops its optimistic bubble", async () => {
    apiMock.withdrawTurnRequest.mockResolvedValueOnce(summary("c", { status: "withdrawn", revision: 2 }));
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
    await click(button("Withdraw request #1"));
    expect(apiMock.withdrawTurnRequest).not.toHaveBeenCalled();
    await click(button("Confirm withdraw"));
    expect(apiMock.withdrawTurnRequest).toHaveBeenCalledTimes(1);
    expect(apiMock.withdrawTurnRequest).toHaveBeenCalledWith(expect.anything(), "c", 4);
    expect(useSentStore.getState().bySession.s1.map((m) => m.taskId)).toEqual(["e"]);
  });

  it("pause/resume toggles the persistent queue hold and shows the paused state", async () => {
    apiMock.resumeTurnRequests.mockResolvedValueOnce({ session_id: "s1", paused: false, hold: null });
    render(<TurnQueuePanel sessionId="s1" page={page([], { paused: true })} ownedIds={owned()} />);
    expect(container.textContent).toContain("Queue paused");
    expect(button("Resume queue").getAttribute("aria-pressed")).toBe("true");
    await click(button("Resume queue"));
    expect(apiMock.resumeTurnRequests).toHaveBeenCalledWith(expect.anything(), "s1");
    expect(apiMock.pauseTurnRequests).not.toHaveBeenCalled();
  });

  it("an operator-stop hold is visible with no cards and offers Resume, never Pause", async () => {
    // [S6-F4] e.g. a stop that held the session without the pause flag.
    apiMock.resumeTurnRequests.mockResolvedValueOnce({ session_id: "s1", paused: false, hold: null });
    render(
      <TurnQueuePanel sessionId="s1" page={page([], { paused: false, hold: "operator_stop" })} ownedIds={owned()} />,
    );
    expect(container.textContent).toContain("Stopped by operator");
    expect(container.textContent).not.toContain("Pause queue");
    expect(button("Resume queue").getAttribute("aria-pressed")).toBe("true");
    await click(button("Resume queue"));
    expect(apiMock.resumeTurnRequests).toHaveBeenCalledWith(expect.anything(), "s1");
    expect(apiMock.pauseTurnRequests).not.toHaveBeenCalled();
  });

  it("an enrolled session with no cards, no pause and no hold renders nothing", () => {
    render(<TurnQueuePanel sessionId="s1" page={page([])} ownedIds={owned()} />);
    expect(container.querySelector("section")).toBeNull();
  });

  it("[Stage 8a] finished turns whose effects failed are surfaced even with no cards", () => {
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page([], { effects_failed: 2, effects_failed_turn_id: "t9" })}
        ownedIds={owned()}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("2 finished turns: reply delivery failed");
    expect(container.querySelector("[title='t9']")).not.toBeNull();
  });

  it("recovery resolution requires an explicit acknowledgement", async () => {
    apiMock.resolveTurnRecovery.mockResolvedValueOnce({ ok: true, task_id: "d", status: "failed" });
    render(
      <TurnQueuePanel
        sessionId="s1"
        page={page([summary("d", { status: "recovery_required", blocked_reason: "carrier lost" })])}
        ownedIds={owned("d")}
      />,
    );
    await click(button("Resolve…"));
    expect(button("Mark failed").disabled).toBe(true);
    const box = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    await click(box);
    await click(button("Mark failed"));
    expect(apiMock.resolveTurnRecovery).toHaveBeenCalledWith(expect.anything(), "d", "failed", expect.any(String));
  });

  it("a non-409 failure keeps state and says so (no silent success)", async () => {
    apiMock.pauseTurnRequests.mockRejectedValueOnce(new ApiError(503, "db_unavailable"));
    render(<TurnQueuePanel sessionId="s1" page={page([summary("c")])} ownedIds={owned("c")} />);
    await click(button("Pause queue"));
    expect(container.querySelector("[role=alert]")?.textContent).toContain("Not saved");
  });
});

describe("useSessionTimeline × queue cards", () => {
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

  it("a waiting/started prompt is never shown as a consumed exchange; acknowledged sends reconcile by id", () => {
    useSentStore.setState({
      bySession: { s1: [
        { id: "m1", sessionId: "s1", text: "queued text", createdAt: "t", delivery: "acknowledged", taskId: "q1" },
        { id: "m2", sessionId: "s1", text: "still sending", createdAt: "t", delivery: "sending", taskId: null },
      ] },
    });
    const turns: RawTranscriptTurn[] = [
      { task_id: "done", timestamp: "t", success: true, status: "completed", instruction: "old ask", result: "old answer", file_count: 0, usage: null },
      { task_id: "run", timestamp: "t", success: true, status: "running", instruction: "running ask", result: "", file_count: 0, usage: null },
    ];
    render(<Probe turns={turns} queueIds={new Set(["run", "q1"])} />);
    const text = container.textContent ?? "";
    expect(text).toContain("old ask");
    expect(text).toContain("old answer");
    expect(text).not.toContain("running ask"); // owned by its "Working" card
    expect(text).not.toContain("queued text"); // owned by its "Waiting" card
    expect(text).toContain("still sending"); // not yet acknowledged: optimistic bubble
  });
});
