// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import { useSessionTimeline } from "./useSessionTimeline";
import { SessionTimeline } from "../components/timeline/SessionTimeline";
import type { RawTranscriptTurn } from "../transport/rawApi";

function Chat({ turn }: { turn: RawTranscriptTurn }) {
  const items = useSessionTimeline("s", undefined, [turn]);
  return <SessionTimeline items={items} />;
}

it("renders and updates one job system notice without user or agent bubbles", () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  const turn: RawTranscriptTurn = {
    kind: "system_notice", task_id: "job_1", timestamp: "2026-11-05T13:08:00Z",
    success: true, instruction: "", result: "Watched job certification done.",
    file_count: 0, usage: null,
  };
  try {
    act(() => root.render(<Chat turn={turn} />));
    expect(container.textContent).toContain(turn.result);
    expect(container.textContent).not.toContain("You");
    expect(container.textContent).not.toContain("Agent");
    act(() => root.render(<Chat turn={{ ...turn, result: `${turn.result} Result consumed.` }} />));
    expect(container.textContent?.match(/Watched job/g)).toHaveLength(1);
    expect(container.textContent).toContain("Result consumed");
  } finally {
    act(() => root.unmount());
  }
});
