/**
 * MobileAppShell — root layout. A single phone-width column (mobile-source,
 * spec §2.1) with a faint vignette so the content sits in a "device". Connection
 * banner + bottom nav persist on root screens; the routed screen scrolls.
 */
import type { ReactNode } from "react";
import { BottomNavigation } from "./BottomNavigation";
import { ConnectionBanner } from "./ConnectionBanner";

export function MobileAppShell({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto flex h-full max-w-[480px] flex-col bg-base">
      <ConnectionBanner />
      <main className="flex-1 overflow-y-auto overscroll-contain">{children}</main>
      <BottomNavigation />
    </div>
  );
}
