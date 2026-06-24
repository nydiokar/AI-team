/**
 * Files screen — UI-4. The phone review loop: "what did the agent change?".
 *
 * LIVE: artifact cards from useArtifacts (/api/artifacts → results/<task>.json
 * headers). Expanding a card fetches that artifact's changed-file rows
 * (useArtifact) — the "unified diff review" over the data the backend actually
 * stores (per-file change type + line counts when present; we do NOT fabricate
 * diff hunks, which no artifact carries today).
 */
import { useState } from "react";
import {
  FolderGit2,
  FilePlus2,
  FilePen,
  FileMinus2,
  ChevronRight,
  ExternalLink,
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
  return (
    <div className="flex items-center gap-2 py-1.5 text-[13px]">
      <Icon className={`size-3.5 shrink-0 ${cls}`} aria-label={label} />
      <span className="min-w-0 flex-1 truncate font-mono text-ink-soft">{file.path}</span>
      {(lines.added != null || lines.deleted != null) && (
        <span className="shrink-0 font-mono text-[11px]">
          {lines.added != null && <span className="text-ok">+{lines.added}</span>}
          {lines.deleted != null && <span className="ml-1 text-bad">−{lines.deleted}</span>}
        </span>
      )}
    </div>
  );
}

function ArtifactChanges({ taskId }: { taskId: string }) {
  const { data, isLoading, isError } = useArtifact(taskId);
  if (isLoading) return <p className="px-1 py-2 text-xs text-ink-muted">Loading changes…</p>;
  if (isError) return <p className="px-1 py-2 text-xs text-bad">Couldn’t load changes.</p>;
  if (!data) return null;
  if (data.files.length === 0)
    return <p className="px-1 py-2 text-xs text-ink-muted">No files changed.</p>;
  return (
    <div className="mt-2 border-t border-hairline pt-1.5">
      {data.files.map((f, i) => (
        <FileRow key={f.path} file={f} lines={data.lineCounts[i]} />
      ))}
      {data.errors.length > 0 && (
        <p className="mt-1.5 truncate text-xs text-bad">{data.errors[0]}</p>
      )}
    </div>
  );
}

function ArtifactCard({ artifact }: { artifact: Artifact }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card-elev rounded-xl px-4 py-3.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
      >
        <ChevronRight
          className={`size-4 shrink-0 text-ink-muted transition-transform ${open ? "rotate-90" : ""}`}
        />
        <span className="min-w-0 flex-1 truncate font-mono text-[13px] text-ink">
          {artifact.name}
        </span>
        {artifact.createdAt && (
          <span className="shrink-0 text-xs text-ink-muted">{artifact.createdAt.slice(0, 10)}</span>
        )}
      </button>
      {artifact.sessionId && (
        <Link
          to={`/sessions/${artifact.sessionId}`}
          className="mt-1 ml-6 inline-flex items-center gap-1 text-xs text-accent/90"
        >
          <ExternalLink className="size-3" /> session
        </Link>
      )}
      {open && (
        <div className="ml-6">
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
      <CompactTopBar title="Files" subtitle="Artifacts · what changed" />
      {isLoading && (
        <p className="px-4 py-8 text-center text-sm text-ink-muted">Loading artifacts…</p>
      )}
      {isError && (
        <p className="px-4 py-8 text-center text-sm text-bad">Couldn’t load artifacts.</p>
      )}
      {empty && (
        <div className="flex flex-col items-center gap-3 px-8 py-20 text-center">
          <FolderGit2 className="size-9 text-ink-muted" />
          <p className="text-sm text-ink-soft">No artifacts yet.</p>
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
