/**
 * Display metadata for Work buckets and session affiliation roles.
 *
 * Pure presentation: maps authoritative bucket/role values → a label + a tone.
 * Tone reuses the StatusChip role vocabulary (running|ok|warn|bad|idle) so the
 * Work surface reads with the same color grammar as the rest of the app.
 * Color is never the sole signal — the label always carries the meaning.
 */
import type { WorkBucket, CaseSessionRole } from "../domain/work";

export type Tone = "running" | "ok" | "warn" | "bad" | "idle";

interface BucketMeta {
  label: string;
  /** Section eyebrow (slightly longer than the chip label). */
  section: string;
  tone: Tone;
}

// Order = attention priority on the inbox (what needs a human first).
export const BUCKET_ORDER: WorkBucket[] = [
  "needs_decision",
  "blocked",
  "review",
  "active",
  "closed",
  "unknown",
];

const BUCKET_META: Record<WorkBucket, BucketMeta> = {
  needs_decision: { label: "Decision", section: "Needs decision", tone: "warn" },
  blocked: { label: "Blocked", section: "Blocked / rework", tone: "bad" },
  review: { label: "Review", section: "In review", tone: "warn" },
  active: { label: "Active", section: "Active", tone: "running" },
  closed: { label: "Closed", section: "Recently closed", tone: "idle" },
  unknown: { label: "Unlinked", section: "Unlinked / unknown", tone: "idle" },
};

export function bucketMeta(bucket: WorkBucket): BucketMeta {
  return BUCKET_META[bucket] ?? BUCKET_META.unknown;
}

const ROLE_LABEL: Record<CaseSessionRole, string> = {
  manager: "Manager",
  worker: "Worker",
  reviewer: "Reviewer",
  evidence: "Evidence",
  session: "Session",
};

export function roleLabel(role: CaseSessionRole): string {
  return ROLE_LABEL[role] ?? "Session";
}

// A session's affiliation tone: managers/workers are active roles; reviewers
// gate; evidence is passive reference.
const ROLE_TONE: Record<CaseSessionRole, Tone> = {
  manager: "running",
  worker: "running",
  reviewer: "warn",
  evidence: "idle",
  session: "idle",
};

export function roleTone(role: CaseSessionRole): Tone {
  return ROLE_TONE[role] ?? "idle";
}

// Terminal (closed) case statuses — mirrors the backend authority set in
// work_read_model._CLOSED_STATUSES so the UI reads "closed" the same way the
// read model buckets it. A closed affiliated case is history, not active work.
const CLOSED_CASE_STATUSES = new Set([
  "closed",
  "superseded",
  "done",
  "complete",
  "completed",
]);

/** Whether an affiliated case's authoritative status is terminal (closed). */
export function isClosedCaseStatus(status: string | null | undefined): boolean {
  return CLOSED_CASE_STATUSES.has((status ?? "").trim().toLowerCase());
}

/** Humanize a flow_events event_type ("review.rework_requested" → "Rework requested"). */
export function eventTypeLabel(eventType: string | null): string {
  if (!eventType) return "Event";
  const tail = eventType.includes(".")
    ? eventType.slice(eventType.indexOf(".") + 1)
    : eventType;
  const words = tail.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
