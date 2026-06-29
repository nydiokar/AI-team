import { useState } from "react";
import {
  CircleDot,
  ShieldQuestion,
  FileCode2,
  CheckCircle2,
  AlertCircle,
  Info,
  Loader2,
  Coins,
} from "lucide-react";
import type { TimelineItem, TokenUsage } from "../../fixtures/timeline";
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
        <span className="text-[13px] font-semibold tracking-tight text-warn">
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

function fmtTokens(n: number | undefined): string {
  if (n == null) return "0";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
}

/**
 * Subtle token-usage badge: a quiet count next to the timestamp; tap to reveal
 * the full input/cached/output breakdown. Hidden entirely when no usage.
 */
function TokenBadge({ usage }: { usage: TokenUsage }) {
  const [open, setOpen] = useState(false);
  const total = (usage.inputTokens ?? 0) + (usage.outputTokens ?? 0);
  if (total === 0) return null;
  return (
    <button
      onClick={() => setOpen((v) => !v)}
      className="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] text-ink-muted transition-colors hover:bg-surface-2 hover:text-ink-soft"
      aria-label="Token usage"
    >
      <Coins className="size-3 opacity-60" />
      {open ? (
        <span className="tabular-nums">
          in {fmtTokens(usage.inputTokens)}
          {usage.cachedInputTokens ? ` (cached ${fmtTokens(usage.cachedInputTokens)})` : ""} · out{" "}
          {fmtTokens(usage.outputTokens)}
          {usage.reasoningOutputTokens ? ` · reasoning ${fmtTokens(usage.reasoningOutputTokens)}` : ""}
        </span>
      ) : (
        <span className="tabular-nums">{fmtTokens(total)} tok</span>
      )}
    </button>
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
  usage,
}: {
  role: "user" | "assistant";
  text: string;
  at: string;
  isFirst: boolean;
  isLast: boolean;
  usage?: TokenUsage | null;
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

      {/* Bubble. Assistant = a calm tonal surface with generous padding (not a
          heavy outlined slab); user = a soft, lightly-tinted accent container. */}
      <div
        className={cn(
          "max-w-[90%] px-4 py-3 text-[15px] leading-relaxed",
          // Shape: rounded on all corners except the "tail" corner (only first bubble)
          mine
            ? cn(
                "bg-accent-dim/60 text-ink",
                isFirst ? "rounded-2xl rounded-tr-md" : "rounded-2xl",
              )
            : cn(
                "bg-surface-2 text-ink",
                isFirst ? "rounded-2xl rounded-tl-md" : "rounded-2xl",
              ),
        )}
      >
        <p className="whitespace-pre-wrap break-words">{text}</p>
      </div>

      {/* Timestamp (+ subtle token badge) — only on last in a group */}
      {isLast && (at || usage) && (
        <span className="mt-1 flex items-center gap-1.5 text-[10px] text-ink-muted">
          {at && timeLabel(at)}
          {usage && <TokenBadge usage={usage} />}
        </span>
      )}
    </div>
  );
}

/** Stable identity for a timeline item, so React keeps DOM nodes pinned to the
 *  same message across 4s polls (index keys made bubbles shuffle/collapse). */
function keyFor(item: TimelineItem, i: number): string {
  switch (item.kind) {
    case "message":
      return `m:${item.message.id}`;
    case "approval":
      return `ap:${item.approval.id}`;
    case "task_state":
      return `ts:${item.taskId}`;
    case "notice":
      return `n:${item.notice.id}`;
    case "artifact":
      return `af:${item.artifact.id}`;
    default:
      return `i:${i}:${item.at}`;
  }
}

export function SessionTimeline({ items }: { items: TimelineItem[] }) {
  return (
    <div role="feed" aria-label="Session timeline" className="pb-4 pt-2">
      {items.map((item, i) => {
        const key = keyFor(item, i);
        // For message items, determine grouping context. We group ONLY within
        // the same turn (same role AND same task), so two adjacent turns never
        // merge into one bubble group and each turn keeps its own timestamp.
        // The message id is `${task_id}-u` / `${task_id}-a`; the turn is the
        // part before the trailing role suffix.
        if (item.kind === "message") {
          const turnOf = (m: TimelineItem) =>
            m.kind === "message" ? m.message.id.replace(/-[ua]$/, "") : null;
          const prev = i > 0 ? items[i - 1] : null;
          const next = i < items.length - 1 ? items[i + 1] : null;
          const turn = turnOf(item);
          const sameGroup = (other: TimelineItem | null) =>
            other?.kind === "message" &&
            other.message.role === item.message.role &&
            turnOf(other) === turn;
          return (
            <MessageBubble
              key={key}
              role={item.message.role}
              text={item.message.text}
              at={item.at}
              isFirst={!sameGroup(prev)}
              isLast={!sameGroup(next)}
              usage={item.usage}
            />
          );
        }

        if (item.kind === "task_state") {
          return (
            <div
              key={key}
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
              key={key}
              className="mx-4 my-1 flex items-center gap-2 px-1 py-0.5"
            >
              <Icon
                className={cn("size-3 shrink-0", NOTICE_TONE[item.notice.severity])}
              />
              <span className="text-[11px] font-medium text-ink-muted">
                {item.notice.kind}
              </span>
              <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-muted">
                {item.notice.text}
              </span>
              <span className="shrink-0 text-[10px] text-ink-muted">
                {timeLabel(item.at)}
              </span>
            </div>
          );
        }

        if (item.kind === "approval") {
          return <ApprovalCard key={key} approval={item.approval} at={item.at} />;
        }

        if (item.kind === "artifact") {
          return (
            <div
              key={key}
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
              key={key}
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
