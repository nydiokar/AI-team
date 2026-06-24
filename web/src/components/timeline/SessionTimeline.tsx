/**
 * SessionTimeline (spec §2.4, §7.2) — a chronological stream of typed cards.
 * Whole-message only (no token streaming). Operational "job" events render as
 * SystemNotice rows — the replacement for the ⛔-dropped tool-execution cards
 * (gap-doc §6). Fed from fixtures in UI-1. Each card answers what/state/when/
 * where/action, compact by default.
 */
import {
  CircleDot,
  ShieldQuestion,
  FileCode2,
  CheckCircle2,
  AlertCircle,
  Info,
} from "lucide-react";
import type { TimelineItem } from "../../fixtures/timeline";
import type { ApprovalRequest } from "../../domain/models";
import { TaskStatusChip } from "../ui/StatusChip";
import { Button } from "../ui/Button";
import { cn } from "../../lib/cn";
import { useResolveApproval } from "../../hooks/useSessionActions";

function time(at: string): string {
  return at.length >= 19 ? at.slice(11, 16) : at;
}

const NOTICE_ICON = { info: Info, success: CheckCircle2, warning: AlertCircle, error: AlertCircle };
const NOTICE_TONE: Record<string, string> = {
  info: "text-ink-muted",
  success: "text-ok",
  warning: "text-warn",
  error: "text-bad",
};
const RISK_TONE: Record<string, string> = { low: "text-ink-soft", medium: "text-warn", high: "text-bad" };

/**
 * Live approval card (Move H / UI-3). The Approve/Reject buttons round-trip
 * through useResolveApproval; the backend guard makes a double-resolve a no-op
 * (409). While the mutation is in flight both buttons disable.
 */
function ApprovalCard({ approval, at }: { approval: ApprovalRequest; at: string }) {
  const resolve = useResolveApproval();
  const decide = (decision: "approved" | "rejected") =>
    resolve.mutate({ approvalId: approval.id, decision });

  return (
    <div className="card-elev attention-glow mx-4 my-2 rounded-xl p-4">
      <div className="flex items-center gap-2">
        <ShieldQuestion className="size-4 text-warn" />
        <span className="text-[11px] font-semibold uppercase tracking-wide text-warn">
          Approval required
        </span>
        <span className="ml-auto text-[10px] text-ink-muted">{time(at)}</span>
      </div>
      <p className="mt-2 text-[14px] text-ink">{approval.action}</p>
      <div className="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs">
        <span className={RISK_TONE[approval.risk]}>{approval.risk} risk</span>
        <span className="text-ink-muted">·</span>
        <span className="text-ink-muted">
          {approval.reversible ? "reversible" : "irreversible"}
        </span>
        {approval.stale && <span className="ml-auto text-warn">state may be stale</span>}
      </div>
      {resolve.isError && (
        <p className="mt-2 text-[11px] text-bad">
          Couldn’t resolve: {String(resolve.error?.message ?? "unknown")}.
        </p>
      )}
      <div className="mt-3 flex gap-2">
        <Button
          size="sm"
          variant="outline"
          className="flex-1"
          disabled={resolve.isPending}
          onClick={() => decide("rejected")}
        >
          Reject
        </Button>
        <Button
          size="sm"
          className="flex-1"
          disabled={resolve.isPending}
          onClick={() => decide("approved")}
        >
          Approve
        </Button>
      </div>
    </div>
  );
}

function Item({ item }: { item: TimelineItem }) {
  switch (item.kind) {
    case "message": {
      const mine = item.message.role === "user";
      return (
        <div className={cn("flex px-4 py-1", mine ? "justify-end" : "justify-start")}>
          <div
            className={cn(
              "max-w-[82%] rounded-2xl px-3.5 py-2.5 text-[14px] leading-relaxed",
              mine
                ? "rounded-br-md bg-accent-dim/60 text-ink ring-1 ring-accent/20"
                : "card-elev rounded-bl-md text-ink",
            )}
          >
            {item.message.text}
          </div>
        </div>
      );
    }
    case "task_state":
      return (
        <div className="mx-4 my-1.5 flex items-center gap-2.5 rounded-xl border border-hairline bg-surface-1 px-3.5 py-2.5">
          <CircleDot className="size-4 text-accent" />
          <span className="min-w-0 flex-1 truncate text-[13px] text-ink-soft">{item.objective}</span>
          <TaskStatusChip state={item.state} />
        </div>
      );
    case "notice": {
      const Icon = NOTICE_ICON[item.notice.severity];
      return (
        <div className="mx-4 my-1 flex items-center gap-2 px-1 text-xs">
          <Icon className={cn("size-3.5 shrink-0", NOTICE_TONE[item.notice.severity])} />
          <span className="font-mono text-[10px] uppercase tracking-wide text-ink-muted">
            {item.notice.kind}
          </span>
          <span className="min-w-0 flex-1 truncate text-ink-soft">{item.notice.text}</span>
          <span className="text-[10px] text-ink-muted">{time(item.at)}</span>
        </div>
      );
    }
    case "approval":
      return <ApprovalCard approval={item.approval} at={item.at} />;
    case "artifact":
      return (
        <div className="mx-4 my-1.5 flex items-center gap-2.5 rounded-xl border border-hairline bg-surface-1 px-3.5 py-2.5 text-[13px]">
          <FileCode2 className="size-4 text-accent" />
          <span className="truncate font-mono text-ink-soft">{item.artifact.path}</span>
        </div>
      );
    case "error":
      return (
        <div className="mx-4 my-1.5 flex items-center gap-2 rounded-xl border border-bad/40 bg-bad/5 px-3.5 py-2.5 text-[13px] text-bad">
          <AlertCircle className="size-4 shrink-0" />
          {item.text}
        </div>
      );
  }
}

export function SessionTimeline({ items }: { items: TimelineItem[] }) {
  return (
    <div role="feed" aria-label="Session timeline" className="py-3">
      {items.map((it, i) => (
        <Item key={i} item={it} />
      ))}
    </div>
  );
}
