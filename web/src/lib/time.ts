/**
 * Human time helpers shared across the cockpit.
 *
 * `relAge` answers "how long ago?" in the coarsest unit that still reads at a
 * glance — seconds → minutes → hours → days → weeks. A node cold for two weeks
 * should say "2w ago", never "362h ago" (which forces the reader to do math).
 */
export function relAge(sec: number | null): string {
  if (sec == null) return "never seen";
  if (sec < 45) return "just now";
  if (sec < 90) return "1m ago";
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86_400) return `${Math.round(sec / 3600)}h ago`;
  if (sec < 604_800) return `${Math.round(sec / 86_400)}d ago`;
  return `${Math.round(sec / 604_800)}w ago`;
}

/** Same scale, but from an ISO timestamp (jobs/log rows). */
export function relAgeFrom(ts: string | null): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  return relAge((Date.now() - d.getTime()) / 1000);
}

/** "12:13 AM" — used to collapse runs of same-minute log rows. */
export function clockLabel(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
