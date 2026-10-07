/**
 * [A82 Stage 6] Queue cards for an ENROLLED session — the managed turn-request
 * read model, separate from the historical exchanges (design §9).
 *
 * - The same durable id moves Waiting → Starting → Working → (terminal: the
 *   transcript exchange takes over; `ownedIds` drops it). "Recovery required"
 *   marks an unresolved execution and offers the operator resolution.
 * - Edit sends the expected revision (If-Match). On 409 the item is refetched;
 *   the operator's draft is kept, and a request that already started is never
 *   overwritten (Save is disabled).
 * - Withdraw affects ONLY the selected waiting item. Pause/Resume is the
 *   persistent operator queue hold; resume never clears recovery/Case/quota
 *   holds (the server decides; the UI just reflects `paused`/`hold`).
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "../../stores/authStore";
import { useSentStore } from "../../stores/sentStore";
import {
  api,
  ApiError,
  type TurnRequestPage,
  type TurnRequestSummary,
} from "../../transport/apiClient";
import {
  blockedReasonLabel,
  effectsFailedLabel,
  isEditableTurn,
  sessionTurnQueueKey,
  turnCardLabel,
  turnSourceLabel,
  type TurnCardLabel,
} from "../../lib/turnQueue";
import { cn } from "../../lib/cn";

const LABEL_TONE: Record<TurnCardLabel, string> = {
  Waiting: "bg-surface-3 text-ink-soft",
  Starting: "bg-accent-dim text-accent",
  Working: "bg-running/15 text-running",
  "Recovery required": "bg-bad/15 text-bad",
  Finished: "bg-surface-3 text-ink-muted",
};

interface EditState {
  turnId: string;
  /** Revision the draft is based on (sent as If-Match). */
  revision: number;
  draft: string;
  /** The request is no longer waiting: editing is closed, the draft is kept. */
  consumed: boolean;
}

export function TurnQueuePanel({
  sessionId,
  page,
  ownedIds,
}: {
  sessionId: string;
  page: TurnRequestPage;
  /** Ids the queue owns (open AND no finished transcript exchange yet). */
  ownedIds: ReadonlySet<string>;
}) {
  const token = useAuthStore((s) => s.token);
  const dropSentTask = useSentStore((s) => s.dropTask);
  const queryClient = useQueryClient();
  const [edit, setEdit] = useState<EditState | null>(null);
  const [confirmWithdraw, setConfirmWithdraw] = useState<string | null>(null);
  const [resolving, setResolving] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const cards = page.turns.filter((turn) => ownedIds.has(turn.id));
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: sessionTurnQueueKey(sessionId) });

  const run = async (
    action: () => Promise<unknown>,
    onConflict?: () => Promise<void>,
  ): Promise<boolean> => {
    setBusy(true);
    setNotice(null);
    try {
      await action();
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && onConflict) {
        await onConflict();
      } else if (err instanceof ApiError && err.status === 409) {
        setNotice("This request changed meanwhile — showing its current state.");
      } else {
        setNotice("Not saved — check the connection and try again.");
      }
      return false;
    } finally {
      setBusy(false);
      void refresh();
    }
  };

  const startEdit = async (turn: TurnRequestSummary) => {
    setNotice(null);
    try {
      const detail = await api.turnRequest(token, turn.id);
      setEdit({
        turnId: turn.id,
        revision: detail.revision,
        draft: detail.body,
        consumed: detail.status !== "queued",
      });
    } catch {
      setNotice("Could not load the request. Try again.");
    }
  };

  const save = async (state: EditState) => {
    const ok = await run(
      () => api.editTurnRequest(token, state.turnId, state.revision, state.draft),
      async () => {
        // 409: refetch the ONE item. Keep the operator's draft; never re-send
        // over a request that has started.
        try {
          const current = await api.turnRequest(token, state.turnId);
          const consumed = current.status !== "queued";
          setEdit({ ...state, revision: current.revision, consumed });
          setNotice(
            consumed
              ? "Already started — your edit was not applied. Your text is kept below."
              : `Changed elsewhere (now revision ${current.revision}). Your text is kept — review and save again.`,
          );
        } catch {
          setNotice("This request changed. Reload it before saving.");
        }
      },
    );
    if (ok) setEdit(null);
  };

  const withdraw = async (turn: TurnRequestSummary) => {
    setConfirmWithdraw(null);
    const ok = await run(() => api.withdrawTurnRequest(token, turn.id, turn.revision));
    if (ok) dropSentTask(turn.id);
  };

  const resolve = async (turn: TurnRequestSummary, decision: "failed" | "cancelled") => {
    const ok = await run(() =>
      api.resolveTurnRecovery(token, turn.id, decision, "operator via web"),
    );
    if (ok) {
      setResolving(null);
      setAcknowledged(false);
    }
  };

  // An operator-stop hold blocks activation like a pause; Resume releases both.
  const held = page.paused || page.hold != null;
  const holdReason = blockedReasonLabel(page.hold);

  const toggleQueue = () =>
    void run(() =>
      held
        ? api.resumeTurnRequests(token, sessionId)
        : api.pauseTurnRequests(token, sessionId),
    );

  const effectsFailed = effectsFailedLabel(page.effects_failed);

  if (!page.enrolled || (cards.length === 0 && !held && !effectsFailed)) return null;

  return (
    <section
      aria-label="Turn queue"
      className="max-h-[40vh] overflow-y-auto overscroll-contain border-t border-hairline bg-surface-1 px-3 py-2.5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-[13px] font-semibold text-ink">
          Next up
          <span className="ml-1.5 font-normal text-ink-muted">· {page.queued} waiting</span>
        </h2>
        <button
          type="button"
          disabled={busy}
          onClick={toggleQueue}
          aria-pressed={held}
          className="h-9 rounded-full border border-hairline px-3 text-[12px] text-ink-soft hover:bg-surface-2 disabled:opacity-50"
        >
          {held ? "Resume queue" : "Pause queue"}
        </button>
      </div>
      {held && (
        <p className="mt-1 text-[12px] text-warn" title={page.hold ?? undefined}>
          {holdReason ? `${holdReason} — ` : "Queue paused — "}nothing new starts until you resume.
        </p>
      )}
      {effectsFailed && (
        <p className="mt-1 text-[12px] text-bad" title={page.effects_failed_turn_id ?? undefined}>
          {effectsFailed}
        </p>
      )}
      {notice && (
        <p role="alert" className="mt-1.5 text-[12px] text-warn">
          {notice}
        </p>
      )}
      <ol className="mt-1.5 space-y-2" aria-live="polite">
        {cards.map((turn) => {
          const label = turnCardLabel(turn.status);
          const reason = blockedReasonLabel(turn.blocked_reason);
          const editing = edit?.turnId === turn.id ? edit : null;
          const position = turn.queue_position ?? turn.queue_sequence;
          return (
            <li
              key={turn.id}
              data-turn-id={turn.id}
              className="rounded-xl border border-hairline bg-surface-2 p-2.5 text-[13px]"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", LABEL_TONE[label])}
                >
                  {label}
                </span>
                <span className="text-[11px] text-ink-muted">#{position}</span>
                <span className="ml-auto text-[11px] text-ink-muted">{turnSourceLabel(turn)}</span>
              </div>

              {editing ? (
                <div className="mt-2">
                  <textarea
                    aria-label={`Edit request #${position}`}
                    value={editing.draft}
                    readOnly={editing.consumed}
                    onChange={(e) => setEdit({ ...editing, draft: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setEdit(null);
                    }}
                    rows={4}
                    className="w-full resize-y rounded-lg bg-surface-1 p-2 text-[14px] text-ink outline-none ring-1 ring-inset ring-hairline focus:ring-accent/50"
                  />
                  <div className="mt-1.5 flex flex-wrap gap-2">
                    <button
                      type="button"
                      disabled={busy || editing.consumed || !editing.draft.trim()}
                      onClick={() => void save(editing)}
                      className="h-9 rounded-full bg-accent-dim px-3 text-[12px] font-medium text-accent disabled:opacity-40"
                    >
                      Save
                    </button>
                    <button
                      type="button"
                      onClick={() => setEdit(null)}
                      className="h-9 rounded-full px-3 text-[12px] text-ink-soft hover:bg-surface-3"
                    >
                      {editing.consumed ? "Close" : "Cancel"}
                    </button>
                  </div>
                </div>
              ) : (
                <p className="mt-1.5 line-clamp-4 whitespace-pre-wrap break-words text-ink-soft">
                  {turn.preview}
                </p>
              )}

              {reason && (
                <p className="mt-1 text-[11px] text-ink-muted" title={turn.blocked_reason ?? undefined}>
                  {turn.status === "recovery_required" ? "Held: " : "Waiting: "}
                  {reason}
                </p>
              )}

              {isEditableTurn(turn) && !editing && (
                <div className="mt-1.5 flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void startEdit(turn)}
                    aria-label={`Edit request #${position}`}
                    className="h-9 rounded-full px-3 text-[12px] text-ink-soft hover:bg-surface-3"
                  >
                    Edit
                  </button>
                  {confirmWithdraw === turn.id ? (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void withdraw(turn)}
                        className="h-9 rounded-full border border-bad/40 px-3 text-[12px] text-bad"
                      >
                        Confirm withdraw
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmWithdraw(null)}
                        className="h-9 rounded-full px-3 text-[12px] text-ink-soft"
                      >
                        Keep
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => setConfirmWithdraw(turn.id)}
                      aria-label={`Withdraw request #${position}`}
                      className="h-9 rounded-full px-3 text-[12px] text-ink-soft hover:bg-surface-3"
                    >
                      Withdraw
                    </button>
                  )}
                </div>
              )}

              {turn.status === "recovery_required" && (
                <div className="mt-1.5">
                  {resolving === turn.id ? (
                    <div className="space-y-1.5">
                      <label className="flex items-start gap-2 text-[12px] text-ink-soft">
                        <input
                          type="checkbox"
                          checked={acknowledged}
                          onChange={(e) => setAcknowledged(e.target.checked)}
                          className="mt-0.5"
                        />
                        I checked the agent has stopped; its outcome is unproven and it will not be re-run.
                      </label>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          disabled={busy || !acknowledged}
                          onClick={() => void resolve(turn, "failed")}
                          className="h-9 rounded-full border border-bad/40 px-3 text-[12px] text-bad disabled:opacity-40"
                        >
                          Mark failed
                        </button>
                        <button
                          type="button"
                          disabled={busy || !acknowledged}
                          onClick={() => void resolve(turn, "cancelled")}
                          className="h-9 rounded-full border border-hairline px-3 text-[12px] text-ink-soft disabled:opacity-40"
                        >
                          Mark cancelled
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setResolving(null);
                            setAcknowledged(false);
                          }}
                          className="h-9 rounded-full px-3 text-[12px] text-ink-soft"
                        >
                          Not now
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setResolving(turn.id)}
                      className="h-9 rounded-full border border-bad/40 px-3 text-[12px] text-bad"
                    >
                      Resolve…
                    </button>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
