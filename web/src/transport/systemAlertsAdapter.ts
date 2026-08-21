import type { RawSystemAlert, RawSystemAlertsResponse } from "./rawApi";
import type { SystemAlert, SystemAlertKind } from "../domain/systemAlerts";

const KINDS: SystemAlertKind[] = ["process_down", "unresponsive", "degraded"];

export function toSystemAlerts(raw: RawSystemAlertsResponse): SystemAlert[] {
  const kind = (k: string | undefined): SystemAlertKind =>
    k && (KINDS as string[]).includes(k) ? (k as SystemAlertKind) : "degraded";
  return (raw.alerts ?? []).map((a: RawSystemAlert) => ({
    id: a.id,
    source: a.source ?? "healthcheck",
    kind: kind(a.kind),
    message: a.message ?? "",
    detail: a.detail ?? "",
    openedAt: a.opened_at,
    resolvedAt: a.resolved_at ?? null,
  }));
}
