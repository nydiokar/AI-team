/**
 * CaseResumePanel — the operator's control over a Case that stopped on quota.
 *
 * The gap this fills: when the account's quota window killed a Manager turn, the
 * Case had no visible state and no operator lever. It came back only if a
 * wait-group happened to satisfy later (so resumption looked random), and the
 * only manual option was forking a session — which is a NEW conversation on a
 * NEW Case, not a continuation.
 *
 * What this shows, in the order the decision is actually made:
 *   1. IS IT PAUSED, and on whose authority (the provider's own limit bit).
 *   2. WHEN does the window reopen (from telemetry, rendered in local time).
 *   3. WHAT WOULD IT COST — resuming hours later re-writes the whole prompt
 *      cache, which is the expense the operator kept paying by accident. Shown
 *      as an estimate with its basis, or an honest "not measurable" — never a
 *      made-up number.
 *   4. THE DECISION — approve/decline a proposal the harness raised, or resume
 *      directly. Both modes stay on the SAME Case:
 *        • in place      — one turn into the existing session (full history,
 *                          pays the cache rewrite)
 *        • fresh Manager — a new Manager rebuilt from the Case ledger (cheap;
 *                          the only option when the old session is gone)
 *
 * The resume itself is single-flight server-side, so pressing this while the
 * automatic path fires cannot produce two Managers — the loser reports
 * `resume_in_flight`.
 */
import { useState } from "react";
import { Clock, PauseCircle, PlayCircle, TriangleAlert } from "lucide-react";
import { Button } from "../ui/Button";
import { useCaseResumeState, useResumeCase } from "../../hooks/useWork";
import { useResolveApproval } from "../../hooks/useSessionActions";
import type { RawCaseResumeEstimate, RawCaseResumeQuota } from "../../transport/rawApi";
import { ApiError } from "../../transport/apiClient";
import { cn } from "../../lib/cn";

/** Local wall-clock render of a UTC instant (the whole point is "when can I go"). */
function localTime(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString(undefined, {
        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      });
}

/** The provider's verdict in the operator's words. Mirrors quota_window_state.evidence. */
function quotaLine(quota: RawCaseResumeQuota): string {
  switch (quota.evidence) {
    case "limit_reached":
      return `Quota spent — window reopens ${localTime(quota.reset_at)}`;
    case "reset_at_future":
      return `Quota at ${quota.used_percent ?? "?"}% — window reopens ${localTime(quota.reset_at)}`;
    case "below_limit":
      return `Quota available (${quota.used_percent ?? "?"}% used)`;
    case "reset_elapsed":
      return "Quota window has rolled over — available";
    default:
      return "No quota telemetry — availability unknown";
  }
}

/** The cost of resuming, or an honest statement that it cannot be measured. */
function estimateLine(estimate: RawCaseResumeEstimate | null): string {
  if (!estimate) return "Resume cost unknown.";
  if (!estimate.known) {
    if (estimate.reason === "no_recorded_turn") {
      return "Resume cost unknown — this session has no recorded turn telemetry.";
    }
    if (estimate.reason === "unknown_model_pricing" || estimate.reason === "no_model") {
      return `Resume rewrites the prompt cache (~${(estimate.cache_creation_tokens ?? 0).toLocaleString()} tokens) — model not priced, so no USD estimate.`;
    }
    return `Resume cost unknown (${estimate.reason || "no telemetry"}).`;
  }
  const tokens = (estimate.cache_creation_tokens ?? 0).toLocaleString();
  return `Resuming rewrites the prompt cache: ~${tokens} tokens ≈ $${(estimate.usd ?? 0).toFixed(2)} (estimated from this session's largest recent cache write).`;
}

const REASON_COPY: Record<string, string> = {
  resume_in_flight: "A resume for this pause is already running.",
  manager_busy: "The Manager is mid-turn — it is not stuck, so nothing to resume.",
  case_terminal: "This Case is closed.",
  no_manager_link: "This Case has no Manager session linked.",
  continuation_disabled: "Case continuation is disabled (CASE_CONTINUATION_ENABLED).",
  respawn_failed: "Could not spawn a fresh Manager — check node placement.",
  deliver_failed: "The resume turn could not be delivered.",
};

export function CaseResumePanel({ caseId }: { caseId: string }) {
  const state = useCaseResumeState(caseId);
  const resume = useResumeCase(caseId);
  const resolveApproval = useResolveApproval();
  const [note, setNote] = useState<string | null>(null);

  const data = state.data;
  if (!data) return null;

  const paused = data.paused;
  const pendingId =
    data.pending_approval?.status === "pending" ? data.pending_approval.id : null;
  const managerGone =
    !data.manager_session_id ||
    ["closed", "cancelled", "error"].includes(
      (data.manager_session_status ?? "").toLowerCase(),
    );
  // Nothing to decide and nothing wrong: keep the panel out of the way.
  if (!paused && !managerGone && !pendingId) return null;

  const blockedByQuota = paused && data.quota.exhausted;

  const run = async (mode: string) => {
    setNote(null);
    try {
      const out = await resume.mutateAsync({ mode });
      setNote(
        out.mode === "fresh_manager"
          ? "Fresh Manager spawned on this Case."
          : "Resume turn delivered to the existing Manager.",
      );
    } catch (err) {
      // `post()` surfaces the backend's stable machine `reason` as the error
      // message — map it to copy, never render raw prose.
      const reason = err instanceof ApiError ? err.message : "";
      setNote(REASON_COPY[reason] ?? `Resume refused${reason ? `: ${reason}` : ""}.`);
    }
  };

  const decide = async (decision: "approved" | "rejected") => {
    if (!pendingId) return;
    setNote(null);
    try {
      await resolveApproval.mutateAsync({ approvalId: pendingId, decision });
      setNote(
        decision === "approved"
          ? "Approved — resuming."
          : "Declined. The Case stays open; you can still resume it manually.",
      );
    } catch {
      setNote("Could not record the decision.");
    }
  };

  return (
    <div className="px-4 pt-6">
      <div
        className={cn(
          "card-elev rounded-2xl px-4 py-4",
          paused && "border border-amber-500/30",
        )}
      >
        <div className="flex items-center gap-2">
          {paused ? (
            <PauseCircle className="size-4 text-amber-300" />
          ) : (
            <TriangleAlert className="size-4 text-ink-muted" />
          )}
          <h3 className="text-[13px] font-semibold tracking-tight text-ink">
            {paused ? "Paused — quota" : "Manager unavailable"}
          </h3>
          {pendingId && (
            <span className="ml-auto rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-300">
              decision pending
            </span>
          )}
        </div>

        <div className="mt-2.5 space-y-1 text-[12px] text-ink-soft">
          <p className="flex items-center gap-1.5">
            <Clock className="size-3.5 shrink-0 text-ink-muted" />
            {quotaLine(data.quota)}
          </p>
          {paused && data.pause?.paused_at && (
            <p className="text-ink-muted">
              Interrupted {localTime(data.pause.paused_at)}
              {data.pause.error_class ? ` · ${data.pause.error_class}` : ""}
            </p>
          )}
          {managerGone && (
            <p className="text-ink-muted">
              Manager session {data.manager_session_status ?? "missing"} — a resume
              rebuilds it from the Case ledger.
            </p>
          )}
          <p className="text-ink-muted">{estimateLine(data.estimate)}</p>
        </div>

        {blockedByQuota ? (
          <p className="mt-3 border-t border-hairline/60 pt-3 text-[12px] text-ink-muted">
            Waiting for the window to reopen. You will be asked before anything is
            spent{data.auto ? " unless auto-resume clears it first" : ""}.
          </p>
        ) : (
          <div className="mt-3 border-t border-hairline/60 pt-3">
            {pendingId ? (
              <div className="flex items-center justify-between gap-3">
                <p className="min-w-0 text-[12px] text-ink-muted">
                  Quota is back. Resume this Case?
                </p>
                <div className="flex shrink-0 items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={resolveApproval.isPending}
                    onClick={() => void decide("rejected")}
                  >
                    Decline
                  </Button>
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={resolveApproval.isPending}
                    onClick={() => void decide("approved")}
                  >
                    Resume
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-between gap-3">
                <p className="min-w-0 text-[12px] text-ink-muted">
                  Continue this Case (same objective, not a fork)
                </p>
                <div className="flex shrink-0 items-center gap-2">
                  <Button
                    variant={data.recommended_mode === "in_place" ? "primary" : "outline"}
                    size="sm"
                    disabled={resume.isPending || managerGone}
                    title={
                      managerGone
                        ? "The existing session is gone — use Fresh Manager"
                        : "One turn into the existing Manager session"
                    }
                    onClick={() => void run("in_place")}
                  >
                    <PlayCircle className="size-4" />
                    In place
                  </Button>
                  <Button
                    variant={
                      data.recommended_mode === "fresh_manager" ? "primary" : "outline"
                    }
                    size="sm"
                    disabled={resume.isPending}
                    title="New Manager on the SAME Case, rebuilt from the ledger"
                    onClick={() => void run("fresh_manager")}
                  >
                    Fresh Manager
                  </Button>
                </div>
              </div>
            )}
            {note && <p className="mt-2 text-[12px] text-ink-soft">{note}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
