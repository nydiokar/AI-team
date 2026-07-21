/**
 * Build the fork carry-over digest from hand-picked ("marked") messages.
 *
 * The digest is a verbatim, human-readable transcript of the selected messages in
 * timeline order — "You: …" / "Agent: …" — that the backend injects once as a
 * reference-only `<prior_context>` block on the forked session's first turn.
 *
 * It is bounded client-side to keep the request sane; the backend independently
 * re-clamps the assembled prior-context block and fence-defuses the content, so
 * this cap is a courtesy, not the security boundary. When a selection exceeds the
 * cap we keep the MOST RECENT tail (drop from the front) — for continuing work the
 * latest turns matter most, so chopping the end (the old behavior) was backwards.
 */

/** Client-side cap on the raw digest (chars). A generous working budget (~12k
 *  tokens) so a normal fork carries in full; matches the API `continue_inline`
 *  field limit and the backend's prior-context clamp. */
export const FORK_DIGEST_MAX_CHARS = 48000;

export interface MarkedMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
}

export function buildForkDigest(messages: MarkedMessage[]): string {
  const parts = messages.map((m) => {
    const who = m.role === "user" ? "You" : "Agent";
    return `${who}: ${(m.text ?? "").trim()}`;
  });
  const joined = parts.join("\n\n");
  if (joined.length <= FORK_DIGEST_MAX_CHARS) return joined;
  // Keep the tail (most recent). Mark that earlier content was dropped.
  const marker = "…(earlier context truncated)…\n";
  return marker + joined.slice(joined.length - (FORK_DIGEST_MAX_CHARS - marker.length));
}
