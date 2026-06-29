/**
 * Rich-text tokenizer for agent output (WebUI feature #31).
 *
 * Agent replies arrive as plain text but routinely carry three things worth
 * lifting out of the prose so they read as first-class, tappable elements:
 *
 *   1. inline code     — `` `text` `` backtick spans (visually distinct chips)
 *   2. URLs            — bare http(s) links (a plain, underlined link)
 *   3. source refs     — `path:line` like `AudioProcessor.kt:25` or `src/a.py:12`
 *                        (a monospace "go to source" link, differentiated from URLs)
 *
 * This module is pure: it turns a string into an ordered list of typed segments.
 * Rendering (and what a source ref links to) lives in the RichText component, so
 * the parsing is unit-testable without React. Everything not matched stays a
 * plain `text` segment, so output is always a loss-less partition of the input.
 */

export type RichSegment =
  | { type: "text"; value: string }
  | { type: "code"; value: string } // inline `code` that is NOT a source ref
  | { type: "url"; value: string; href: string }
  | { type: "ref"; value: string; path: string; line: number | null };

// A source ref: a path-ish token (has a slash OR a dotted filename) optionally
// followed by :line (and :col, which we keep in `path` display but parse the
// first number as the line). We require either a directory separator or a file
// extension so we don't match bare "foo:25" English like "Step 2:25".
//   src/foo.py:12   AudioProcessor.kt:25   a/b/c.tsx:9:3   ./x.rs
const REF_CORE =
  "(?:[\\w.@~-]+[\\\\/])*[\\w.@~-]+\\.[A-Za-z][\\w]*" + // dotted filename, maybe with dirs
  "|" +
  "(?:[\\w.@~-]+[\\\\/])+[\\w.@~-]+"; // path with at least one separator (extension optional)
const REF_RE = new RegExp(`^(?:${REF_CORE})(?::(\\d+)(?::\\d+)?)?$`);

const URL_RE = /^https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/;

/** Parse a single backtick-or-paren-extracted token into a ref, else null. */
function asRef(token: string): Extract<RichSegment, { type: "ref" }> | null {
  const m = REF_RE.exec(token);
  if (!m) return null;
  // The path portion is everything before the first `:<digits>` line marker.
  const lineMatch = /:(\d+)(?::\d+)?$/.exec(token);
  const path = lineMatch ? token.slice(0, lineMatch.index) : token;
  const line = lineMatch ? Number(lineMatch[1]) : null;
  return { type: "ref", value: token, path, line };
}

/**
 * Scan free (non-backtick) text for URLs and bare source refs, emitting typed
 * segments. Backtick handling happens in the caller, which feeds only the
 * between-backtick runs here.
 */
function scanPlain(text: string): RichSegment[] {
  const out: RichSegment[] = [];
  let buf = "";
  const flush = () => {
    if (buf) out.push({ type: "text", value: buf });
    buf = "";
  };
  // Walk token-by-token on whitespace boundaries so URLs/refs are recognised as
  // whole words, while preserving the original whitespace between them.
  const parts = text.split(/(\s+)/);
  for (const part of parts) {
    if (!part || /^\s+$/.test(part)) {
      buf += part;
      continue;
    }
    // A token may be wrapped in parens, e.g. "(src/a.py:12)" — strip a single
    // leading "(" and trailing ")"/punctuation, keeping them as plain text.
    const lead = /^[([]+/.exec(part)?.[0] ?? "";
    const trail = /[)\].,;:!?]+$/.exec(part.slice(lead.length))?.[0] ?? "";
    const core = part.slice(lead.length, part.length - trail.length || undefined);

    let seg: RichSegment | null = null;
    if (URL_RE.test(core)) {
      seg = { type: "url", value: core, href: core };
    } else {
      seg = asRef(core);
    }

    if (seg) {
      buf += lead;
      flush();
      out.push(seg);
      buf += trail;
    } else {
      buf += part;
    }
  }
  flush();
  return out;
}

/**
 * Tokenize agent text into rich segments. Backtick spans are extracted first
 * (they win over URL/ref scanning inside the prose), and a backtick span whose
 * content is itself a source ref becomes a `ref` segment rather than `code`.
 */
export function parseRichText(text: string): RichSegment[] {
  const out: RichSegment[] = [];
  // Split on inline code spans. Unmatched/odd backticks fall through as text.
  const re = /`([^`]+)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(...scanPlain(text.slice(last, m.index)));
    const inner = m[1];
    const ref = asRef(inner.trim());
    out.push(ref ?? { type: "code", value: inner });
    last = re.lastIndex;
  }
  if (last < text.length) out.push(...scanPlain(text.slice(last)));
  return out;
}
