/**
 * Real session timeline — the conversation, honestly.
 *
 * REWRITTEN after live review found the old version showed no actual messages
 * (only optimistic-typed text + raw SSE operational events rendered as chat, with
 * stale "Running" pills). The conversation now comes from the server-reconstructed
 * transcript (GET /api/sessions/{id}/messages): each turn → a USER instruction
 * bubble + an ASSISTANT result bubble. That is the ChatGPT-style thread.
 *
 * Sources, in render order:
 *   1. Transcript turns (real history)        — user instruction → assistant result.
 *   2. Optimistic user messages not yet in the transcript (just-typed, no backend
 *      echo yet) — so a send shows instantly, then de-dupes once the turn lands.
 *   3. Pending approvals for this session (Move H) — actionable cards.
 *   4. A SINGLE live "running" indicator, ONLY when session.opState === "running"
 *      RIGHT NOW. We no longer splice the rolling SSE buffer into the chat — that
 *      was the source of stale/false "Running" rows and operational bloat. The raw
 *      event feed lives on the System screen (UI-5), not in the conversation.
 */
import { useMemo } from "react";
import type { TimelineItem } from "../fixtures/timeline";
import type { Session, ApprovalRequest } from "../domain/models";
import type { RawTranscriptTurn } from "../transport/rawApi";
import { useSentStore } from "../stores/sentStore";

export function useSessionTimeline(
  sessionId: string | undefined,
  session: Session | undefined,
  turns: RawTranscriptTurn[] = [],
  approvals: ApprovalRequest[] = [],
): TimelineItem[] {
  const sent = useSentStore((s) =>
    sessionId ? s.bySession[sessionId] : undefined,
  );

  return useMemo(() => {
    if (!sessionId) return [];
    const items: TimelineItem[] = [];

    // 1 — real conversation turns. Each task is one exchange.
    const seenInstructions = new Set<string>();
    for (const t of turns) {
      // Distinct anchors: the USER bubble is stamped when the turn STARTED (when
      // the message was sent); the ASSISTANT bubble when the reply LANDED (start +
      // time spent working). Older turns without the split fall back to the single
      // coarse `timestamp` so nothing renders blank.
      const startedAt = t.started_at || t.timestamp || "";
      const completedAt = t.completed_at || t.timestamp || "";
      if (t.instruction) {
        seenInstructions.add(t.instruction.trim());
        items.push({
          kind: "message",
          at: startedAt,
          message: {
            id: `${t.task_id}-u`,
            sessionId,
            role: "user",
            text: t.instruction,
            createdAt: startedAt,
          },
        });
      }
      if (t.result) {
        const u = t.usage;
        items.push({
          kind: "message",
          at: completedAt,
          message: {
            id: `${t.task_id}-a`,
            sessionId,
            role: "assistant",
            text: t.result,
            createdAt: completedAt,
          },
          usage: u
            ? {
                inputTokens: u.input_tokens,
                cachedInputTokens: u.cached_input_tokens,
                outputTokens: u.output_tokens,
                reasoningOutputTokens: u.reasoning_output_tokens,
              }
            : null,
        });
      }
    }

    // 2 — optimistic user messages not yet reflected in the transcript. Dedupe
    //     against transcript instructions so a sent message doesn't double once
    //     its turn completes and the poll picks it up. Match is truncation-tolerant
    //     in BOTH directions: an artifact's instruction can be a truncated prefix
    //     of the full optimistic text (task.title cap), or — with the summary
    //     overlay — an exact match. So treat them as the same turn if either is a
    //     prefix of the other (after trimming any trailing ellipsis).
    const norm = (s: string) => s.trim().replace(/[.…]+$/, "").trim();
    const seen = [...seenInstructions].map(norm);
    const isDup = (text: string) => {
      const t = norm(text);
      return seen.some((s) => s.startsWith(t) || t.startsWith(s));
    };
    for (const m of sent ?? []) {
      if (isDup(m.text)) continue;
      items.push({
        kind: "message",
        at: m.createdAt,
        message: {
          id: m.id,
          sessionId,
          role: "user",
          text: m.text,
          createdAt: m.createdAt,
        },
      });
    }

    // 3 — pending approvals (durable, round-trip via the card buttons). Splice
    //     each into its chronological slot WITHOUT re-sorting the message stream:
    //     insert before the first message whose `at` is later than the approval.
    for (const appr of approvals) {
      if (appr.sessionId !== sessionId) continue;
      const card: TimelineItem = { kind: "approval", at: appr.createdAt, approval: appr };
      const idx = items.findIndex((it) => it.at && appr.createdAt && it.at > appr.createdAt);
      if (idx === -1) items.push(card);
      else items.splice(idx, 0, card);
    }

    // NOTE: we deliberately do NOT sort the message stream by `at`. The turns
    // arrive from the backend already oldest→newest, and each turn's user +
    // assistant bubble share ONE coarse artifact timestamp — so sorting by `at`
    // would (a) be a no-op at best and (b) actively scramble order whenever a
    // newly-sent message's wall-clock `createdAt` disagrees with the artifact's
    // server timestamp (clock skew / tz), flipping the answer above the question
    // and collapsing turns. Insertion order IS the chronology. Below we only
    // *splice in* approvals at their chronological spot, preserving message order.

    // 4 — ONE live running indicator, only if the session is actually running
    //     now (honest state from the live snapshot — never a buffered event).
    if (session?.opState === "running") {
      items.push({
        kind: "task_state",
        at: session.updatedAt,
        taskId: session.lastTaskId ?? "current",
        state: "running",
        objective: "Working…",
      });
    }

    return items;
  }, [sessionId, session, turns, sent, approvals]);
}
