/**
 * Model picker sheet — parity with Telegram /model command.
 * Shows the catalog for the session's backend; tapping a model calls
 * POST /api/sessions/{id}/model and closes the sheet.
 */
import { X } from "lucide-react";
import { useModels } from "../../hooks/useLiveData";
import { useSetModel } from "../../hooks/useSessionActions";
import { cn } from "../../lib/cn";

interface Props {
  sessionId: string;
  currentModel: string | null;
  backend: string;
  onClose: () => void;
}

export function ModelPickerSheet({ sessionId, currentModel, backend, onClose }: Props) {
  const { data: models, isLoading } = useModels(backend);
  const setModel = useSetModel();

  const pick = (name: string | null) => {
    setModel.mutate(
      { sessionId, model: name },
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
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold text-ink">
            Change model
            <span className="ml-2 font-mono text-[12px] text-ink-muted">({backend})</span>
          </h2>
          <button
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Close"
          >
            <X className="size-5" />
          </button>
        </div>

        {isLoading && (
          <p className="py-4 text-center text-sm text-ink-muted">Loading models…</p>
        )}

        <div className="flex flex-col gap-2">
          {/* Default option */}
          <button
            onClick={() => pick(null)}
            disabled={setModel.isPending}
            className={cn(
              "rounded-xl border px-4 py-3 text-left text-[13px] transition disabled:opacity-50",
              currentModel === null
                ? "border-accent/40 bg-accent-dim/40 text-ink ring-1 ring-accent/30"
                : "border-hairline bg-surface-1 text-ink hover:bg-surface-2",
            )}
          >
            <span className="font-medium">⚡ Default</span>
            <span className="ml-2 text-ink-muted">(backend decides)</span>
          </button>

          {(models ?? []).map((m) => (
            <button
              key={m.name}
              onClick={() => pick(m.name)}
              disabled={setModel.isPending}
              className={cn(
                "rounded-xl border px-4 py-3 text-left text-[13px] transition disabled:opacity-50",
                currentModel === m.name
                  ? "border-accent/40 bg-accent-dim/40 text-ink ring-1 ring-accent/30"
                  : "border-hairline bg-surface-1 text-ink hover:bg-surface-2",
              )}
            >
              <span className="font-medium font-mono">{m.name}</span>
              {m.is_default && (
                <span className="ml-2 text-[11px] text-ink-muted">(catalog default)</span>
              )}
            </button>
          ))}
        </div>

        {setModel.isError && (
          <p className="mt-3 text-[12px] text-bad">
            Failed: {String(setModel.error?.message ?? "unknown")}.
          </p>
        )}
      </div>
    </div>
  );
}
