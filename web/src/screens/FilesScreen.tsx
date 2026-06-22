/**
 * Files screen — placeholder in UI-1. Artifact cards / previews / diff review are
 * UI-4, gated on an artifact listing API (gap-doc §6; spec §7.6). Present so the
 * four-tab IA is complete at 360px.
 */
import { FolderGit2 } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";

export function FilesScreen() {
  return (
    <div>
      <CompactTopBar title="Files" subtitle="Artifacts & uploads" />
      <div className="flex flex-col items-center gap-3 px-8 py-20 text-center">
        <FolderGit2 className="size-9 text-ink-muted" />
        <p className="text-sm text-ink-soft">Files arrive in UI-4.</p>
        <p className="max-w-xs text-xs text-ink-muted">
          Artifacts already exist on disk as{" "}
          <code className="font-mono text-ink-soft">results/&lt;task&gt;.json</code>, but there's no
          listing endpoint to browse them yet.
        </p>
      </div>
    </div>
  );
}
