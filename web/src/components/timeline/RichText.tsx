import { parseRichText, type RichSegment } from "../../lib/richText";
import { cn } from "../../lib/cn";

/**
 * Renders agent text with rich segments lifted out of the prose (WebUI #31):
 *
 *   - inline `code`   → a subtle monospace chip
 *   - http(s) URLs    → a plain underlined link (opens in a new tab)
 *   - source refs     → a monospace "source" link, visually distinct from URLs
 *                       (accent-tinted, file icon); clickable when `onRef` is given
 *
 * The three styles are deliberately distinguishable: a URL reads as an external
 * link (underline), a source ref as a code location (monospace + accent), and
 * inline code as quiet emphasis. Whitespace and newlines are preserved by the
 * surrounding `whitespace-pre-wrap`, so this stays a drop-in for a plain <p>.
 */
export function RichText({
  text,
  onRef,
  className,
}: {
  text: string;
  /** Called when a source ref is tapped; renders refs as buttons when provided. */
  onRef?: (ref: { path: string; line: number | null; raw: string }) => void;
  className?: string;
}) {
  const segments = parseRichText(text);
  return (
    <p className={cn("whitespace-pre-wrap break-words", className)}>
      {segments.map((seg, i) => (
        <Segment key={i} seg={seg} onRef={onRef} />
      ))}
    </p>
  );
}

function Segment({
  seg,
  onRef,
}: {
  seg: RichSegment;
  onRef?: (ref: { path: string; line: number | null; raw: string }) => void;
}) {
  switch (seg.type) {
    case "text":
      return <>{seg.value}</>;

    case "code":
      return (
        <code className="rounded bg-surface-3/70 px-1 py-0.5 font-mono text-[0.85em] text-ink-soft">
          {seg.value}
        </code>
      );

    case "url":
      return (
        <a
          href={seg.href}
          target="_blank"
          rel="noopener noreferrer"
          className="break-all text-accent underline decoration-accent/40 underline-offset-2 hover:decoration-accent"
        >
          {seg.value}
        </a>
      );

    case "ref": {
      const cls =
        "inline rounded bg-accent-dim/40 px-1 py-0.5 font-mono text-[0.85em] text-accent";
      if (onRef) {
        return (
          <button
            type="button"
            onClick={() =>
              onRef({ path: seg.path, line: seg.line, raw: seg.value })
            }
            className={cn(cls, "hover:bg-accent-dim/70")}
            title={`Source: ${seg.value}`}
          >
            {seg.value}
          </button>
        );
      }
      return (
        <span className={cls} title={`Source: ${seg.value}`}>
          {seg.value}
        </span>
      );
    }

    default:
      return null;
  }
}
