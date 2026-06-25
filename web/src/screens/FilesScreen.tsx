import { useState } from "react";
import {
  FolderGit2,
  FilePlus2,
  FilePen,
  FileMinus2,
  ChevronDown,
  ExternalLink,
  Calendar,
} from "lucide-react";
import { Link } from "react-router-dom";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { useArtifacts, useArtifact } from "../hooks/useLiveData";
import type { Artifact, RemoteFile } from "../domain/models";

const CHANGE_META: Record<
  RemoteFile["change"],
  { Icon: typeof FilePen; cls: string; label: string }
> = {
  added: { Icon: FilePlus2, cls: "text-ok", label: "added" },
  modified: { Icon: FilePen, cls: "text-warn", label: "modified" },
  deleted: { Icon: FileMinus2, cls: "text-bad", label: "deleted" },
};

function FileRow({
  file,
  lines,
}: {
  file: RemoteFile;
  lines: { added: number | null; deleted: number | null };
}) {
  const { Icon, cls, label } = CHANGE_META[file.change];
  const filename = file.path.split(/[/\\]/).pop() ?? file.path;
  const dir = file.path.includes("/") || file.path.includes("\\")
    ? file.path.slice(0, file.path.lastIndexOf(filename) - 1)
    : "";

  return (
    <div className="flex items-start gap-2.5 py-2 text-[13px]">
      <Icon className={`mt-0.5 size-3.5 shrink-0 ${cls}`} aria-label={label} />
      <div className="min-w-0 flex-1">
        <span className="font-mono text-ink">{filename}</span>
        {dir && (
          <p className="truncate font-mono text-[11px] text-ink-muted">{dir}</p>
        )}
      </div>
      {(lines.added != null || lines.deleted != null) && (
        <span className="shrink-0 font-mono text-[11px] tabular-nums">
          {lines.added != null && <span className="text-ok">+{lines.added}</span>}
          {lines.deleted != null && <span className="ml-1 text-bad">−{lines.deleted}</span>}
        </span>
      )}
    </div>
  );
}

function ArtifactChanges({ taskId }: { taskId: string }) {
  const { data, isLoading, isError } = useArtifact(taskId);
  if (isLoading) return <p className="py-2 text-xs text-ink-muted">Loading changes…</p>;
  if (isError) return <p className="py-2 text-xs text-bad">Couldn't load changes.</p>;
  if (!data) return null;
  if (data.files.length === 0)
    return <p className="py-2 text-xs text-ink-muted">No files changed.</p>;
  return (
    <div className="mt-2 divide-y divide-hairline border-t border-hairline">
      {data.files.map((f, i) => (
        <FileRow key={f.path} file={f} lines={data.lineCounts[i]} />
      ))}
      {data.errors.length > 0 && (
        <p className="pt-2 truncate text-xs text-bad">{data.errors[0]}</p>
      )}
    </div>
  );
}

function taskShortId(name: string): string {
  // Convert "task_68985b56" → "#68985b56", or show raw if no underscore
  if (name.startsWith("task_")) return `#${name.slice(5)}`;
  return name;
}

function ArtifactCard({ artifact }: { artifact: Artifact }) {
  const [open, setOpen] = useState(false);
  const dateStr = artifact.createdAt
    ? new Date(artifact.createdAt).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <div className="card-elev overflow-hidden rounded-xl">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-3.5 text-left"
      >
        <ChevronDown
          className={`size-4 shrink-0 text-ink-muted transition-transform ${open ? "" : "-rotate-90"}`}
        />
        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-[13px] font-medium text-ink">
            {taskShortId(artifact.name)}
          </p>
          {dateStr && (
            <p className="mt-0.5 flex items-center gap-1 text-[11px] text-ink-muted">
              <Calendar className="size-3" />
              {dateStr}
            </p>
          )}
        </div>
        {artifact.sessionId && (
          <Link
            to={`/sessions/${artifact.sessionId}`}
            onClick={(e) => e.stopPropagation()}
            className="shrink-0 inline-flex items-center gap-1 rounded-md bg-accent-dim/40 px-2 py-1 text-[11px] text-accent hover:bg-accent-dim"
          >
            <ExternalLink className="size-3" />
            session
          </Link>
        )}
      </button>

      {open && (
        <div className="border-t border-hairline px-4 pb-3.5 pt-1">
          <ArtifactChanges taskId={artifact.taskId ?? artifact.id} />
        </div>
      )}
    </div>
  );
}

export function FilesScreen() {
  const { data, isLoading, isError } = useArtifacts();
  const empty = !isLoading && !isError && (data?.length ?? 0) === 0;

  return (
    <div className="pb-8">
      <CompactTopBar title="Files" subtitle="Changed by session tasks" />

      {isLoading && (
        <div className="space-y-2.5 px-4 pt-4">
          {[1, 2, 3].map((n) => (
            <div key={n} className="card-elev animate-pulse rounded-xl px-4 py-3.5">
              <div className="flex items-center gap-3">
                <div className="size-4 rounded bg-surface-2" />
                <div className="h-4 w-32 rounded bg-surface-2" />
                <div className="ml-auto h-4 w-16 rounded bg-surface-2" />
              </div>
              <div className="mt-1.5 ml-7 h-3 w-24 rounded bg-surface-2" />
            </div>
          ))}
        </div>
      )}

      {isError && (
        <p className="px-4 py-8 text-center text-sm text-bad">Couldn't load files.</p>
      )}

      {empty && (
        <div className="flex flex-col items-center gap-3 px-8 py-20 text-center">
          <div className="flex size-14 items-center justify-center rounded-2xl bg-surface-1 ring-1 ring-hairline">
            <FolderGit2 className="size-7 text-ink-muted" />
          </div>
          <div>
            <p className="text-[15px] font-medium text-ink-soft">No file changes yet</p>
            <p className="mt-1 text-sm text-ink-muted">
              Files modified by tasks will appear here.
            </p>
          </div>
        </div>
      )}

      <div className="space-y-2.5 px-4">
        {(data ?? []).map((a) => (
          <ArtifactCard key={a.id} artifact={a} />
        ))}
      </div>
    </div>
  );
}
