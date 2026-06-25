import {
  CircleDot,
  ShieldQuestion,
  FileCode2,
  CheckCircle2,
  AlertCircle,
  Info,
  Loader2,
} from "lucide-react";
import type { TimelineItem } from "../../fixtures/timeline";
import type { ApprovalRequest } from "../../domain/models";
import { TaskStatusChip } from "../ui/StatusChip";
import { Button } from "../ui/Button";
import { cn } from "../../lib/cn";
import { useResolveApproval } from "../../hooks/useSessionActions";

function timeLabel(at: string): string {
  if (!at) return "";
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return at.slice(11, 16) || "";
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

const NOTICE_ICON = {
  info: Info,
  success: CheckCircle2,
  warning: AlertCircle,
  error: AlertCircle,
};
const NOTICE_TONE: Record<string, string> = {
  info: "text-ink-muted",
  success: "text-ok",
  warning: "text-warn",
  error: "text-bad",
};
const RISK_TONE: Record<string, string> = {
  low: "text-ink-soft",
  medium: "text-warn",
  high: "text-bad",
};

function ApprovalCard({ approval, at }: { approval: ApprovalRequest; at: string }) {
  const resolve = useResolveApproval();
  const decide = (decision: "approved" | "rejected") =>
    resolve.mutate({ approvalId: approval.id, decision });

  return (
    <div className="card-elev attention-glow mx-4 my-3 rounded-xl p-4">
      <div className="flex items-center gap-2">
        <ShieldQuestion className="size-4 text-warn" />
        <span className="text-[11px] font-semibold uppercase tracking-wide text-warn">
          Approval required
        </span>
        <span className="ml-auto text-[10px] text-ink-muted">{timeLabel(at)}</span>
      </div>
      <p className="mt-2.5 text-[14px] text-ink">{approval.action}</p>
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
          Couldn't resolve: {String(resolve.error?.message ?? "unknown")}.
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

/**
 * Grouped message bubble. When consecutive messages share a role we tighten
 * vertical spacing and suppress the role label on all but the first. The
 * timestamp only shows on the last bubble in a group (cleaner, less noise).
 */
function MessageBubble({
  role,
  text,
  at,
  isFirst,
  isLast,
}: {
  role: "user" | "assistant";
  text: string;
  at: string;
  isFirst: boolean;
  isLast: boolean;
}) {
  const mine = role === "user";
  return (
    <div
      className={cn(
        "flex flex-col px-4",
        mine ? "items-end" : "items-start",
        isFirst ? "mt-3" : "mt-0.5",
      )}
    >
      {/* Role label — only on first in a group */}
      {isFirst && (
        <span className="mb-1 text-[11px] font-medium text-ink-muted">
          {mine ? "You" : "Agent"}
        </span>
      )}

      {/* Bubble */}
      <div
        className={cn(
          "max-w-[85%] px-3.5 py-2.5 text-[14px] leading-relaxed",
          // Shape: rounded on all corners except the "tail" corner (only first bubble)
          mine
            ? cn(
                "bg-accent-dim/70 text-ink ring-1 ring-accent/25",
                isFirst ? "rounded-2xl rounded-tr-md" : "rounded-2xl",
              )
            : cn(
                "card-elev text-ink",
                isFirst ? "rounded-2xl rounded-tl-md" : "rounded-2xl",
              ),
        )}
      >
        <p className="whitespace-pre-wrap break-words">{text}</p>
      </div>

      {/* Timestamp — only on last in a group */}
      {isLast && at && (
        <span className="mt-1 text-[10px] text-ink-muted">{timeLabel(at)}</span>
      )}
    </div>
  );
}

export function SessionTimeline({ items }: { items: TimelineItem[] }) {
  return (
    <div role="feed" aria-label="Session timeline" className="pb-4 pt-2">
      {items.map((item, i) => {
        // For message items, determine grouping context
        if (item.kind === "message") {
          const prev = i > 0 ? items[i - 1] : null;
          const next = i < items.length - 1 ? items[i + 1] : null;
          const prevSameRole =
            prev?.kind === "message" && prev.message.role === item.message.role;
          const nextSameRole =
            next?.kind === "message" && next.message.role === item.message.role;
          return (
            <MessageBubble
              key={i}
              role={item.message.role}
              text={item.message.text}
              at={item.at}
              isFirst={!prevSameRole}
              isLast={!nextSameRole}
            />
          );
        }

        if (item.kind === "task_state") {
          return (
            <div
              key={i}
              className="mx-4 my-3 flex items-center gap-2.5 rounded-xl border border-hairline bg-surface-1/60 px-3.5 py-2.5"
            >
              {item.state === "running" ? (
                <Loader2 className="size-3.5 shrink-0 animate-spin text-accent" />
              ) : (
                <CircleDot className="size-3.5 shrink-0 text-accent opacity-70" />
              )}
              <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-soft">
                {item.objective}
              </span>
              <TaskStatusChip state={item.state} />
            </div>
          );
        }

        if (item.kind === "notice") {
          const Icon = NOTICE_ICON[item.notice.severity];
          return (
            <div
              key={i}
              className="mx-4 my-1 flex items-center gap-2 px-1 py-0.5"
            >
              <Icon
                className={cn("size-3 shrink-0", NOTICE_TONE[item.notice.severity])}
              />
              <span className="font-mono text-[10px] uppercase tracking-wide text-ink-muted">
                {item.notice.kind}
              </span>
              <span className="min-w-0 flex-1 truncate text-[12px] text-ink-muted">
                {item.notice.text}
              </span>
              <span className="shrink-0 text-[10px] text-ink-muted">
                {timeLabel(item.at)}
              </span>
            </div>
          );
        }

        if (item.kind === "approval") {
          return <ApprovalCard key={i} approval={item.approval} at={item.at} />;
        }

        if (item.kind === "artifact") {
          return (
            <div
              key={i}
              className="mx-4 my-2 flex items-center gap-2.5 rounded-xl border border-hairline bg-surface-1/60 px-3.5 py-2.5"
            >
              <FileCode2 className="size-3.5 shrink-0 text-accent opacity-70" />
              <span className="truncate font-mono text-[12.5px] text-ink-soft">
                {item.artifact.path}
              </span>
            </div>
          );
        }

        if (item.kind === "error") {
          return (
            <div
              key={i}
              className="mx-4 my-2 flex items-center gap-2 rounded-xl border border-bad/30 bg-bad/5 px-3.5 py-2.5 text-[13px] text-bad"
            >
              <AlertCircle className="size-4 shrink-0" />
              <span className="min-w-0 flex-1">{item.text}</span>
            </div>
          );
        }

        return null;
      })}
    </div>
  );
}
