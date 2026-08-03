/**
 * CostProjectFilter — the project dropdown for the Cost tab. Fed by the
 * `/api/cost/projects` list (token-ordered); "All projects" is the default.
 * Options are the server's repo paths, rendered with a short basename label.
 */
import { projectLabel } from "../../lib/costPresentation";
import { cn } from "../../lib/cn";
import type { CostProject } from "../../domain/cost";

export function CostProjectFilter({
  projects,
  value,
  onChange,
}: {
  projects: CostProject[];
  value: string | null;
  onChange: (repoPath: string | null) => void;
}) {
  const options = [
    { repoPath: null, label: "All projects" },
    ...projects.map((p) => ({ repoPath: p.repoPath, label: projectLabel(p.repoPath) })),
  ];
  return (
    <select
      aria-label="Filter by project"
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      className={cn(
        "w-full appearance-none rounded-xl bg-surface-2/70 px-3 py-2 text-[12px] font-medium text-ink ring-1 ring-hairline/60",
        "focus:outline-none focus:ring-2 focus:ring-accent/50",
      )}
    >
      {options.map((o) => (
        <option key={o.repoPath ?? "__all__"} value={o.repoPath ?? ""}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
