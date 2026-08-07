import { useEffect, useState } from "react";
import { Loader2, Pin, X } from "lucide-react";
import type { Session } from "../../domain/models";
import { useKeepSession } from "../../hooks/useSessionActions";

const KEEP_NOTE_MAX = 4000;

export function SessionKeepSheet({
  session,
  onClose,
}: {
  session: Session;
  onClose: () => void;
}) {
  const keep = useKeepSession();
  const [note, setNote] = useState(session.keepNote);
  const [enabled, setEnabled] = useState(session.keepPinned);

  useEffect(() => {
    setNote(session.keepNote);
    setEnabled(session.keepPinned);
  }, [session.id, session.keepNote, session.keepPinned]);

  const save = () => {
    keep.mutate(
      {
        sessionId: session.id,
        keepPinned: enabled,
        keepNote: enabled ? note.slice(0, KEEP_NOTE_MAX) : "",
      },
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
            <Pin className="size-4" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold text-ink">Keep session</h2>
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

        <label className="mb-3 flex items-center justify-between gap-4 rounded-xl border border-hairline bg-surface-1 px-3 py-3">
          <span className="text-[14px] font-medium text-ink-soft">Marked as kept</span>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.currentTarget.checked)}
            className="size-5 accent-accent"
          />
        </label>

        <label className="block">
          <span className="mb-1.5 block text-[12px] font-medium text-ink-muted">
            Note
          </span>
          <textarea
            value={note}
            onChange={(e) => setNote(e.currentTarget.value.slice(0, KEEP_NOTE_MAX))}
            disabled={!enabled}
            rows={5}
            placeholder="Why should this session be easy to find later?"
            className="w-full resize-none rounded-xl border border-hairline bg-surface-1 px-3 py-2.5 text-[14px] leading-5 text-ink outline-none placeholder:text-ink-muted focus:border-accent/50 disabled:opacity-50"
          />
        </label>
        <div className="mt-1 flex justify-between text-[11px] text-ink-muted">
          <span>{keep.isError ? String((keep.error as Error).message) : ""}</span>
          <span>{note.length}/{KEEP_NOTE_MAX}</span>
        </div>

        <div className="mt-5 flex gap-3">
          <button
            type="button"
            onClick={onClose}
            className="flex-1 rounded-xl border border-hairline bg-surface-1 py-3 text-[14px] font-medium text-ink-soft hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={keep.isPending}
            className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-accent-dim py-3 text-[14px] font-medium text-accent ring-1 ring-accent/30 hover:bg-accent-dim/80 disabled:opacity-60"
          >
            {keep.isPending && <Loader2 className="size-4 animate-spin" />}
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
