/**
 * Node detail sheet — parity with Telegram /node <id>.
 * Shows backends, repos, load, heartbeat. Data comes from the already-fetched
 * Target (no extra API call) plus useProjects for the repo list.
 */
import { X, HeartPulse, Layers, FolderOpen, Cpu } from "lucide-react";
import { useProjects } from "../../hooks/useLiveData";
import type { Target } from "../../domain/models";

interface Props {
  target: Target;
  onClose: () => void;
}

function ageLabel(sec: number | null): string {
  if (sec == null) return "never seen";
  if (sec < 90) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  return `${Math.round(sec / 3600)}h ago`;
}

export function NodeDetailSheet({ target, onClose }: Props) {
  const { data: projects, isLoading: reposLoading } = useProjects(target.id);

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="card-elev w-full max-w-[480px] rounded-t-2xl p-5 pb-8 max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Cpu className="size-4 text-ink-muted" />
            <h2 className="text-base font-semibold text-ink truncate">{target.id}</h2>
          </div>
          <button
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Close"
          >
            <X className="size-5" />
          </button>
        </div>

        <div className="space-y-3 text-[13px]">
          {/* Liveness */}
          <div className="flex items-center gap-2">
            <HeartPulse className={`size-4 ${target.live ? "text-ok" : "text-ink-muted"}`} />
            <span className={target.live ? "text-ok" : "text-ink-muted"}>
              {target.live ? "Online" : "Offline"}
            </span>
            <span className="text-ink-muted">· heartbeat {ageLabel(target.heartbeatAgeSec)}</span>
          </div>

          {target.tailscaleIp && (
            <div>
              <span className="text-ink-muted">Tailscale IP: </span>
              <span className="font-mono text-ink">{target.tailscaleIp}</span>
            </div>
          )}

          <div>
            <span className="text-ink-muted">Max concurrent: </span>
            <span className="text-ink">{target.maxConcurrent}</span>
          </div>

          {/* Backends */}
          {target.backends.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                <Layers className="size-3.5" />
                Backends
              </div>
              <div className="flex flex-wrap gap-1.5">
                {target.backends.map((b) => (
                  <span
                    key={b}
                    className="rounded-full border border-hairline bg-surface-1 px-2.5 py-0.5 font-mono text-[11px] text-accent/90"
                  >
                    {b}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Repos */}
          <div>
            <div className="flex items-center gap-1.5 mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
              <FolderOpen className="size-3.5" />
              Repos
            </div>
            {reposLoading && (
              <p className="text-ink-muted">Loading repos…</p>
            )}
            {!reposLoading && (projects ?? []).length === 0 && (
              <p className="text-ink-muted">No repos advertised.</p>
            )}
            <ul className="space-y-1">
              {(projects ?? []).map((p) => (
                <li key={p.path} className="min-w-0">
                  <p className="font-medium text-ink">{p.name}</p>
                  <p className="truncate font-mono text-[10px] text-ink-muted">{p.path}</p>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
