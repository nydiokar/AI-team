import { NavLink } from "react-router-dom";
import { MessagesSquare, Activity, Briefcase } from "lucide-react";
import { cn } from "../../lib/cn";

const TABS = [
  { to: "/work", label: "Work", Icon: Briefcase },
  { to: "/sessions", label: "Sessions", Icon: MessagesSquare },
  { to: "/system", label: "System", Icon: Activity },
];

export function BottomNavigation() {
  return (
    <nav
      aria-label="Main navigation"
      className="sticky bottom-0 z-20 grid grid-cols-3 border-t border-hairline bg-surface-1/80 backdrop-blur-xl"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      {TABS.map(({ to, label, Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              "relative flex min-h-touch flex-col items-center justify-center gap-1 py-2 text-[11px] font-medium transition-colors",
              isActive ? "text-accent" : "text-ink-soft hover:text-ink",
            )
          }
        >
          {({ isActive }) => (
            <>
              {/* Material-3 active indicator: a soft tinted pill behind the icon,
                  not a thin technical tab line above it. */}
              <span
                className={cn(
                  "flex h-7 w-14 items-center justify-center rounded-full transition-colors",
                  isActive ? "bg-accent-dim" : "bg-transparent",
                )}
              >
                <Icon className="size-5" strokeWidth={isActive ? 2.4 : 1.8} />
              </span>
              {label}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}
