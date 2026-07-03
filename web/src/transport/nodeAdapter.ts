/**
 * RawNode → canonical Target. Liveness comes from the derived `live` flag +
 * `heartbeat_age_sec`, NEVER the stale `status` column (gap-doc §2 note —
 * dashboard derives `live` per-request; `status` is owned by another process).
 */
import type { Target, ActiveTaskDetail } from "../domain/models";
import type { TargetHealth } from "../domain/status";
import type { RawNode, RawLiveStateTask } from "./rawApi";

function parseBackends(b: string | string[]): string[] {
  if (Array.isArray(b)) return b;
  if (!b) return [];
  try {
    const parsed = JSON.parse(b);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

/**
 * health: we DON'T invent the spec's 4-state online/degraded/offline/unknown —
 * the backend can't substantiate `degraded`. We map the boolean truth:
 *   live=true            → online
 *   live=false, age known → offline
 *   live=false, age null  → unknown (never heard from / unparseable heartbeat)
 */
export function deriveHealth(raw: RawNode): TargetHealth {
  if (raw.live) return "online";
  if (raw.heartbeat_age_sec == null) return "unknown";
  return "offline";
}

function parseLiveStateTasks(raw: RawNode): ActiveTaskDetail[] {
  const ls = raw.live_state;
  if (!ls || !Array.isArray(ls.active_task_details)) return [];
  return ls.active_task_details.map((d: RawLiveStateTask) => ({
    taskId: d.task_id,
    backend: d.backend,
    action: d.action,
    phase: d.phase ?? "",
    startedAt: d.started_at ?? null,
  }));
}

export function toTarget(raw: RawNode): Target {
  const ls = raw.live_state ?? null;
  return {
    id: raw.node_id,
    health: deriveHealth(raw),
    live: raw.live,
    heartbeatAgeSec: raw.heartbeat_age_sec,
    backends: parseBackends(raw.backends),
    tailscaleIp: raw.tailscale_ip,
    maxConcurrent: raw.max_concurrent,
    slotsUsed: ls?.slots_used ?? null,
    slotsTotal: ls?.slots_total ?? null,
    activeTasks: parseLiveStateTasks(raw),
  };
}

export function toTargets(raws: RawNode[]): Target[] {
  return raws.map(toTarget);
}
