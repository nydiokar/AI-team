/**
 * System-alert canonical types — recorded by the external liveness probe
 * (~/scripts/aiteam-healthcheck.sh), which runs OUTSIDE the gateway process on
 * purpose so it can record an outage even when the gateway itself is
 * unresponsive. The gateway only ever reads this log (GET /api/system-alerts).
 */

export type SystemAlertKind = "process_down" | "unresponsive" | "degraded";

export interface SystemAlert {
  id: number;
  source: string;
  kind: SystemAlertKind;
  message: string;
  /** Best-effort diagnostic snippet (e.g. the blocking stack frame or a log
   *  tail) — the "what happened" detail, not just "it happened". */
  detail: string;
  openedAt: string;
  /** null while the outage is ongoing. */
  resolvedAt: string | null;
}
