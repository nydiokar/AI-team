import { AlertTriangle, Clock3, Gauge, ShieldCheck } from "lucide-react";
import { SectionHeader } from "../ui/SectionHeader";
import { useQuotaWindows } from "../../hooks/useLiveData";
import type {
  RawQuotaBucket,
  RawQuotaSnapshot,
  RawQuotaWindowsResponse,
} from "../../transport/rawApi";
import { relAgeFrom } from "../../lib/time";

function pct(value: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)}%` : "unknown";
}

function resetLabel(value: string | null): string {
  if (!value) return "reset unknown";
  const t = new Date(value).getTime();
  if (!Number.isFinite(t)) return "reset unknown";
  const delta = t - Date.now();
  const abs = Math.abs(delta) / 1000;
  const unit =
    abs < 90
      ? "1m"
      : abs < 3600
        ? `${Math.round(abs / 60)}m`
        : abs < 86_400
          ? `${Math.round(abs / 3600)}h`
          : `${Math.round(abs / 86_400)}d`;
  return delta >= 0 ? `resets in ${unit}` : `reset ${relAgeFrom(value)}`;
}

function qualityTone(quality: string): "ok" | "warn" | "bad" | "idle" {
  if (quality === "authoritative") return "ok";
  if (quality === "partial") return "warn";
  if (quality === "malformed") return "bad";
  if (quality === "unsupported" || quality === "unavailable") return "idle";
  return "idle";
}

function toneClasses(tone: "ok" | "warn" | "bad" | "idle"): string {
  switch (tone) {
    case "ok":
      return "bg-ok/12 text-ok";
    case "warn":
      return "bg-warm-dim/70 text-warn";
    case "bad":
      return "bg-bad/12 text-bad";
    case "idle":
      return "bg-surface-3/70 text-ink-soft";
  }
}

function bucketKey(row: Pick<RawQuotaBucket | RawQuotaSnapshot, "provider" | "principal_hash" | "bucket_id">): string {
  return `${row.provider}\u0000${row.principal_hash}\u0000${row.bucket_id}`;
}

function snapshotMap(rows: RawQuotaSnapshot[]): Map<string, RawQuotaSnapshot> {
  return new Map(rows.map((row) => [bucketKey(row), row]));
}

function rowsFrom(data: RawQuotaWindowsResponse): Array<{ bucket: RawQuotaBucket | null; snapshot: RawQuotaSnapshot }> {
  const snapshots = snapshotMap(data.latest_snapshots);
  const rows: Array<{ bucket: RawQuotaBucket | null; snapshot: RawQuotaSnapshot }> = [];
  for (const bucket of data.buckets) {
    const snapshot = snapshots.get(bucketKey(bucket));
    if (snapshot && snapshot.telemetry_quality !== "unsupported") rows.push({ bucket, snapshot });
  }
  const bucketKeys = new Set(data.buckets.map(bucketKey));
  for (const snapshot of data.latest_snapshots) {
    if (snapshot.telemetry_quality !== "unsupported" && !bucketKeys.has(bucketKey(snapshot))) {
      rows.push({ bucket: null, snapshot });
    }
  }
  return rows;
}

function adapterLine(data: RawQuotaWindowsResponse): string {
  if (!data.enabled) return "observer off";
  const ready = data.adapters.filter((a) => a.status === "ready").length;
  const unavailable = data.adapters.filter((a) => a.status !== "ready").length;
  if (ready === 0 && unavailable === 0) return "no adapters";
  if (unavailable === 0) return `${ready} ready`;
  return `${ready} ready · ${unavailable} unavailable`;
}

function QuotaRow({ bucket, snapshot }: { bucket: RawQuotaBucket | null; snapshot: RawQuotaSnapshot }) {
  const quality = snapshot.telemetry_quality || bucket?.telemetry_quality || "unavailable";
  const tone = qualityTone(quality);
  const used = snapshot.used_percent;
  const width = typeof used === "number" && Number.isFinite(used) ? Math.max(0, Math.min(100, used)) : 0;
  const label = bucket?.bucket_name || snapshot.bucket_id.replace(/_/g, " ");
  const reason = snapshot.unavailable_reason || "";

  return (
    <div className="card-elev rounded-xl px-4 py-3">
      <div className="flex min-w-0 items-center gap-2">
        <Gauge className="size-4 shrink-0 text-ink-muted" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-medium text-ink">
            {snapshot.provider} · {label}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-ink-muted">
            {snapshot.bucket_id} · {bucket?.window_semantics || "unknown"}
          </div>
        </div>
        <span className={`shrink-0 rounded-full px-2 py-1 text-[11px] font-medium ${toneClasses(tone)}`}>
          {quality}
        </span>
      </div>

      <div className="mt-3 flex items-end justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="h-2 overflow-hidden rounded-full bg-surface-1">
            <div className="h-full rounded-full bg-running" style={{ width: `${width}%` }} />
          </div>
          <div className="mt-1 flex items-center gap-1.5 text-[11px] text-ink-muted">
            <Clock3 className="size-3" />
            <span className="truncate">
              {resetLabel(snapshot.reset_at)} · observed {relAgeFrom(snapshot.observed_at)}
            </span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[18px] font-semibold tabular-nums text-ink">{pct(used)}</div>
          <div className="text-[10px] uppercase text-ink-muted">used</div>
        </div>
      </div>

      {reason && (
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-ink-muted">
          <AlertTriangle className="size-3 text-warn" />
          <span className="min-w-0 truncate">{reason}</span>
        </div>
      )}
    </div>
  );
}

export function QuotaWindowPanel() {
  const { data, isLoading, error } = useQuotaWindows();
  const rows = data ? rowsFrom(data) : [];

  return (
    <>
      <SectionHeader
        label="Quota windows"
        count={rows.length || undefined}
        action={data ? <span className="text-[11px] text-ink-muted">{adapterLine(data)}</span> : undefined}
      />
      <div className="space-y-2 px-4">
        {isLoading && !data && (
          <div className="card-elev animate-pulse rounded-xl px-4 py-3">
            <div className="h-4 w-36 rounded bg-surface-2" />
            <div className="mt-3 h-2 rounded-full bg-surface-2" />
          </div>
        )}

        {error && (
          <div className="card-elev rounded-xl px-4 py-3 text-[12px] text-warn">
            Couldn't load quota windows.
          </div>
        )}

        {data && !data.enabled && (
          <div className="card-elev flex items-start gap-3 rounded-xl px-4 py-3">
            <ShieldCheck className="mt-0.5 size-4 shrink-0 text-ink-muted" />
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-ink">Observer off</div>
              <div className="mt-0.5 text-[12px] text-ink-muted">No quota DB, polling loop, or provider calls.</div>
            </div>
          </div>
        )}

        {data && data.enabled && rows.length === 0 && (
          <div className="card-elev rounded-xl px-4 py-3 text-[12px] text-ink-muted">
            No quota observations yet.
          </div>
        )}

        {rows.map(({ bucket, snapshot }) => (
          <QuotaRow key={bucketKey(snapshot)} bucket={bucket} snapshot={snapshot} />
        ))}
      </div>
    </>
  );
}
