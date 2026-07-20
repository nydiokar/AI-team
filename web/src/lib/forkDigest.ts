/**
 * Build the fork carry-over digest from hand-picked ("marked") messages.
 *
 * The digest is a verbatim, human-readable transcript of the selected messages in
 * timeline order — "You: …" / "Agent: …" — that the backend injects once as a
 * reference-only `<prior_context>` block on the forked session's first turn.
 *
 * It is bounded client-side to keep the request small; the backend independently
 * hard-caps the assembled prior-context block (4KB) and fence-defuses the content,
 * so this cap is a courtesy, not the security boundary.
 */

/** Client-side cap on the raw digest (chars). The backend re-clamps to its own
 *  hard cap; this keeps the payload well under the API's 8KB field limit. */
export const FORK_DIGEST_MAX_CHARS = 6000;

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
  return joined.slice(0, FORK_DIGEST_MAX_CHARS).trimEnd() + "\n…(truncated)";
}
