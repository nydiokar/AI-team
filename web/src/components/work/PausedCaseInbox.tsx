/**
 * PausedCaseInbox — "a Case is waiting on your decision", at the top of Work.
 *
 * A resume proposal is raised by the harness the moment quota telemetry says the
 * window reopened. Without this, that proposal only existed inside the Case
 * detail screen, so the operator had to already suspect something to find it —
 * which is how a paused Case used to sit for hours.
 *
 * Read from the ONE existing pending-approvals endpoint (no new backend, no
 * per-case fanout): the Case-level `case_resume` rows carry their case id,
 * objective excerpt and cost estimate in the approval payload. Deciding here is
 * the same durable resolve the Case panel uses — approving is what RUNS the
 * resume.
 */
import { Link } from "react-router-dom";
import { PlayCircle } from "lucide-react";
import { Button } from "../ui/Button";
import { useCaseResumeApprovals } from "../../hooks/useWork";
import { useResolveApproval } from "../../hooks/useSessionActions";

interface ResumePayload {
  case_id?: string;
  objective_excerpt?: string;
  estimate_usd?: number | null;
  estimate_known?: boolean;
  reset_at?: string | null;
  mode?: string;
}

function parsePayload(raw: string | null | undefined): ResumePayload {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as ResumePayload;
  } catch {
    return {};
  }
}

function costLabel(p: ResumePayload): string {
  if (p.estimate_known && typeof p.estimate_usd === "number") {
    return `≈ $${p.estimate_usd.toFixed(2)} to resume`;
  }
  return "resume cost not measurable";
}

export function PausedCaseInbox() {
  const { data: approvals } = useCaseResumeApprovals();
  const resolve = useResolveApproval();

  const items = (approvals ?? [])
    .map((a) => ({ id: a.id, payload: parsePayload(a.payload) }))
    .filter((a) => Boolean(a.payload.case_id));

  if (items.length === 0) return null;

  return (
    <div className="px-4 pt-4">
      <div className="rounded-xl border border-amber-500/30 bg-surface-1 px-3 py-3">
        <div className="flex items-center gap-2">
          <PlayCircle className="size-4 text-amber-300" />
          <p className="text-[13px] font-semibold text-ink">
            Quota is back — {items.length} case{items.length === 1 ? "" : "s"} waiting
          </p>
        </div>
        <div className="mt-2 space-y-2">
          {items.map((item) => (
            <div key={item.id} className="rounded-lg bg-surface-0/60 p-2">
              <Link
                to={`/work/${item.payload.case_id}`}
                className="block truncate text-[12px] text-ink-soft hover:text-ink"
              >
                {item.payload.objective_excerpt || item.payload.case_id}
              </Link>
              <div className="mt-1.5 flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[11px] text-ink-muted">
                  {costLabel(item.payload)}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={resolve.isPending}
                  onClick={() =>
                    resolve.mutate({ approvalId: item.id, decision: "rejected" })
                  }
                >
                  Later
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  disabled={resolve.isPending}
                  onClick={() =>
                    resolve.mutate({ approvalId: item.id, decision: "approved" })
                  }
                >
                  Resume
                </Button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
