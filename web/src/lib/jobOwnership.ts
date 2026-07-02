import type { RawJob } from "../transport/rawApi";

export type JobOwnershipFilter = "all" | "unowned";

export function filterJobsByOwnership(
  jobs: RawJob[],
  ownership: JobOwnershipFilter,
): RawJob[] {
  if (ownership === "all") return jobs;
  return jobs.filter((job) => job.session_id == null);
}
