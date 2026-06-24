/**
 * Composer (UI-2) — the live send surface. Replaces the UI-1 disabled stub.
 *
 * Flow (spec §9.2 optimistic + ack): on send we (1) push an optimistic user
 * message into the sent store with a client id that DOUBLES as the idempotency
 * key, (2) fire the submit mutation, (3) reconcile the optimistic message's
 * delivery state from the mutation result (acknowledged → carries the task_id;
 * rejected → shows the stable reason). A failed send keeps the typed text so the
 * user can retry — the same key dedupes server-side.
 *
 * Stop is shown instead of attachments while a task is in flight, wired to
 * useStopSession (gate: "stop/retry work").
 */
import { useState } from "react";
import { ArrowUp, Square, Plus } from "lucide-react";
import { Button } from "../ui/Button";
import { newIdempotencyKey } from "../../transport/apiClient";
import { useSubmitInstruction, useStopSession } from "../../hooks/useSessionActions";
import { useSentStore } from "../../stores/sentStore";

export function Composer({
  sessionId,
  running,
}: {
  sessionId: string;
  running: boolean;
}) {
  const [text, setText] = useState("");
  const submit = useSubmitInstruction();
  const stop = useStopSession();
  const addSent = useSentStore((s) => s.add);
  const updateSent = useSentStore((s) => s.update);

  const send = () => {
    const body = text.trim();
    if (!body || submit.isPending) return;
    const id = newIdempotencyKey();
    addSent({
      id,
      sessionId,
      text: body,
      createdAt: new Date().toISOString(),
      delivery: "sending",
      taskId: null,
    });
    setText("");
    submit.mutate(
      { description: body, sessionId, idempotencyKey: id },
      {
        onSuccess: (res) =>
          updateSent(id, { delivery: "acknowledged", taskId: res.task_id }),
        onError: () => {
          updateSent(id, { delivery: "rejected" });
          setText(body); // restore so the user can retry (same idempotency seam)
        },
      },
    );
  };

  const rejected = submit.isError;

  return (
    <div
      className="border-t border-hairline bg-surface-1/90 px-3 py-2.5 backdrop-blur-xl"
      style={{ paddingBottom: "max(0.625rem, env(safe-area-inset-bottom))" }}
    >
      {rejected && (
        <p className="mb-1.5 px-1 text-[11px] text-bad">
          Send failed: {String(submit.error?.message ?? "unknown")}. Tap send to retry.
        </p>
      )}
      <div className="flex items-end gap-2">
        {running ? (
          <button
            onClick={() => stop.mutate(sessionId)}
            disabled={stop.isPending}
            className="flex size-11 shrink-0 items-center justify-center rounded-full border border-bad/50 text-bad hover:bg-bad/10 disabled:opacity-50"
            aria-label="Stop running task"
          >
            <Square className="size-4" fill="currentColor" />
          </button>
        ) : (
          <button
            disabled
            className="flex size-11 shrink-0 items-center justify-center rounded-full border border-hairline text-ink-muted opacity-50"
            aria-label="Attachments (UI-4)"
          >
            <Plus className="size-5" />
          </button>
        )}
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={running ? "Task running…" : "Send an instruction…"}
          className="h-11 flex-1 rounded-full border border-hairline bg-base px-4 text-sm text-ink outline-none placeholder:text-ink-muted focus:border-accent/50"
        />
        <Button
          size="icon"
          aria-label="Send"
          disabled={!text.trim() || submit.isPending}
          onClick={send}
        >
          <ArrowUp className="size-5" />
        </Button>
      </div>
    </div>
  );
}
