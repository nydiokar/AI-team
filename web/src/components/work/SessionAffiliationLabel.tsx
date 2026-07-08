/**
 * SessionAffiliationLabel — shows a session's AUTHORITATIVE role within a case
 * (Manager / Worker / Reviewer / Evidence), sourced only from the Work read
 * model's flow_links. A session with no affiliation renders "Standalone" — we
 * never infer ownership from task adjacency or prose.
 *
 * Two forms: a plain chip (for use inside another link, e.g. a SessionRow, where
 * a nested anchor would be invalid) and a link form that jumps to the case.
 */
import { Link } from "react-router-dom";
import { Briefcase } from "lucide-react";
import type { SessionAffiliation } from "../../domain/work";
import { roleLabel, roleTone } from "../../lib/workPresentation";
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
  return (
    <span className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-full bg-accent-dim/50 py-1 pl-2 pr-2.5 text-[11px] font-medium text-accent">
      <Briefcase className="size-3 shrink-0" />
      <span className="shrink-0">{roleLabel(affiliation.role)}</span>
      <span className="min-w-0 truncate text-accent/80">· {affiliation.caseTitle}</span>
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
  return (
    <Link
      to={`/work/${encodeURIComponent(affiliation.flowRunId)}`}
      className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-full bg-accent-dim/50 py-1 pl-2 pr-2.5 text-[11px] font-medium text-accent hover:bg-accent-dim"
    >
      <Briefcase className="size-3 shrink-0" />
      <span className="shrink-0">{roleLabel(affiliation.role)} for case</span>
      <span className="min-w-0 truncate text-accent/80">· {affiliation.caseTitle}</span>
    </Link>
  );
}
