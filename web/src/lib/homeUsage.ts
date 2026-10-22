/**
 * homeUsage — pure derivation of per-provider usage meters for the Home screen
 * from the observe-only /api/quota-windows read model.
 *
 * Usage/quota telemetry is scoped to a provider *account* (principal_hash), NOT
 * to a node — the gateway has no node→account binding today. So the Home screen
 * shows usage per account, honestly, and degrades to "unknown" rather than
 * fabricating a 0% when the provider exposes nothing (issue #235: "degrade
 * gracefully when usage is unknown/unavailable — don't show a fake 0%").
 */
import type {
  RawQuotaWindowsResponse,
  RawQuotaBucket,
  RawQuotaWindowState,
  RawQuotaSnapshot,
} from "../transport/rawApi";

/** A single usage window (e.g. the 5-hour or 7-day rolling limit). */
export interface UsageWindow {
  /** Human label derived from the window duration ("5h", "7d", …). */
  label: string;
  /** Ordering key (seconds) so meters render shortest-window first. */
  durationSeconds: number | null;
  /** 0–100, or null when the provider reports no number (→ render "unknown"). */
  usedPercent: number | null;
  /** True once usedPercent crosses the near-limit threshold. */
  nearLimit: boolean;
  /** Why a meter is unknown (adapter off, telemetry unavailable, no data). */
  unknownReason: string | null;
  /** ISO reset time when known. */
  resetAt: string | null;
}

export interface ProviderUsage {
  provider: string;
  /** Short, stable account hint (never a secret) — the principal hash prefix. */
  accountHint: string | null;
  windows: UsageWindow[];
}

export const NEAR_LIMIT_PERCENT = 80;

/** A human label for a rolling-window duration. */
export function windowLabel(seconds: number | null): string {
  if (seconds == null || seconds <= 0) return "window";
  const hours = seconds / 3600;
  // Snap the common provider windows to crisp labels.
  if (Math.abs(hours - 5) < 0.5) return "5h";
  if (Math.abs(seconds - 604800) < 3600) return "7d";
  if (hours < 24) return `${Math.round(hours)}h`;
  const days = Math.round(hours / 24);
  return `${days}d`;
}

function clampPercent(p: number | null | undefined): number | null {
  if (p == null || Number.isNaN(p)) return null;
  return Math.max(0, Math.min(100, p));
}

/**
 * Collapse the quota read model into per-provider usage meters.
 *
 * Prefers `window_states` (the richest view); falls back to joining
 * `latest_snapshots` with `buckets` when window_states is absent. Returns [] when
 * quota observation is disabled or nothing is known — the caller renders an
 * honest "telemetry off / unavailable" state rather than empty meters.
 */
export function summarizeProviderUsage(
  resp: RawQuotaWindowsResponse | undefined | null,
): ProviderUsage[] {
  if (!resp || !resp.enabled) return [];

  const bucketById = new Map<string, RawQuotaBucket>();
  for (const b of resp.buckets ?? []) bucketById.set(b.bucket_id, b);

  const byProvider = new Map<string, { accountHint: string | null; windows: UsageWindow[] }>();

  const push = (
    provider: string,
    principalHash: string,
    durationSeconds: number | null,
    usedPercent: number | null,
    unknownReason: string | null,
    resetAt: string | null,
  ) => {
    const key = provider;
    const entry =
      byProvider.get(key) ??
      { accountHint: principalHash ? principalHash.slice(0, 8) : null, windows: [] as UsageWindow[] };
    const pct = clampPercent(usedPercent);
    entry.windows.push({
      label: windowLabel(durationSeconds),
      durationSeconds,
      usedPercent: pct,
      nearLimit: pct != null && pct >= NEAR_LIMIT_PERCENT,
      unknownReason: pct == null ? unknownReason ?? "unavailable" : null,
      resetAt,
    });
    byProvider.set(key, entry);
  };

  const states: RawQuotaWindowState[] = resp.window_states ?? [];
  if (states.length > 0) {
    for (const s of states) {
      const bucket = bucketById.get(s.bucket_id);
      const unknown =
        s.used_percent == null
          ? s.telemetry_state || s.classification_status || "unavailable"
          : null;
      push(
        s.provider,
        s.principal_hash,
        bucket?.window_duration_seconds ?? null,
        s.used_percent,
        unknown,
        s.window_end_at,
      );
    }
  } else {
    for (const snap of resp.latest_snapshots ?? ([] as RawQuotaSnapshot[])) {
      const unknown =
        snap.used_percent == null ? snap.unavailable_reason || snap.raw_status || "unavailable" : null;
      push(
        snap.provider,
        snap.principal_hash,
        snap.window_duration_seconds ?? bucketById.get(snap.bucket_id)?.window_duration_seconds ?? null,
        snap.used_percent,
        unknown,
        snap.reset_at,
      );
    }
  }

  return [...byProvider.entries()]
    .map(([provider, v]) => ({
      provider,
      accountHint: v.accountHint,
      windows: v.windows.sort(
        (a, b) => (a.durationSeconds ?? Infinity) - (b.durationSeconds ?? Infinity),
      ),
    }))
    .sort((a, b) => a.provider.localeCompare(b.provider));
}
