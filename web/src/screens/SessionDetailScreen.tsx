/**
 * Active session screen (spec §7.2) — compact header with live status, the
 * chronological timeline, and a composer with the mock's gradient send button.
 * Header binds LIVE; timeline renders from fixtures (whole-message).
 *
 * Composer is DISABLED — write paths (send/stop) arrive with Move F (UI-2). The
 * backend is read-only in this scope; an enabled composer would promise a
 * delivery the gateway can't yet accept.
 */
import { useParams, useNavigate } from "react-router-dom";
import { ChevronLeft, Plus, ArrowUp } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { SessionStatusChip } from "../components/ui/StatusChip";
import { SessionTimeline } from "../components/timeline/SessionTimeline";
import { Button } from "../components/ui/Button";
import { useSessions } from "../hooks/useLiveData";
import { timelineFixture } from "../fixtures/timeline";

export function SessionDetailScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data } = useSessions();
  const session = data?.find((s) => s.id === id);

  return (
    <div className="mx-auto flex h-full max-w-[480px] flex-col bg-base">
      <CompactTopBar
        title={session?.id ?? id ?? "Session"}
        subtitle={
          session ? (
            <span className="font-mono">
              {session.backend} · {session.workspace.targetId}
            </span>
          ) : (
            "loading…"
          )
        }
        left={
          <button
            onClick={() => navigate("/sessions")}
            className="-ml-1 flex size-9 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Back to sessions"
          >
            <ChevronLeft className="size-5" />
          </button>
        }
        right={
          session && (
            <SessionStatusChip state={session.opState} closed={session.lifecycle === "closed"} />
          )
        }
      />

      <div className="flex-1 overflow-y-auto overscroll-contain">
        <SessionTimeline items={timelineFixture} />
      </div>

      {/* Composer — disabled in UI-1 (read-only backend; write = Move F). */}
      <div
        className="border-t border-hairline bg-surface-1/90 px-3 py-2.5 backdrop-blur-xl"
        style={{ paddingBottom: "max(0.625rem, env(safe-area-inset-bottom))" }}
      >
        <div className="flex items-end gap-2 opacity-70">
          <button
            disabled
            className="flex size-11 shrink-0 items-center justify-center rounded-full border border-hairline text-ink-muted"
            aria-label="Attachments (UI-2)"
          >
            <Plus className="size-5" />
          </button>
          <input
            disabled
            placeholder="Sending arrives in UI-2 (Move F)…"
            className="h-11 flex-1 rounded-full border border-hairline bg-base px-4 text-sm text-ink-muted outline-none"
          />
          <Button disabled size="icon" aria-label="Send">
            <ArrowUp className="size-5" />
          </Button>
        </div>
      </div>
    </div>
  );
}
