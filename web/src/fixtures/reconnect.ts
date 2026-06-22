/**
 * Failure + reconnect fixtures — UI-0 deliverable (spec §14 Phase 0). These
 * model the connectivity/recovery flow (spec §9): an offline gap, a stale
 * marker, and the reconcile step. No backend source — they drive ConnectionBanner
 * + RecoveryNotice component states.
 */
import type { ConnectionState } from "../domain/status";
import type { SystemNotice } from "../domain/events";

export interface ConnectionScenario {
  label: string;
  state: ConnectionState;
  /** True when the displayed data may be stale (spec §9.1 state_unknown). */
  stale: boolean;
  banner: string;
}

export const connectionScenarios: ConnectionScenario[] = [
  { label: "Healthy", state: "online", stale: false, banner: "" },
  {
    label: "Reconnecting",
    state: "reconnecting",
    stale: true,
    banner: "Reconnecting… showing last known state.",
  },
  {
    label: "Offline",
    state: "offline",
    stale: true,
    banner: "Offline — remote work continues. Cached view shown.",
  },
  {
    label: "State unknown",
    state: "state_unknown",
    stale: true,
    banner: "Connection restored — reconciling state…",
  },
];

/** A recovery notice rendered in the timeline after a reconnect (spec §9.3). */
export const recoveryNoticeFixture: SystemNotice = {
  id: "recovery-1",
  sessionId: "sess_gateway_ui",
  taskId: null,
  kind: "reconnect",
  text: "Reconnected. Reconciled 3 events; 1 marked stale.",
  severity: "warning",
  timestamp: "2026-06-22T10:44:10Z",
};

/** A task-failure fixture for ErrorCard states. */
export const failureNoticeFixture: SystemNotice = {
  id: "fail-1",
  sessionId: "sess_deploy_failed",
  taskId: "task_c3",
  kind: "timeout",
  text: "Deploy step failed: permission denied on /srv/gateway/bin.",
  severity: "error",
  timestamp: "2026-06-22T09:58:40Z",
};
