/**
 * Raw artifact payloads → canonical Artifact / RemoteFile (UI-4).
 *
 * The domain objects (domain/models.ts) were defined in UI-0 as the target shape;
 * UI-4's backend (src/control/artifacts.py) is what finally produces them. An
 * artifact today is a *task result* — so `kind` is "result". `RemoteFile.change`
 * is already normalized by the backend (added|modified|deleted); we narrow the
 * string to the domain union with a safe default rather than trust it blindly.
 */
import type { Artifact, RemoteFile } from "../domain/models";
import type {
  RawArtifactSummary,
  RawArtifactDetailResponse,
  RawRemoteFile,
} from "./rawApi";

export function toArtifact(raw: RawArtifactSummary): Artifact {
  return {
    id: raw.task_id,
    taskId: raw.task_id,
    sessionId: raw.session_id,
    kind: "result",
    path: raw.artifact_path,
    name: raw.task_id,
    createdAt: raw.timestamp,
  };
}

export function toArtifacts(raws: RawArtifactSummary[]): Artifact[] {
  return raws.map(toArtifact);
}

function narrowChange(change: string): RemoteFile["change"] {
  return change === "added" || change === "deleted" ? change : "modified";
}

export function toRemoteFile(
  raw: RawRemoteFile,
  sessionId: string | null,
): RemoteFile {
  return {
    path: raw.path,
    sessionId,
    change: narrowChange(raw.change),
  };
}

export interface ArtifactDetail {
  artifact: Artifact;
  files: RemoteFile[];
  /** Per-file line counts kept alongside (RemoteFile has no line fields); null
   *  when the artifact didn't store them. Indexed parallel to `files`. */
  lineCounts: Array<{ added: number | null; deleted: number | null }>;
  success: boolean;
  errors: string[];
}

export function toArtifactDetail(res: RawArtifactDetailResponse): ArtifactDetail {
  const a = res.artifact;
  return {
    artifact: {
      id: a.task_id,
      taskId: a.task_id,
      sessionId: a.session_id,
      kind: "result",
      path: "",
      name: a.task_id,
      createdAt: a.timestamp,
    },
    files: res.files.map((f) => toRemoteFile(f, a.session_id)),
    lineCounts: res.files.map((f) => ({ added: f.added, deleted: f.deleted })),
    success: a.success,
    errors: a.errors ?? [],
  };
}
