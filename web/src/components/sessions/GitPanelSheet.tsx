/**
 * Git panel sheet — parity with Telegram /git_status, /commit, /commit_all.
 * Routes all ops through POST /api/sessions/{id}/inspect (NodeInspector), so
 * results reflect the owning node's filesystem state.
 */
import { useState, useEffect } from "react";
import { X, RefreshCw } from "lucide-react";
import { useInspectSession } from "../../hooks/useSessionActions";

interface Props {
  sessionId: string;
  onClose: () => void;
}

interface GitStatus {
  current_branch?: string;
  working_directory_clean?: boolean;
  changes?: { modified: string[]; created: string[]; deleted: string[]; total: number };
  staged_files?: string[];
  unstaged_files?: string[];
  safety?: { has_sensitive_files: boolean; sensitive_files: string[] };
  error?: string;
}

interface CommitResult {
  success?: boolean;
  branch_name?: string;
  files_committed?: string[];
  errors?: string[];
  error?: string;
}

export function GitPanelSheet({ sessionId, onClose }: Props) {
  const inspect = useInspectSession();
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [commitMsg, setCommitMsg] = useState("");
  const [commitResult, setCommitResult] = useState<CommitResult | null>(null);
  const [commitAllResult, setCommitAllResult] = useState<CommitResult | null>(null);

  const loadStatus = () => {
    inspect.mutate(
      { sessionId, op: "git_status" },
      { onSuccess: (r) => setStatus(r as GitStatus) },
    );
  };

  useEffect(() => { loadStatus(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const doCommit = (op: "commit" | "commit_all") => {
    const setter = op === "commit" ? setCommitResult : setCommitAllResult;
    setter(null);
    inspect.mutate(
      {
        sessionId,
        op,
        params: {
          task_id: `web_${Date.now()}`,
          task_description: commitMsg.trim() || undefined,
          create_branch: true,
          push_branch: false,
        },
      },
      { onSuccess: (r) => { setter(r as CommitResult); loadStatus(); } },
    );
  };

  // Full file lists — never truncated. You need to see exactly what you're about
  // to commit, so every path is listed (the <pre> scrolls if long).
  const fileBlock = (label: string, files: string[]) =>
    files.length > 0 ? `${label} (${files.length}):\n${files.map((f) => `  ${f}`).join("\n")}` : "";

  const formatStatus = (s: GitStatus) => {
    if (s.error) return `Error: ${s.error}`;
    const lines: string[] = [`Branch: ${s.current_branch ?? "?"}`];
    if (s.working_directory_clean) {
      lines.push("Working directory: clean");
    } else {
      const c = s.changes ?? { modified: [], created: [], deleted: [], total: 0 };
      const modified = c.modified ?? [];
      const created = c.created ?? [];
      const deleted = c.deleted ?? [];
      lines.push(`Modified: ${modified.length}  Created: ${created.length}  Deleted: ${deleted.length}`);
      const blocks = [
        fileBlock("Modified", modified),
        fileBlock("Created", created),
        fileBlock("Deleted", deleted),
        fileBlock("Staged", s.staged_files ?? []),
        fileBlock("Unstaged", s.unstaged_files ?? []),
      ].filter(Boolean);
      if (blocks.length) lines.push("", blocks.join("\n"));
      if (s.safety?.has_sensitive_files) {
        lines.push("", `⚠ Sensitive files (${s.safety.sensitive_files.length}):`, ...s.safety.sensitive_files.map((f) => `  ${f}`));
      }
    }
    return lines.join("\n");
  };

  const formatCommitResult = (r: CommitResult) => {
    if (!r.success) {
      return `Failed: ${(r.errors ?? [r.error ?? "unknown error"]).join("; ")}`;
    }
    const files = r.files_committed ?? [];
    const head = `✅ Committed ${files.length} file${files.length !== 1 ? "s" : ""}${r.branch_name ? ` on ${r.branch_name}` : ""}`;
    return files.length ? `${head}\n${files.map((f) => `  ${f}`).join("\n")}` : head;
  };

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
          <h2 className="text-base font-semibold text-ink">Git</h2>
          <button
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Close"
          >
            <X className="size-5" />
          </button>
        </div>

        {/* Status section */}
        <div className="mb-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">Status</span>
            <button
              onClick={loadStatus}
              disabled={inspect.isPending}
              className="flex items-center gap-1 text-[11px] text-accent hover:underline disabled:opacity-50"
            >
              <RefreshCw className="size-3" />
              Refresh
            </button>
          </div>
          {inspect.isPending && !status && (
            <p className="text-sm text-ink-muted">Loading…</p>
          )}
          {inspect.isError && !status && (
            <p className="text-sm text-bad">Failed to load git status.</p>
          )}
          {status && (
            <pre className="rounded-lg bg-surface-1 border border-hairline px-3 py-2 font-mono text-[11px] text-ink whitespace-pre-wrap">
              {formatStatus(status)}
            </pre>
          )}
        </div>

        {/* Commit message */}
        <div className="mb-3">
          <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
            Commit message (optional)
          </label>
          <input
            value={commitMsg}
            onChange={(e) => setCommitMsg(e.target.value)}
            placeholder="Leave blank to auto-generate from last task"
            className="w-full rounded-lg border border-hairline bg-surface-1 px-3 py-2 text-[13px] text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
          />
        </div>

        {/* Commit buttons */}
        <div className="flex gap-2 mb-2">
          <button
            onClick={() => doCommit("commit")}
            disabled={inspect.isPending}
            className="flex-1 rounded-xl bg-accent px-4 py-2.5 text-[13px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
          >
            Commit safe changes
          </button>
          <button
            onClick={() => doCommit("commit_all")}
            disabled={inspect.isPending}
            className="flex-1 rounded-xl border border-hairline bg-surface-1 px-4 py-2.5 text-[13px] font-medium text-ink hover:bg-surface-2 disabled:opacity-50"
          >
            Commit all staged
          </button>
        </div>

        {commitResult && (
          <pre className={`whitespace-pre-wrap font-mono text-[12px] ${commitResult.success ? "text-ok" : "text-bad"}`}>
            {formatCommitResult(commitResult)}
          </pre>
        )}
        {commitAllResult && (
          <pre className={`whitespace-pre-wrap font-mono text-[12px] ${commitAllResult.success ? "text-ok" : "text-bad"}`}>
            {formatCommitResult(commitAllResult)}
          </pre>
        )}
      </div>
    </div>
  );
}
