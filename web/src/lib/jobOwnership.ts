import type { RawJob } from "../transport/rawApi";

export type JobOwnershipFilter = "all" | "unowned";

export function filterJobsByOwnership(
  jobs: RawJob[],
  ownership: JobOwnershipFilter,
): RawJob[] {
  if (ownership === "all") return jobs;
  // "Unowned" = not attached to a session the UI can show: either a genuinely null
  // session_id OR an orphaned one (session_id set but matching no known session,
  // flagged server-side). Both belong in System > Jobs — otherwise a registered
  // job whose session id doesn't resolve would be invisible everywhere.
  return jobs.filter((job) => job.session_id == null || Boolean(job.orphaned));
}
