/**
 * StatusChip — the SIGNATURE element. A live status pill: a state dot (which
 * breathes when running) + an uppercase label. This is the one place
 * SessionOpState / TaskState / TargetHealth map to a color + label (spec §8.2).
 * Color is never the only signal — the label always carries the meaning
 * (acceptance #13, no color-only meaning).
 */
import { cn } from "../../lib/cn";
import type {
  SessionOpState,
  TaskState,
  TargetHealth,
} from "../../domain/status";

type Role = "running" | "ok" | "warn" | "bad" | "idle";

const ROLE: Record<Role, { dot: string; text: string; ring: string; pulse: boolean }> = {
  running: { dot: "bg-running text-running", text: "text-running", ring: "ring-running/25", pulse: true },
  ok: { dot: "bg-ok text-ok", text: "text-ok", ring: "ring-ok/25", pulse: false },
  warn: { dot: "bg-warn text-warn", text: "text-warn", ring: "ring-warn/30", pulse: true },
  bad: { dot: "bg-bad text-bad", text: "text-bad", ring: "ring-bad/30", pulse: false },
  idle: { dot: "bg-ink-muted text-ink-muted", text: "text-ink-soft", ring: "ring-hairline", pulse: false },
};

function opMap(s: SessionOpState): { role: Role; label: string } {
  switch (s) {
    case "running": return { role: "running", label: "Running" };
    case "waiting_for_input": return { role: "warn", label: "Waiting" };
    case "waiting_for_approval": return { role: "warn", label: "Needs approval" };
    case "failed_attention": return { role: "bad", label: "Failed" };
    case "idle": return { role: "idle", label: "Idle" };
  }
}

function taskMap(s: TaskState): { role: Role; label: string } {
  switch (s) {
    case "queued": return { role: "idle", label: "Queued" };
    case "dispatching": return { role: "running", label: "Dispatching" };
    case "running": return { role: "running", label: "Running" };
    case "waiting_for_input": return { role: "warn", label: "Waiting" };
    case "waiting_for_approval": return { role: "warn", label: "Needs approval" };
    case "succeeded": return { role: "ok", label: "Succeeded" };
    case "failed": return { role: "bad", label: "Failed" };
    case "cancelled": return { role: "idle", label: "Cancelled" };
    case "connection_unknown": return { role: "warn", label: "Unknown" };
  }
}

function healthMap(h: TargetHealth): { role: Role; label: string } {
  switch (h) {
    case "online": return { role: "ok", label: "Online" };
    case "offline": return { role: "bad", label: "Offline" };
    case "unknown": return { role: "warn", label: "Unknown" };
  }
}

function Pill({ role, label }: { role: Role; label: string }) {
  const r = ROLE[role];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full bg-surface-2/60 py-1 pl-2 pr-2.5 text-[11px] font-medium ring-1 ring-inset",
        r.text,
        r.ring,
      )}
    >
      <span className={cn("size-1.5 rounded-full", r.dot, r.pulse && "pulse-dot")} />
      {label}
    </span>
  );
}

export function SessionStatusChip({ state, closed }: { state: SessionOpState; closed?: boolean }) {
  if (closed) return <Pill role="idle" label="Closed" />;
  const { role, label } = opMap(state);
  return <Pill role={role} label={label} />;
}

export function TaskStatusChip({ state }: { state: TaskState }) {
  const { role, label } = taskMap(state);
  return <Pill role={role} label={label} />;
}

export function HealthChip({ health }: { health: TargetHealth }) {
  const { role, label } = healthMap(health);
  return <Pill role={role} label={label} />;
}

/** Bare status dot (no pill) for inline use, e.g. the target selector. */
export function StatusDot({ live }: { live: boolean }) {
  return (
    <span
      className={cn(
        "size-2 rounded-full",
        live ? "bg-ok text-ok pulse-dot" : "bg-ink-muted",
      )}
    />
  );
}
