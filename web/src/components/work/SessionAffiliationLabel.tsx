/**
 * SessionAffiliationLabel — shows a session's AUTHORITATIVE role within a case
 * (Manager / Worker / Reviewer / Evidence), sourced only from the Work read
 * model's flow_links. A session with no affiliation renders "Standalone" — we
 * never infer ownership from task adjacency or prose.
 *
 * When the affiliated case is CLOSED (authoritative flow_runs.status), the label
 * reads as muted history with a "closed" marker — so an idle session whose case
 * finished is not shown as if it were still on active work. Closed/active is
 * derived only from the case's own status, never inferred.
 *
 * Two forms: a plain chip (for use inside another link, e.g. a SessionRow, where
 * a nested anchor would be invalid) and a link form that jumps to the case.
 */
import { Link } from "react-router-dom";
import { Briefcase } from "lucide-react";
import type { SessionAffiliation } from "../../domain/work";
import { roleLabel, roleTone, isClosedCaseStatus } from "../../lib/workPresentation";
import { cn } from "../../lib/cn";
import { ToneBadge } from "./ToneBadge";

export function SessionAffiliationChip({
  affiliation,
}: {
  affiliation: SessionAffiliation | undefined;
}) {
  if (!affiliation) {
    return (
      <ToneBadge tone="idle" label="Standalone" dot={false} className="max-w-full" />
    );
  }
  const closed = isClosedCaseStatus(affiliation.caseStatus);
  return (
    <span
      className={cn(
        "inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-full py-1 pl-2 pr-2.5 text-[11px] font-medium",
        closed ? "bg-surface-3/70 text-ink-soft" : "bg-accent-dim/50 text-accent",
      )}
    >
      <Briefcase className="size-3 shrink-0" />
      <span className="shrink-0">{roleLabel(affiliation.role)}</span>
      <span className={cn("min-w-0 truncate", closed ? "text-ink-soft" : "text-accent/80")}>
        · {affiliation.caseTitle}
      </span>
      {closed && <span className="shrink-0 text-ink-muted">· closed</span>}
    </span>
  );
}

/** Link form — for standalone contexts (e.g. SessionDetail header). */
export function SessionAffiliationLink({
  affiliation,
}: {
  affiliation: SessionAffiliation | undefined;
}) {
  if (!affiliation) {
    return <ToneBadge tone={roleTone("session")} label="Standalone" dot={false} />;
  }
  const closed = isClosedCaseStatus(affiliation.caseStatus);
  return (
    <Link
      to={`/work/${encodeURIComponent(affiliation.flowRunId)}`}
      className={cn(
        "inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-full py-1 pl-2 pr-2.5 text-[11px] font-medium",
        closed
          ? "bg-surface-3/70 text-ink-soft hover:bg-surface-3"
          : "bg-accent-dim/50 text-accent hover:bg-accent-dim",
      )}
    >
      <Briefcase className="size-3 shrink-0" />
      <span className="shrink-0">
        {roleLabel(affiliation.role)} {closed ? "of closed case" : "for case"}
      </span>
      <span className={cn("min-w-0 truncate", closed ? "text-ink-soft" : "text-accent/80")}>
        · {affiliation.caseTitle}
      </span>
    </Link>
  );
}
