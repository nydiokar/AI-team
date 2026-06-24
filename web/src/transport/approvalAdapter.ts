/**
 * RawApproval (approvals table row) → canonical ApprovalRequest (Move H / UI-3).
 *
 * The domain object (domain/models.ts) was defined in UI-0 as the contract shape;
 * Move H is the backend that finally produces it. The mapping is mostly direct;
 * the int `reversible` (SQLite 0|1) becomes a bool, and `risk` is narrowed to the
 * domain union with a safe default. `affectedFiles` has no backend source yet
 * (the gated action's files aren't enumerated) → empty; `stale` is false (a live
 * pending row by definition).
 */
import type { ApprovalRequest } from "../domain/models";
import type { RawApproval } from "./rawApi";

function narrowRisk(risk: string): ApprovalRequest["risk"] {
  return risk === "low" || risk === "high" ? risk : "medium";
}

export function toApproval(raw: RawApproval): ApprovalRequest {
  return {
    id: raw.id,
    sessionId: raw.session_id ?? "",
    taskId: raw.task_id,
    targetId: null,
    action: raw.action,
    affectedFiles: [],
    risk: narrowRisk(raw.risk),
    reversible: raw.reversible === 1,
    stale: false,
    expiresAt: raw.expires_at,
    createdAt: raw.created_at,
  };
}

export function toApprovals(raws: RawApproval[]): ApprovalRequest[] {
  return raws.map(toApproval);
}
