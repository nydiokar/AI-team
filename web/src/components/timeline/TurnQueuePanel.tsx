import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "../../stores/authStore";
import { api, ApiError, type TurnRequestPage, type TurnRequestSummary } from "../../transport/apiClient";

function turnLabel(turn: TurnRequestSummary): string {
  if (turn.status === "queued") return "Next up";
  if (turn.status === "pending" || turn.status === "claimed") return "Starting";
  if (turn.status === "running") return "Working";
  if (turn.status === "recovery_required") return "Recovery required";
  return turn.status;
}

export function TurnQueuePanel({ sessionId, page, transcriptIds }: {
  sessionId: string;
  page: TurnRequestPage;
  transcriptIds: ReadonlySet<string>;
}) {
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const visible = page.turns.filter((turn) => !transcriptIds.has(turn.id));
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["session-turn-queue", sessionId] });
  };
  const startEdit = async (turn: TurnRequestSummary) => {
    setError(null);
    try {
      const detail = await api.turnRequest(token, turn.id);
      setBody(detail.body);
      setEditing(turn.id);
    } catch {
      setError("Could not load the request. Try again.");
    }
  };
  const mutate = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      setEditing(null);
      await refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        await refresh();
        setError("This request changed. Review its current version before saving.");
      } else {
        setError("The change was not saved. Try again.");
      }
    } finally {
      setBusy(false);
    }
  };
  if (!page.enrolled) return null;
  return (
    <section className="max-h-[35vh] overflow-y-auto border-t border-hairline bg-surface-1 px-4 py-3" aria-label="Turn queue">
      <div className="flex items-center justify-between gap-3 text-sm">
        <strong>Turn queue · {page.count}</strong>
        <button type="button" disabled={busy} className="text-ink-soft underline"
          onClick={() => void mutate(() => page.paused
            ? api.resumeTurnRequests(token, sessionId)
            : api.pauseTurnRequests(token, sessionId))}>
          {page.paused ? "Resume queue" : "Pause queue"}
        </button>
      </div>
      {page.hold && <p className="mt-1 text-xs text-ink-muted">Held: {page.hold}</p>}
      {error && <p role="alert" className="mt-2 text-xs text-warn">{error}</p>}
      {visible.map((turn) => {
        const editable = turn.status === "queued" &&
          (turn.turn_source === "human" || turn.turn_source === "operator") &&
          turn.turn_kind === "instruction";
        return <div key={turn.id} className="mt-2 rounded-lg border border-hairline p-3 text-sm">
          <div className="flex justify-between gap-3">
            <span className="font-medium">{turnLabel(turn)} · #{turn.queue_sequence}</span>
            <span className="text-xs text-ink-muted">{turn.turn_source}</span>
          </div>
          {editing === turn.id ? (
            <div className="mt-2">
              <textarea aria-label="Edit queued request" value={body}
                onChange={(event) => setBody(event.target.value)}
                className="w-full rounded border border-hairline bg-surface-0 p-2" rows={4} />
              <button type="button" disabled={busy || !body.trim()} className="mr-3 underline"
                onClick={() => void mutate(() => api.editTurnRequest(token, turn.id, turn.revision, body))}>Save</button>
              <button type="button" disabled={busy} className="underline" onClick={() => setEditing(null)}>Cancel</button>
            </div>
          ) : (
            <p className="mt-1 whitespace-pre-wrap break-words text-ink-soft">{turn.preview}</p>
          )}
          {turn.blocked_reason && <p className="mt-1 text-xs text-ink-muted">Waiting: {turn.blocked_reason}</p>}
          {editable && editing !== turn.id && <div className="mt-2 flex gap-3 text-xs">
            <button type="button" disabled={busy} className="underline" onClick={() => void startEdit(turn)}>Edit</button>
            <button type="button" disabled={busy} className="underline"
              onClick={() => void mutate(() => api.withdrawTurnRequest(token, turn.id, turn.revision))}>Withdraw</button>
          </div>}
        </div>;
      })}
    </section>
  );
}
