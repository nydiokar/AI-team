import { X } from "lucide-react";
import { useSetEffort } from "../../hooks/useSessionActions";
import { useModels } from "../../hooks/useLiveData";
import { cn } from "../../lib/cn";

const EFFORTS: Record<string, string[]> = {
  claude: ["low", "medium", "high", "xhigh", "max"],
  codex: ["low", "medium", "high", "xhigh", "max", "ultra"],
};

interface Props {
  sessionId: string;
  backend: string;
  currentEffort: string | null;
  currentModel: string | null;
  onClose: () => void;
}

export function EffortPickerSheet({ sessionId, backend, currentEffort, currentModel, onClose }: Props) {
  const setEffort = useSetEffort();
  const { data: models } = useModels(backend);
  const selected = models?.find((model) => model.name === currentModel);
  const choices = selected?.efforts?.length ? selected.efforts : (EFFORTS[backend] ?? []);
  const pick = (effort: string | null) => setEffort.mutate({ sessionId, effort }, { onSuccess: onClose });
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50" onClick={onClose}>
      <div className="card-elev w-full max-w-[480px] rounded-t-2xl p-5 pb-8" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-semibold text-ink">Thinking effort
            <span className="ml-2 font-mono text-[12px] text-ink-muted">({backend})</span>
          </h2>
          <button onClick={onClose} className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2" aria-label="Close"><X className="size-5" /></button>
        </div>
        <div className="flex flex-col gap-2">
          {[null, ...choices].map((effort) => (
            <button key={effort ?? "default"} onClick={() => pick(effort)} disabled={setEffort.isPending}
              className={cn("rounded-xl border px-4 py-3 text-left text-[13px] transition disabled:opacity-50",
                currentEffort === effort ? "border-accent/40 bg-accent-dim/40 text-ink ring-1 ring-accent/30" : "border-hairline bg-surface-1 text-ink hover:bg-surface-2")}
            >
              <span className="font-medium font-mono">{effort ?? "default"}</span>
              {effort === null && <span className="ml-2 text-ink-muted">(backend decides)</span>}
            </button>
          ))}
        </div>
        {setEffort.isError && <p className="mt-3 text-[12px] text-bad">Failed to save effort.</p>}
      </div>
    </div>
  );
}
