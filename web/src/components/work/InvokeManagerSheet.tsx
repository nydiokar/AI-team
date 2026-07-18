/**
 * Invoke Manager sheet — the operator's one-tap entry into the autonomous flow.
 *
 * You type a short intent ("continue the work on this project"), pick the repo,
 * and submit. The gateway boots a Case-owning Manager session with the Manager
 * role profile; the Manager self-orients from the project CLAUDE.md (canonical
 * docs + how to find open work) and drives workers to satisfy the intent. You
 * do NOT spell out the plan — that is the Manager's job.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { X } from "lucide-react";
import { Button } from "../ui/Button";
import { useInvokeManager } from "../../hooks/useSessionActions";
import { useProjects } from "../../hooks/useLiveData";

export function InvokeManagerSheet({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const invoke = useInvokeManager();

  const { data: projects } = useProjects("__local__");
  const [objective, setObjective] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [criteria, setCriteria] = useState("");

  // Default the repo to the first discovered project once available.
  const effectiveRepo = repoPath || projects?.[0]?.path || "";

  const submit = () => {
    const obj = objective.trim();
    const repo = effectiveRepo.trim();
    if (!obj || !repo) return;
    invoke.mutate(
      { objective: obj, repoPath: repo, completionCriteria: criteria.trim() || undefined },
      {
        onSuccess: (res) => {
          onClose();
          if (res?.case_id) navigate(`/work/${res.case_id}`);
          else if (res?.session_id) navigate(`/sessions/${res.session_id}`);
        },
      },
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
        {/* Header */}
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-ink">Invoke Manager</h2>
          <button
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Close"
          >
            <X className="size-5" />
          </button>
        </div>

        <p className="mt-1 mb-3 text-xs text-ink-muted">
          Relay an intent — the Manager orients itself and drives the workers. You
          don't need to spell out the plan.
        </p>

        {/* Objective */}
        <label className="mb-1 block text-xs text-ink-muted">Intent</label>
        <textarea
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
          rows={3}
          autoFocus
          placeholder="continue the work on this project"
          className="mb-3 w-full resize-none rounded-lg border border-hairline bg-surface-1 px-3 py-2.5 text-[14px] text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
        />

        {/* Repo */}
        <label className="mb-1 block text-xs text-ink-muted">Repo</label>
        {projects && projects.length > 0 ? (
          <select
            value={effectiveRepo}
            onChange={(e) => setRepoPath(e.target.value)}
            className="mb-3 w-full rounded-lg border border-hairline bg-surface-1 px-3 py-2.5 text-[13px] text-ink focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
          >
            {projects.map((p) => (
              <option key={p.path} value={p.path}>
                {p.name} — {p.path}
              </option>
            ))}
          </select>
        ) : (
          <input
            value={repoPath}
            onChange={(e) => setRepoPath(e.target.value)}
            placeholder="/absolute/path/to/repo"
            className="mb-3 w-full rounded-lg border border-hairline bg-surface-1 px-3 py-2.5 font-mono text-[13px] text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
          />
        )}

        {/* Optional completion criteria */}
        <label className="mb-1 block text-xs text-ink-muted">
          Done gate <span className="text-ink-muted/70">(optional)</span>
        </label>
        <input
          value={criteria}
          onChange={(e) => setCriteria(e.target.value)}
          placeholder="what 'done' means — the Manager will demand it at close"
          className="mb-3 w-full rounded-lg border border-hairline bg-surface-1 px-3 py-2.5 text-[13px] text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
        />

        {invoke.isError && (
          <p className="mb-3 text-[12px] text-bad">
            Couldn't invoke: {String(invoke.error?.message ?? "unknown")}.
          </p>
        )}

        <Button
          className="w-full"
          disabled={!objective.trim() || !effectiveRepo.trim() || invoke.isPending}
          onClick={submit}
        >
          {invoke.isPending ? "Invoking…" : "Invoke Manager"}
        </Button>
      </div>
    </div>
  );
}
