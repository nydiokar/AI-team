import { useEffect, useState } from "react";
import { Loader2, Pin, Trash2, X } from "lucide-react";
import type { Session } from "../../domain/models";
import { useKeepSession } from "../../hooks/useSessionActions";

const KEEP_NOTE_MAX = 4000;

/**
 * Keep-note editor. The pin itself is a one-tap toggle on the row / in the
 * detail menu; this sheet is only for the optional note. Opening it implies the
 * session is (or is being) kept — Save writes the note and keeps; "Remove keep"
 * clears both. No checkbox gate: the textarea is always editable.
 */
export function SessionKeepSheet({
  session,
  onClose,
}: {
  session: Session;
  onClose: () => void;
}) {
  const keep = useKeepSession();
  const [note, setNote] = useState(session.keepNote);

  useEffect(() => {
    setNote(session.keepNote);
  }, [session.id, session.keepNote]);

  const save = () => {
    keep.mutate(
      { sessionId: session.id, keepPinned: true, keepNote: note.slice(0, KEEP_NOTE_MAX) },
      { onSuccess: onClose },
    );
  };

  const removeKeep = () => {
    keep.mutate(
      { sessionId: session.id, keepPinned: false, keepNote: "" },
      { onSuccess: onClose },
    );
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="card-elev w-full max-w-[480px] rounded-t-2xl p-5 pb-8"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center gap-3">
          <div className="flex size-9 items-center justify-center rounded-xl bg-accent-dim/50 text-accent ring-1 ring-accent/25">
            <Pin className="size-4 fill-current" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold text-ink">Keep note</h2>
            <p className="truncate font-mono text-[11px] text-ink-muted">{session.id}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-full text-ink-muted hover:bg-surface-2 hover:text-ink"
            aria-label="Close"
          >
            <X className="size-4" />
          </button>
        </div>

        <label className="block">
          <span className="mb-1.5 block text-[12px] font-medium text-ink-muted">
            Why keep this session?
          </span>
          <textarea
            value={note}
            onChange={(e) => setNote(e.currentTarget.value.slice(0, KEEP_NOTE_MAX))}
            rows={5}
            autoFocus
            placeholder="e.g. reference implementation for the retry policy"
            className="w-full resize-none rounded-xl border border-hairline bg-surface-1 px-3 py-2.5 text-[14px] leading-5 text-ink outline-none placeholder:text-ink-muted focus:border-accent/50"
          />
        </label>
        <div className="mt-1 flex justify-end text-[11px] text-ink-muted">
          <span>{note.length}/{KEEP_NOTE_MAX}</span>
        </div>

        {keep.isError && (
          <p className="mt-2 rounded-lg bg-bad/10 px-3 py-2 text-[12px] text-bad">
            Couldn't save keep note: {String((keep.error as Error).message)}
          </p>
        )}

        <div className="mt-5 flex items-center gap-3">
          {session.keepPinned && (
            <button
              type="button"
              onClick={removeKeep}
              disabled={keep.isPending}
              className="flex items-center gap-1.5 rounded-xl px-3 py-3 text-[14px] font-medium text-bad hover:bg-bad/10 disabled:opacity-60"
            >
              <Trash2 className="size-4" />
              Remove keep
            </button>
          )}
          <button
            type="button"
            onClick={save}
            disabled={keep.isPending}
            className="ml-auto flex flex-1 items-center justify-center gap-2 rounded-xl bg-accent-dim py-3 text-[14px] font-medium text-accent ring-1 ring-accent/30 hover:bg-accent-dim/80 disabled:opacity-60"
          >
            {keep.isPending && <Loader2 className="size-4 animate-spin" />}
            {session.keepPinned ? "Save note" : "Keep session"}
          </button>
        </div>
      </div>
    </div>
  );
}
