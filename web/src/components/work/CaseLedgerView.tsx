/**
 * CaseLedgerView — the authoritative case↔entity ledger, grouped by entity
 * type. Sessions deep-link to the Sessions surface (the runtime inspector);
 * other entities render their id + role as honest references. Empty sections
 * render an explicit "none linked" rather than being hidden or inferred.
 */
import { Link } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import type { CaseLedger, CaseLink } from "../../domain/work";
import { roleLabel } from "../../lib/workPresentation";
import { normalizeSessionRole } from "../../transport/workAdapter";

const SECTION_TITLES: { key: keyof CaseLedger; label: string }[] = [
  { key: "sessions", label: "Sessions" },
  { key: "tasks", label: "Tasks" },
  { key: "approvals", label: "Approvals" },
  { key: "artifacts", label: "Artifacts" },
  { key: "jobs", label: "Jobs" },
  { key: "flows", label: "Flows" },
  { key: "other", label: "Other" },
];

function RoleTag({ role }: { role: string | null }) {
  if (!role) return null;
  return (
    <span className="shrink-0 rounded-md bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-ink-soft">
      {role}
    </span>
  );
}

function LinkRow({ link, section }: { link: CaseLink; section: keyof CaseLedger }) {
  const id = link.entityId ?? "(no id)";
  // Sessions are the one entity with a live detail surface to jump to.
  if (section === "sessions" && link.entityId) {
    return (
      <Link
        to={`/sessions/${encodeURIComponent(link.entityId)}`}
        className="flex items-center gap-2 rounded-lg bg-surface-1 px-3 py-2 ring-1 ring-hairline hover:bg-surface-2"
      >
        <span className="shrink-0 rounded bg-accent-dim/60 px-1.5 py-0.5 text-[10px] font-medium text-accent">
          {roleLabel(normalizeSessionRole(link.role))}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-ink-soft">
          {id}
        </span>
        <ExternalLink className="size-3.5 shrink-0 text-ink-muted" />
      </Link>
    );
  }
  return (
    <div className="flex items-center gap-2 rounded-lg bg-surface-1 px-3 py-2 ring-1 ring-hairline">
      <RoleTag role={link.role} />
      <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-ink-soft">
        {id}
      </span>
    </div>
  );
}

export function CaseLedgerView({ ledger }: { ledger: CaseLedger }) {
  const total = SECTION_TITLES.reduce((n, s) => n + ledger[s.key].length, 0);
  if (total === 0) {
    return (
      <p className="rounded-xl bg-surface-1 px-3 py-3 text-[12px] text-ink-muted ring-1 ring-hairline">
        No entities linked to this case yet.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {SECTION_TITLES.map(({ key, label }) => {
        const links = ledger[key];
        if (links.length === 0) return null;
        return (
          <div key={key}>
            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-ink-muted">
              {label} · {links.length}
            </p>
            <div className="space-y-1.5">
              {links.map((link, i) => (
                <LinkRow key={`${link.entityId}-${i}`} link={link} section={key} />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
