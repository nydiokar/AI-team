/**
 * ApprovalsScreen — the real place to make decisions. The Home "N approvals to
 * review" pill lands HERE (not in the Work tab): a focused list of pending
 * approvals, each with its risk / affected files / which session asked, and
 * Approve / Reject actions resolved in place.
 */
import { useNavigate, Link } from "react-router-dom";
import { ChevronLeft, ShieldAlert, CheckCircle2 } from "lucide-react";
import { CompactTopBar } from "../components/shell/CompactTopBar";
import { Button } from "../components/ui/Button";
import { useApprovals } from "../hooks/useLiveData";
import { useResolveApproval } from "../hooks/useSessionActions";
import { cn } from "../lib/cn";
import type { ApprovalRequest } from "../domain/models";

const RISK_TINT: Record<ApprovalRequest["risk"], string> = {
  high: "text-bad",
  medium: "text-warn",
  low: "text-ink-soft",
};

function ApprovalCard({ approval }: { approval: ApprovalRequest }) {
  const resolve = useResolveApproval();
  const pending = resolve.isPending;
  return (
    <div className="rounded-2xl border border-hairline bg-surface-1 p-4">
      <div className="flex items-center gap-2">
        <ShieldAlert className={cn("size-4 shrink-0", RISK_TINT[approval.risk])} strokeWidth={2} />
        <span className={cn("text-[11px] font-semibold uppercase tracking-wide", RISK_TINT[approval.risk])}>
          {approval.risk} risk
        </span>
        {approval.reversible ? (
          <span className="text-[11px] text-ink-muted">· reversible</span>
        ) : (
          <span className="text-[11px] text-bad">· irreversible</span>
        )}
        {approval.stale ? <span className="ml-auto text-[11px] text-warn">stale</span> : null}
      </div>

      <p className="mt-2 text-sm text-ink">{approval.action}</p>

      {approval.affectedFiles.length > 0 ? (
        <ul className="mt-2 space-y-0.5">
          {approval.affectedFiles.slice(0, 5).map((f) => (
            <li key={f} className="truncate font-mono text-[11px] text-ink-muted">
              {f}
            </li>
          ))}
          {approval.affectedFiles.length > 5 ? (
            <li className="text-[11px] text-ink-muted">+{approval.affectedFiles.length - 5} more</li>
          ) : null}
        </ul>
      ) : null}

      {approval.sessionId ? (
        <Link
          to={`/sessions/${approval.sessionId}`}
          className="mt-2 inline-block font-mono text-[11px] text-accent"
        >
          session {approval.sessionId.slice(0, 8)} →
        </Link>
      ) : null}

      {resolve.isError ? (
        <p className="mt-2 text-[12px] text-bad">Couldn't resolve — try again.</p>
      ) : null}

      <div className="mt-3 flex gap-2">
        <Button
          variant="primary"
          size="sm"
          className="flex-1"
          disabled={pending}
          onClick={() => resolve.mutate({ approvalId: approval.id, decision: "approved" })}
        >
          Approve
        </Button>
        <Button
          variant="danger"
          size="sm"
          className="flex-1"
          disabled={pending}
          onClick={() => resolve.mutate({ approvalId: approval.id, decision: "rejected" })}
        >
          Reject
        </Button>
      </div>
    </div>
  );
}

export function ApprovalsScreen() {
  const navigate = useNavigate();
  const approvals = useApprovals("pending");
  const list = approvals.data ?? [];

  return (
    <div className="desktop-frame mx-auto flex h-full max-w-[480px] flex-col bg-base">
      <CompactTopBar
        title="Approvals"
        subtitle={list.length > 0 ? `${list.length} to review` : undefined}
        left={
          <button
            type="button"
            aria-label="Back"
            onClick={() => navigate(-1)}
            className="flex size-9 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
          >
            <ChevronLeft className="size-5" />
          </button>
        }
      />
      <main className="flex-1 space-y-2 overflow-y-auto overscroll-contain p-4">
        {approvals.isLoading ? (
          <div className="py-10 text-center text-sm text-ink-muted">Loading…</div>
        ) : list.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-16 text-center">
            <CheckCircle2 className="size-8 text-ok" strokeWidth={1.8} />
            <p className="text-sm text-ink-soft">No approvals waiting.</p>
            <p className="text-[12px] text-ink-muted">Decisions you need to make will appear here.</p>
          </div>
        ) : (
          list.map((a) => <ApprovalCard key={a.id} approval={a} />)
        )}
      </main>
    </div>
  );
}
