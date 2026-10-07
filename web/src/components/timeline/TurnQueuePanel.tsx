/**
 * [A82 Stage 6 · A99 redesign] The session turn queue — the WAITING read model,
 * kept distinct from the chat transcript (design §9/§10).
 *
 * Shape (operator directives D1–D9):
 * - It shows ONLY turns that are still WAITING. A turn that has started, is
 *   working, or has finished lives in the chat, never here — SessionDetailScreen
 *   hands us a waiting-only `ownedIds` set (D1), so an active turn can no longer
 *   hide inside the queue.
 * - Default is a super-thin, barely-there indicator tucked above the composer:
 *   how many are waiting + who sent them (D3). Click to expand.
 * - Expanded is a compact box, one super-thin row per turn (sender + a 1–2 word
 *   preview, D4). Click a row to read the full message in a plain view; from
 *   there Edit opens a focused, roomy editor (D7) and Withdraw keeps its
 *   two-step confirm (D8). Pause/Resume lives in the box header, out of the way
 *   when collapsed (D9).
 * - Nothing renders when there is nothing to act on (no waiting, not paused, no
 *   recovery/effects attention) — no empty panel (D2).
 *
 * Edit sends the expected revision (If-Match). On 409 the item is refetched; the
 * operator's draft is kept and a started request is never overwritten. Withdraw
 * affects only the selected waiting item. Pause/Resume is the persistent operator
 * hold; resume never clears recovery/Case/quota holds (the server decides).
 */
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Pause,
  Play,
} from "lucide-react";
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
  previewWords,
  recoveryOptions,
  senderSummary,
  sessionTurnQueueKey,
  turnSourceLabel,
  type RecoveryOption,
} from "../../lib/turnQueue";
import { cn } from "../../lib/cn";

/** The opened row's reader/editor view (lazily loads the full body). */
interface RowView {
  turnId: string;
  loading: boolean;
  error: boolean;
  /** Full editable intent (one-item read); empty until loaded. */
  body: string;
  /** Revision the draft is based on (sent as If-Match). */
  revision: number;
  /** The request is no longer waiting: editing is closed, the draft is kept. */
  consumed: boolean;
  editing: boolean;
  draft: string;
}

export function TurnQueuePanel({
  sessionId,
  page,
  ownedIds,
}: {
  sessionId: string;
  page: TurnRequestPage;
  /** Ids of turns the queue owns — WAITING ONLY (active/finished live in chat). */
  ownedIds: ReadonlySet<string>;
}) {
  const token = useAuthStore((s) => s.token);
  const dropSentTask = useSentStore((s) => s.dropTask);
  const queryClient = useQueryClient();

  const [expanded, setExpanded] = useState(false);
  const [view, setView] = useState<RowView | null>(null);
  const [confirmWithdraw, setConfirmWithdraw] = useState<string | null>(null);
  const [resolving, setResolving] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [showEffects, setShowEffects] = useState(false);
  const [effectsDetail, setEffectsDetail] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const editorRef = useRef<HTMLTextAreaElement>(null);

  // Waiting-only: the component never shows an active/finished turn (D1/D6).
  const waiting = page.turns.filter((turn) => ownedIds.has(turn.id));
  const waitingCount = waiting.length;

  // An operator-stop hold blocks activation like a pause; Resume releases both.
  const held = page.paused || page.hold != null;
  const holdReason = blockedReasonLabel(page.hold);
  const effectsFailed = effectsFailedLabel(page.effects_failed);

  // The single active slot can need a human: a held (recovery_required) turn, or
  // a never-started claim that stalled (requeue-eligible). Everything else about
  // the active turn is shown in the chat, not here.
  const activeTurn = page.active_turn_id
    ? page.turns.find((t) => t.id === page.active_turn_id)
    : undefined;
  const attentionTurn =
    activeTurn &&
    (activeTurn.status === "recovery_required" ||
      (activeTurn.status === "claimed" && Boolean(activeTurn.blocked_reason)))
      ? activeTurn
      : undefined;
  const needsAttention = Boolean(attentionTurn) || Boolean(effectsFailed);

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

  // Open a waiting row → load its full body for a plain, readable view (D4).
  const openRow = async (turn: TurnRequestSummary) => {
    if (view?.turnId === turn.id) {
      setView(null);
      return;
    }
    setNotice(null);
    setConfirmWithdraw(null);
    setView({
      turnId: turn.id,
      loading: true,
      error: false,
      body: "",
      revision: turn.revision,
      consumed: false,
      editing: false,
      draft: "",
    });
    try {
      const detail = await api.turnRequest(token, turn.id);
      setView((v) =>
        v?.turnId === turn.id
          ? {
              ...v,
              loading: false,
              body: detail.body,
              revision: detail.revision,
              consumed: detail.status !== "queued",
            }
          : v,
      );
    } catch {
      setView((v) => (v?.turnId === turn.id ? { ...v, loading: false, error: true } : v));
    }
  };

  const startEdit = () =>
    setView((v) => (v ? { ...v, editing: true, draft: v.body } : v));

  // Focus the editor when it opens and place the caret at the end (D7).
  useEffect(() => {
    if (!view?.editing) return;
    const el = editorRef.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, [view?.editing, view?.turnId]);

  const save = async (state: RowView) => {
    const ok = await run(
      () => api.editTurnRequest(token, state.turnId, state.revision, state.draft),
      async () => {
        // 409: refetch the ONE item. Keep the operator's draft; never re-send
        // over a request that has started.
        try {
          const current = await api.turnRequest(token, state.turnId);
          const consumed = current.status !== "queued";
          setView({ ...state, revision: current.revision, consumed });
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
    if (ok) setView(null);
  };

  const withdraw = async (turn: TurnRequestSummary) => {
    setConfirmWithdraw(null);
    const ok = await run(() => api.withdrawTurnRequest(token, turn.id, turn.revision));
    if (ok) {
      dropSentTask(turn.id);
      if (view?.turnId === turn.id) setView(null);
    }
  };

  const resolve = async (turn: TurnRequestSummary, option: RecoveryOption) => {
    const ok = await run(() =>
      api.resolveTurnRecovery(
        token,
        turn.id,
        option.decision,
        "operator via web",
        option.requiresAck,
      ),
    );
    if (ok) {
      setResolving(null);
      setAcknowledged(false);
    }
  };

  const viewEffectsError = async () => {
    setShowEffects((s) => !s);
    if (effectsDetail || !page.effects_failed_turn_id) return;
    try {
      const detail = await api.turnRequest(token, page.effects_failed_turn_id);
      setEffectsDetail(detail.effects_error || "No further detail was recorded.");
    } catch {
      setEffectsDetail("Could not load the error detail.");
    }
  };

  const toggleQueue = () =>
    void run(() =>
      held
        ? api.resumeTurnRequests(token, sessionId)
        : api.pauseTurnRequests(token, sessionId),
    );

  // D2 — nothing to act on ⇒ render nothing (no empty panel).
  if (!page.enrolled || (waitingCount === 0 && !held && !needsAttention)) return null;

  // D3 — collapsed default: a super-thin, barely-visible indicator.
  if (!expanded) {
    const senders = senderSummary(waiting);
    return (
      <button
        type="button"
        aria-label="Show turn queue"
        aria-expanded={false}
        onClick={() => setExpanded(true)}
        className={cn(
          "flex w-full items-center gap-2 border-t border-hairline px-3 py-1.5 text-left text-[11px] transition-colors",
          needsAttention
            ? "bg-bad/10 text-bad hover:bg-bad/15"
            : "bg-surface-1/70 text-ink-muted hover:bg-surface-2",
        )}
      >
        <ChevronUp className="size-3.5 shrink-0 opacity-70" />
        {needsAttention ? (
          <span className="flex items-center gap-1.5 font-medium">
            <AlertTriangle className="size-3.5" /> Needs attention
          </span>
        ) : waitingCount > 0 ? (
          <span className="truncate">
            <span className="font-semibold text-ink-soft">{waitingCount}</span> waiting
            {senders && <span className="text-ink-muted"> · {senders}</span>}
          </span>
        ) : (
          <span className="flex items-center gap-1.5 text-warn">
            <Pause className="size-3" /> Queue paused
          </span>
        )}
        {held && (needsAttention || waitingCount > 0) && (
          <span className="ml-auto flex shrink-0 items-center gap-1 text-warn">
            <Pause className="size-3" /> Paused
          </span>
        )}
      </button>
    );
  }

  // D4 — expanded: a compact box (not full-screen), thin rows, pause in the header.
  return (
    <section
      aria-label="Turn queue"
      className="border-t border-hairline bg-surface-1 text-[13px]"
    >
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          aria-label="Hide turn queue"
          aria-expanded
          onClick={() => setExpanded(false)}
          className="flex size-6 items-center justify-center rounded-md text-ink-muted hover:bg-surface-2"
        >
          <ChevronDown className="size-4" />
        </button>
        <h2 className="text-[12px] font-semibold text-ink">
          Waiting
          {waitingCount > 0 && <span className="ml-1 font-normal text-ink-muted">· {waitingCount}</span>}
        </h2>
        {(waitingCount > 0 || held) && (
          <button
            type="button"
            disabled={busy}
            onClick={toggleQueue}
            aria-pressed={held}
            className={cn(
              "ml-auto flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[11px] disabled:opacity-50",
              held
                ? "border-warn/40 bg-warn/10 text-warn hover:bg-warn/15"
                : "border-hairline text-ink-soft hover:bg-surface-2",
            )}
          >
            {held ? <Play className="size-3" /> : <Pause className="size-3" />}
            {held ? "Resume" : "Pause"}
          </button>
        )}
      </div>

      {held && (
        <p className="px-3 pb-1.5 text-[11px] text-warn" title={page.hold ?? undefined}>
          {holdReason ? `${holdReason} — ` : "Queue paused — "}nothing new starts until you resume.
        </p>
      )}

      {effectsFailed && (
        <div className="mx-3 mb-2 rounded-lg border border-bad/30 bg-bad/5 px-2.5 py-1.5">
          <div className="flex items-center gap-2 text-[12px] text-bad">
            <AlertTriangle className="size-3.5 shrink-0" />
            <span className="flex-1" title={page.effects_failed_turn_id ?? undefined}>
              {effectsFailed}
            </span>
            {page.effects_failed_turn_id && (
              <button
                type="button"
                onClick={() => void viewEffectsError()}
                className="shrink-0 rounded-md px-1.5 text-[11px] text-bad underline-offset-2 hover:underline"
              >
                {showEffects ? "Hide" : "View error"}
              </button>
            )}
          </div>
          {showEffects && effectsDetail && (
            <p className="mt-1 whitespace-pre-wrap break-words text-[11px] text-ink-soft">
              {effectsDetail}
            </p>
          )}
        </div>
      )}

      {notice && (
        <p role="alert" className="px-3 pb-1.5 text-[11px] text-warn">
          {notice}
        </p>
      )}

      {attentionTurn && (
        <RecoveryBlock
          turn={attentionTurn}
          open={resolving === attentionTurn.id}
          acknowledged={acknowledged}
          busy={busy}
          onOpen={() => {
            setResolving(attentionTurn.id);
            setAcknowledged(false);
          }}
          onAck={setAcknowledged}
          onResolve={(option) => void resolve(attentionTurn, option)}
          onCancel={() => {
            setResolving(null);
            setAcknowledged(false);
          }}
        />
      )}

      {waitingCount > 0 && (
        <ol
          className="max-h-[42vh] divide-y divide-hairline/60 overflow-y-auto overscroll-contain border-t border-hairline"
          aria-live="polite"
        >
          {waiting.map((turn) => {
            const position = turn.queue_position ?? turn.queue_sequence;
            const source = turnSourceLabel(turn);
            const reason = blockedReasonLabel(turn.blocked_reason);
            const open = view?.turnId === turn.id;
            const editing = open && view?.editing ? view : null;
            return (
              <li key={turn.id} data-turn-id={turn.id} className="bg-surface-1">
                <button
                  type="button"
                  aria-expanded={open}
                  aria-label={`Open request #${position} from ${source}`}
                  onClick={() => void openRow(turn)}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-surface-2"
                >
                  <span className="w-5 shrink-0 text-[11px] tabular-nums text-ink-muted">#{position}</span>
                  <span className="shrink-0 truncate text-[12px] font-medium text-ink-soft">{source}</span>
                  <span className="flex-1 truncate text-[12px] text-ink-muted">
                    {previewWords(turn.preview)}
                  </span>
                  <ChevronDown
                    className={cn("size-3.5 shrink-0 text-ink-muted transition-transform", open && "rotate-180")}
                  />
                </button>

                {open && (
                  <div className="border-t border-hairline/60 bg-surface-2/30 px-3 py-2">
                    <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-ink-muted">
                      <span className="font-medium text-ink-soft">{source}</span>
                      <span>· #{position}</span>
                      {reason && (
                        <span className="text-warn" title={turn.blocked_reason ?? undefined}>
                          · {reason}
                        </span>
                      )}
                    </div>

                    {editing ? (
                      <div>
                        <textarea
                          ref={editorRef}
                          aria-label={`Edit request #${position}`}
                          value={editing.draft}
                          readOnly={editing.consumed}
                          onChange={(e) =>
                            setView((v) => (v ? { ...v, draft: e.target.value } : v))
                          }
                          onKeyDown={(e) => {
                            if (e.key === "Escape") setView((v) => (v ? { ...v, editing: false } : v));
                          }}
                          rows={8}
                          className="min-h-[9rem] w-full resize-y rounded-lg bg-surface-1 p-2.5 text-[14px] leading-relaxed text-ink outline-none ring-1 ring-inset ring-hairline focus:ring-accent/50"
                        />
                        <div className="mt-2 flex flex-wrap gap-2">
                          <button
                            type="button"
                            disabled={busy || editing.consumed || !editing.draft.trim()}
                            onClick={() => void save(editing)}
                            className="h-8 rounded-full bg-accent-dim px-3 text-[12px] font-medium text-accent disabled:opacity-40"
                          >
                            Save
                          </button>
                          <button
                            type="button"
                            onClick={() => setView((v) => (v ? { ...v, editing: false } : v))}
                            className="h-8 rounded-full px-3 text-[12px] text-ink-soft hover:bg-surface-3"
                          >
                            {editing.consumed ? "Close" : "Cancel"}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        {view?.loading ? (
                          <p className="text-[12px] text-ink-muted">Loading…</p>
                        ) : view?.error ? (
                          <p className="text-[12px] text-warn">Could not load the request. Tap again to retry.</p>
                        ) : (
                          <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-ink">
                            {view?.body || turn.preview}
                          </p>
                        )}

                        {isEditableTurn(turn) && !view?.loading && (
                          <div className="mt-2 flex flex-wrap gap-2">
                            <button
                              type="button"
                              disabled={busy}
                              onClick={startEdit}
                              aria-label={`Edit request #${position}`}
                              className="h-8 rounded-full border border-hairline px-3 text-[12px] text-ink-soft hover:bg-surface-3"
                            >
                              Edit
                            </button>
                            {confirmWithdraw === turn.id ? (
                              <>
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => void withdraw(turn)}
                                  className="h-8 rounded-full bg-bad/15 px-3 text-[12px] font-medium text-bad hover:bg-bad/25"
                                >
                                  Confirm withdraw
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setConfirmWithdraw(null)}
                                  className="h-8 rounded-full px-3 text-[12px] text-ink-soft hover:bg-surface-3"
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
                                className="h-8 rounded-full px-3 text-[12px] text-bad hover:bg-bad/10"
                              >
                                Withdraw
                              </button>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

/** The single active slot's human-needed resolution (recovery / stalled claim). */
function RecoveryBlock({
  turn,
  open,
  acknowledged,
  busy,
  onOpen,
  onAck,
  onResolve,
  onCancel,
}: {
  turn: TurnRequestSummary;
  open: boolean;
  acknowledged: boolean;
  busy: boolean;
  onOpen: () => void;
  onAck: (v: boolean) => void;
  onResolve: (option: RecoveryOption) => void;
  onCancel: () => void;
}) {
  const options = recoveryOptions(turn.status);
  const needsAck = options.some((o) => o.requiresAck);
  const source = turnSourceLabel(turn);
  const reason = blockedReasonLabel(turn.blocked_reason);
  const title =
    turn.status === "recovery_required"
      ? "Recovery required"
      : "Stalled before starting";

  return (
    <div className="mx-3 mb-2 rounded-lg border border-bad/30 bg-bad/5 px-2.5 py-2">
      <div className="flex items-center gap-2 text-[12px] font-medium text-bad">
        <AlertTriangle className="size-3.5 shrink-0" />
        {title}
        <span className="font-normal text-ink-muted">· {source}</span>
      </div>
      <p className="mt-0.5 whitespace-pre-wrap break-words text-[12px] text-ink-soft">{turn.preview}</p>
      {reason && (
        <p className="mt-0.5 text-[11px] text-ink-muted" title={turn.blocked_reason ?? undefined}>
          {reason}
        </p>
      )}

      {open ? (
        <div className="mt-2 space-y-2">
          <ul className="space-y-1 text-[11px] text-ink-muted">
            {options.map((o) => (
              <li key={o.decision}>
                <span className="font-medium text-ink-soft">{o.label}</span> — {o.description}
              </li>
            ))}
          </ul>
          {needsAck && (
            <label className="flex items-start gap-2 text-[12px] text-ink-soft">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => onAck(e.target.checked)}
                className="mt-0.5"
              />
              I checked the agent has stopped; its outcome is unproven and it will not be re-run.
            </label>
          )}
          <div className="flex flex-wrap gap-2">
            {options.map((o) => (
              <button
                key={o.decision}
                type="button"
                disabled={busy || (o.requiresAck && !acknowledged)}
                onClick={() => onResolve(o)}
                className={cn(
                  "h-8 rounded-full px-3 text-[12px] font-medium disabled:opacity-40",
                  o.tone === "danger" && "bg-bad/15 text-bad hover:bg-bad/25",
                  o.tone === "safe" && "bg-accent-dim text-accent hover:bg-accent-dim/70",
                  o.tone === "neutral" && "border border-hairline text-ink-soft hover:bg-surface-3",
                )}
              >
                {o.label}
              </button>
            ))}
            <button
              type="button"
              onClick={onCancel}
              className="h-8 rounded-full px-3 text-[12px] text-ink-soft hover:bg-surface-3"
            >
              Not now
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={onOpen}
          className="mt-2 h-8 rounded-full border border-bad/40 px-3 text-[12px] font-medium text-bad hover:bg-bad/10"
        >
          Resolve…
        </button>
      )}
    </div>
  );
}
