/**
 * MobileAppShell — root layout. A phone-width column on small viewports with a
 * wider content frame on desktop. Connection banner + bottom nav persist on root
 * screens; the routed screen scrolls.
 */
import type { ReactNode } from "react";
import { BottomNavigation } from "./BottomNavigation";
import { ConnectionBanner } from "./ConnectionBanner";

export function MobileAppShell({ children }: { children: ReactNode }) {
  return (
    <div className="desktop-frame mx-auto flex h-full max-w-[480px] flex-col bg-base">
      <ConnectionBanner />
      <main className="flex-1 overflow-y-auto overscroll-contain">{children}</main>
      <BottomNavigation />
    </div>
  );
}
