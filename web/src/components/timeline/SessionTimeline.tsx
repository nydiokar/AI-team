import { useState, useRef } from "react";
import {
  CircleDot,
  ShieldQuestion,
  FileCode2,
  CheckCircle2,
  AlertCircle,
  Info,
  Loader2,
  Coins,
  Copy,
  Check,
} from "lucide-react";
import type { TimelineItem, TokenUsage } from "../../fixtures/timeline";
import type { ApprovalRequest } from "../../domain/models";
import { TaskStatusChip } from "../ui/StatusChip";
import { Button } from "../ui/Button";
import { cn } from "../../lib/cn";
import { useResolveApproval } from "../../hooks/useSessionActions";
import { RichText } from "./RichText";

/** DOM id for a user message bubble — shared with SessionDetailScreen so the
 *  "jump to message" controls can scroll straight to it. */
export function userAnchorId(messageId: string): string {
  return `user-msg-${messageId}`;
}

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

async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    // Clipboard API unavailable/blocked (older WebView, insecure context) —
    // fall back to the hidden-textarea + execCommand trick.
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
}

const LONG_PRESS_MS = 450;
const MOVE_CANCEL_PX = 10;

/** Char threshold past which an agent reply is collapsed to a preview. Long
 *  replies (including salvaged context-overflow progress) never flood the thread;
 *  the full text is one tap away. */
const REPLY_COLLAPSE_CHARS = 1200;

/**
 * Agent reply text that collapses when long. Shows a preview + "Show full reply"
 * toggle. Short replies render as-is with no chrome. This is the in-chat
 * summary→full affordance: the whole message is always present client-side
 * (nothing is dropped), just visually collapsed until expanded.
 */
function ExpandableRichText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > REPLY_COLLAPSE_CHARS;
  if (!long) return <RichText text={text} />;

  // Snap the preview to a paragraph/line boundary so it doesn't cut mid-sentence.
  const slice = text.slice(0, REPLY_COLLAPSE_CHARS);
  const cut = Math.max(slice.lastIndexOf("\n\n"), slice.lastIndexOf("\n"));
  const preview = cut > REPLY_COLLAPSE_CHARS / 2 ? slice.slice(0, cut) : slice;

  return (
    <div>
      <RichText text={open ? text : preview.trimEnd()} />
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="mt-2 text-[12px] font-medium text-accent hover:underline"
      >
        {open
          ? "Show less"
          : `Show full reply (${(text.length / 1000).toFixed(1)}k chars)`}
      </button>
    </div>
  );
}

/**
 * Grouped message bubble. When consecutive messages share a role we tighten
 * vertical spacing and suppress the role label on all but the first. The
 * timestamp only shows on the last bubble in a group (cleaner, less noise).
 *
 * Long-press (or right-click) selects the WHOLE bubble — Telegram-style —
 * and surfaces a Copy action, rather than relying on native text selection
 * (which only grabs a fragment and fights the touch scroller).
 */
function MessageBubble({
  id,
  role,
  text,
  at,
  isFirst,
  isLast,
  usage,
}: {
  id?: string;
  role: "user" | "assistant";
  text: string;
  at: string;
  isFirst: boolean;
  isLast: boolean;
  usage?: TokenUsage | null;
}) {
  const mine = role === "user";
  const [menuOpen, setMenuOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const pressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pressStart = useRef<{ x: number; y: number } | null>(null);

  const clearPressTimer = () => {
    if (pressTimer.current) {
      clearTimeout(pressTimer.current);
      pressTimer.current = null;
    }
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    pressStart.current = { x: e.clientX, y: e.clientY };
    clearPressTimer();
    pressTimer.current = setTimeout(() => {
      setMenuOpen(true);
      navigator.vibrate?.(12);
    }, LONG_PRESS_MS);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!pressStart.current) return;
    const dx = e.clientX - pressStart.current.x;
    const dy = e.clientY - pressStart.current.y;
    if (Math.hypot(dx, dy) > MOVE_CANCEL_PX) clearPressTimer();
  };

  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setMenuOpen(true);
  };

  const handleCopy = () => {
    copyToClipboard(text);
    setCopied(true);
    setTimeout(() => {
      setMenuOpen(false);
      setCopied(false);
    }, 900);
  };

  return (
    <div
      id={id}
      className={cn(
        "flex flex-col px-4 scroll-mt-16",
        mine ? "items-end" : "items-start",
        isFirst ? "mt-3" : "mt-0.5",
      )}
    >
      {/* Role label — only on first in a group */}
      {isFirst && (
        <span
          className={cn(
            "mb-1 text-[11px] font-medium",
            mine ? "text-user-label" : "text-ink-muted",
          )}
        >
          {mine ? "You" : "Agent"}
        </span>
      )}

      {/* Bubble. Assistant = a calm tonal surface with generous padding (not a
          heavy outlined slab); user = a flat, bright lilac fill — no gradient,
          no glow, just a confident block of color that reads as yours.
          Long-press/right-click selects the whole thing and offers Copy. */}
      <div
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={clearPressTimer}
        onPointerCancel={clearPressTimer}
        onPointerLeave={clearPressTimer}
        onContextMenu={onContextMenu}
        className={cn(
          "relative max-w-[90%] select-none px-4 py-3 text-[15px] leading-relaxed transition-transform",
          menuOpen && "scale-[0.97]",
          // Shape: rounded on all corners except the "tail" corner (only first bubble)
          mine
            ? cn(
                "bg-user-bubble text-user-text border-r-[3px] border-r-user-border",
                isFirst ? "rounded-2xl rounded-tr-md" : "rounded-2xl",
              )
            : cn(
                "bg-surface-2 text-ink border-l-[3px] border-l-accent/50",
                isFirst ? "rounded-2xl rounded-tl-md" : "rounded-2xl",
              ),
          menuOpen && (mine ? "ring-2 ring-user-border" : "ring-2 ring-accent"),
        )}
      >
        {/* Agent output gets rich formatting (code/links/source refs); the user's
            own message is echoed verbatim as plain text. Long agent replies are
            collapsed to a preview with an inline "Show full reply" toggle so a
            large turn never floods the thread — the full text stays one tap away. */}
        {mine ? (
          <p className="whitespace-pre-wrap break-words">{text}</p>
        ) : (
          <ExpandableRichText text={text} />
        )}

        {menuOpen && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setMenuOpen(false)} />
            <div
              className={cn(
                "absolute -top-11 z-50 flex items-center gap-1 rounded-full border border-hairline bg-surface-3 p-1 shadow-xl",
                mine ? "right-0" : "left-0",
              )}
            >
              <button
                type="button"
                onClick={handleCopy}
                className="flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] font-medium text-ink hover:bg-surface-2"
              >
                {copied ? (
                  <>
                    <Check className="size-3.5 text-ok" /> Copied
                  </>
                ) : (
                  <>
                    <Copy className="size-3.5" /> Copy
                  </>
                )}
              </button>
            </div>
          </>
        )}
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
              id={item.message.role === "user" ? userAnchorId(item.message.id) : undefined}
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
