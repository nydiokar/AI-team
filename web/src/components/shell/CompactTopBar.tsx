/**
 * Compact top bar (spec §12 shell). Frosted, sticky, hairline base. Title +
 * optional mono subtitle (target/backend context) + a right slot. Safe-area top.
 */
import type { ReactNode } from "react";

export function CompactTopBar({
  title,
  subtitle,
  left,
  right,
  onTitleClick,
}: {
  title: string;
  subtitle?: ReactNode;
  left?: ReactNode;
  right?: ReactNode;
  onTitleClick?: () => void;
}) {
  const titleBlock = (
    <>
      <h1 className="truncate text-[15px] font-semibold tracking-tight text-ink">
        {title}
      </h1>
      {subtitle && (
        <p className="truncate text-xs text-ink-muted">{subtitle}</p>
      )}
    </>
  );

  return (
    <header
      className="sticky top-0 z-20 flex items-center gap-3 border-b border-hairline bg-base/70 px-4 py-3 backdrop-blur-xl"
      style={{ paddingTop: "max(0.75rem, env(safe-area-inset-top))" }}
    >
      {left}
      {onTitleClick ? (
        <button
          type="button"
          onClick={onTitleClick}
          className="min-w-0 flex-1 text-left"
          aria-label="Open session info"
        >
          {titleBlock}
        </button>
      ) : (
        <div className="min-w-0 flex-1">{titleBlock}</div>
      )}
      {right}
    </header>
  );
}
