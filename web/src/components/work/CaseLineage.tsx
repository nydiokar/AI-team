/**
 * CaseLineage — a COMPACT vertical lineage tree (navigation, not an editable
 * canvas). Renders the case's parent (if any), the case itself, and its direct
 * children, from the authoritative /api/work/{id}/graph. Each other node links
 * to its own case detail; the self node is highlighted and inert.
 *
 * No free-form DAG, no drag, no edit — this is a read-only breadcrumb of
 * authoritative parent→child lineage.
 */
import { Link } from "react-router-dom";
import { CornerDownRight } from "lucide-react";
import type { CaseGraph, CaseGraphNode } from "../../domain/work";
import { ToneBadge } from "./ToneBadge";
import { bucketMeta } from "../../lib/workPresentation";
import { cn } from "../../lib/cn";

function Node({ node, indent }: { node: CaseGraphNode; indent: boolean }) {
  const meta = bucketMeta(node.bucket);
  const isSelf = node.rel === "self";
  const inner = (
    <div
      className={cn(
        "flex items-center gap-2 rounded-xl px-3 py-2",
        isSelf
          ? "bg-accent-dim/40 ring-1 ring-accent/30"
          : "bg-surface-1 ring-1 ring-hairline hover:bg-surface-2",
      )}
    >
      {indent && <CornerDownRight className="size-3.5 shrink-0 text-ink-muted" />}
      <span
        className={cn(
          "min-w-0 flex-1 truncate text-[13px]",
          isSelf ? "font-semibold text-ink" : "text-ink-soft",
        )}
      >
        {node.title}
      </span>
      <ToneBadge tone={meta.tone} label={meta.label} dot={false} />
    </div>
  );

  if (isSelf) return inner;
  return (
    <Link to={`/work/${encodeURIComponent(node.flowRunId)}`} className="block">
      {inner}
    </Link>
  );
}

export function CaseLineage({ graph }: { graph: CaseGraph }) {
  const parent = graph.nodes.find((n) => n.rel === "parent");
  const self = graph.nodes.find((n) => n.rel === "self");
  const children = graph.nodes.filter((n) => n.rel === "child");

  if (!self) return null;

  return (
    <div className="space-y-1.5">
      {parent && <Node node={parent} indent={false} />}
      <div className={parent ? "pl-3" : undefined}>
        <Node node={self} indent={Boolean(parent)} />
      </div>
      {children.length > 0 && (
        <div className={cn("space-y-1.5", parent ? "pl-6" : "pl-3")}>
          {children.map((c) => (
            <Node key={c.flowRunId} node={c} indent />
          ))}
        </div>
      )}
      {!parent && children.length === 0 && (
        <p className="px-1 text-[12px] text-ink-muted">
          Root case · no linked parent or children.
        </p>
      )}
    </div>
  );
}
