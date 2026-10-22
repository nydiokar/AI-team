/**
 * BackendLogo — a small, recognizable mark per coding backend.
 *
 * These are ORIGINAL glyphs (an asterisk-spark, a hexagon knot, a terminal
 * prompt) tinted in each backend's signature colour — not reproductions of the
 * vendors' trademarked logos. Recognition comes from the colour + form + the
 * adjacent name label, so the mark never carries meaning by itself.
 */
import { cn } from "../../lib/cn";

export type BackendKind = "claude" | "codex" | "opencode" | "unknown";

export function normalizeBackend(name: string): BackendKind {
  const n = name.toLowerCase();
  if (n.includes("claude")) return "claude";
  if (n.includes("codex")) return "codex";
  if (n.includes("opencode")) return "opencode";
  return "unknown";
}

const TINT: Record<BackendKind, { bg: string; fg: string }> = {
  claude: { bg: "bg-[#2a1b12]", fg: "text-[#d97757]" }, // warm clay
  codex: { bg: "bg-[#0f1713]", fg: "text-[#10a37f]" }, // teal-green
  opencode: { bg: "bg-[#1a160a]", fg: "text-[#e2b43a]" }, // amber
  unknown: { bg: "bg-surface-3", fg: "text-ink-soft" },
};

function Glyph({ kind }: { kind: BackendKind }) {
  switch (kind) {
    case "claude": // an original radial spark
      return (
        <svg viewBox="0 0 24 24" fill="none" className="size-[15px]" aria-hidden>
          <g stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6L5.6 18.4" />
          </g>
        </svg>
      );
    case "codex": // an original hexagon knot
      return (
        <svg viewBox="0 0 24 24" fill="none" className="size-[15px]" aria-hidden>
          <path
            d="M12 3l7.8 4.5v9L12 21l-7.8-4.5v-9L12 3z"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinejoin="round"
          />
          <circle cx="12" cy="12" r="2.4" fill="currentColor" />
        </svg>
      );
    case "opencode": // an original terminal prompt
      return (
        <svg viewBox="0 0 24 24" fill="none" className="size-[15px]" aria-hidden>
          <g stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 7l4 5-4 5" />
            <path d="M12 17h7" />
          </g>
        </svg>
      );
    default:
      return (
        <svg viewBox="0 0 24 24" className="size-[15px]" aria-hidden>
          <circle cx="12" cy="12" r="7" fill="currentColor" opacity="0.5" />
        </svg>
      );
  }
}

export function BackendLogo({ backend, size = 7 }: { backend: string; size?: 6 | 7 | 8 }) {
  const kind = normalizeBackend(backend);
  const tint = TINT[kind];
  const box = size === 6 ? "size-6" : size === 8 ? "size-8" : "size-7";
  return (
    <span
      className={cn("flex shrink-0 items-center justify-center rounded-lg", box, tint.bg, tint.fg)}
      title={backend}
    >
      <Glyph kind={kind} />
    </span>
  );
}
