/**
 * Bottom navigation — Sessions | Tasks | Files | System (spec §5). Stable on
 * root screens, frosted, 44px touch targets, safe-area aware. Active tab gets a
 * cyan glyph + a small top indicator bar.
 */
import { NavLink } from "react-router-dom";
import { MessagesSquare, ListChecks, FolderGit2, Activity } from "lucide-react";
import { cn } from "../../lib/cn";

const TABS = [
  { to: "/sessions", label: "Sessions", Icon: MessagesSquare },
  { to: "/tasks", label: "Tasks", Icon: ListChecks },
  { to: "/files", label: "Files", Icon: FolderGit2 },
  { to: "/system", label: "System", Icon: Activity },
];

export function BottomNavigation() {
  return (
    <nav
      className="sticky bottom-0 z-20 grid grid-cols-4 border-t border-hairline bg-surface-1/80 backdrop-blur-xl"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      {TABS.map(({ to, label, Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              "relative flex min-h-touch flex-col items-center justify-center gap-1 py-2.5 text-[11px] transition-colors",
              isActive ? "text-accent" : "text-ink-muted hover:text-ink-soft",
            )
          }
        >
          {({ isActive }) => (
            <>
              {isActive && (
                <span className="absolute top-0 h-0.5 w-8 rounded-full bg-accent" />
              )}
              <Icon className="size-5" strokeWidth={isActive ? 2.2 : 1.8} />
              {label}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}
