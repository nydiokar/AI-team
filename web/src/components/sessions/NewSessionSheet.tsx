/**
 * New session sheet — the create-session control the UI was missing (it had told
 * the operator "Start one from Telegram"). Ports the Telegram /session_new flow:
 * pick a backend, give a repo path, create. Wired to POST /api/sessions via
 * useCreateSession (idempotency-keyed). On success it navigates into the new
 * session so you can immediately send an instruction.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { X } from "lucide-react";
import { Button } from "../ui/Button";
import { useCreateSession } from "../../hooks/useSessionActions";
import { cn } from "../../lib/cn";

// The backend set the gateway declares (src/backends/registry.py). The Telegram
// picker offers the same three CLI backends; opencode-server is omitted here for
// the same reason (it needs a running server) — add it if/when that's wired.
const BACKENDS = [
  { id: "claude", label: "Claude Code" },
  { id: "codex", label: "Codex" },
  { id: "opencode", label: "OpenCode" },
] as const;

export function NewSessionSheet({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const create = useCreateSession();
  const [backend, setBackend] = useState<string>("claude");
  const [repoPath, setRepoPath] = useState<string>("");

  const submit = () => {
    if (!repoPath.trim()) return;
    create.mutate(
      { backend, repoPath: repoPath.trim() },
      {
        onSuccess: (env) => {
          const id = env.session?.session_id;
          onClose();
          if (id) navigate(`/sessions/${id}`);
        },
      },
    );
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50" onClick={onClose}>
      <div
        className="card-elev w-full max-w-[480px] rounded-t-2xl p-5 pb-8"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-ink">New session</h2>
          <button
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Close"
          >
            <X className="size-5" />
          </button>
        </div>

        <p className="mt-1 mb-3 text-xs text-ink-muted">
          Backend &amp; repo path. The session opens unbound; send an instruction to start.
        </p>

        <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
          Backend
        </label>
        <div className="mb-4 flex gap-2">
          {BACKENDS.map((b) => (
            <button
              key={b.id}
              onClick={() => setBackend(b.id)}
              className={cn(
                "flex-1 rounded-lg border px-3 py-2 text-[13px] transition",
                backend === b.id
                  ? "border-accent/40 bg-accent-dim/40 text-ink ring-1 ring-accent/30"
                  : "border-hairline bg-surface-1 text-ink-soft hover:bg-surface-2",
              )}
            >
              {b.label}
            </button>
          ))}
        </div>

        <label
          htmlFor="repo-path"
          className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide text-ink-muted"
        >
          Repo path
        </label>
        <input
          id="repo-path"
          value={repoPath}
          onChange={(e) => setRepoPath(e.target.value)}
          placeholder="C:/Users/you/Projects/your-repo"
          autoFocus
          className="mb-4 w-full rounded-lg border border-hairline bg-surface-1 px-3 py-2.5 font-mono text-[13px] text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />

        {create.isError && (
          <p className="mb-3 text-[12px] text-bad">
            Couldn’t create: {String(create.error?.message ?? "unknown")}.
          </p>
        )}

        <Button
          className="w-full"
          disabled={!repoPath.trim() || create.isPending}
          onClick={submit}
        >
          {create.isPending ? "Creating…" : "Create session"}
        </Button>
      </div>
    </div>
  );
}
