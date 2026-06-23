/**
 * Approval fixtures — no live source (backend M4 emits events but no object;
 * Move H, ❌ MISSING). These are hand-authored to the canonical ApprovalRequest
 * shape so UI-3 components can be built/tested against the contract now.
 */
import type { ApprovalRequest } from "../domain/models";

export const approvalFixtures: ApprovalRequest[] = [
  {
    id: "appr_1",
    sessionId: "sess_gateway_ui",
    taskId: "task_a1",
    targetId: "main-pc",
    action: "Apply patch: refactor event adapter (7 files)",
    affectedFiles: [
      "web/src/transport/eventAdapter.ts",
      "web/src/domain/events.ts",
    ],
    risk: "medium",
    reversible: true,
    stale: false,
    expiresAt: "2026-06-22T11:00:00Z",
    createdAt: "2026-06-22T10:43:00Z",
  },
  {
    id: "appr_2",
    sessionId: "sess_deploy_failed",
    taskId: "task_c3",
    targetId: "main-pc",
    action: "Run deploy script with sudo on /srv/gateway",
    affectedFiles: ["/srv/gateway/bin"],
    risk: "high",
    reversible: false,
    stale: true, // last-known state may be stale (spec §7.7)
    expiresAt: null,
    createdAt: "2026-06-22T09:56:00Z",
  },
];
