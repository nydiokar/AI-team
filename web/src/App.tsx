/**
 * App — routes the root tabs + the session detail route. Gated on a token
 * (TokenGate) before any /api/* call. Session detail is full-screen (no bottom
 * nav) per the back-stack model (spec §6.3).
 *
 * Screens are code-split (React.lazy): the initial download only carries the
 * shell + the first screen; the others fetch on navigation. A single Suspense
 * boundary covers both the outer detail routes and the shell's inner tabs.
 */
import { Suspense, lazy, useEffect } from "react";
import { Routes, Route, Navigate, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { MobileAppShell } from "./components/shell/MobileAppShell";
import { TokenGate } from "./components/shell/TokenGate";
import { useAuthStore } from "./stores/authStore";
import { EventStreamProvider } from "./hooks/eventStreamContext";
import { invalidateRouteTarget } from "./lib/liveInvalidation";

const SessionsScreen = lazy(() =>
  import("./screens/SessionsScreen").then((m) => ({ default: m.SessionsScreen })),
);
const SystemScreen = lazy(() =>
  import("./screens/SystemScreen").then((m) => ({ default: m.SystemScreen })),
);
const SessionDetailScreen = lazy(() =>
  import("./screens/SessionDetailScreen").then((m) => ({ default: m.SessionDetailScreen })),
);
const WorkScreen = lazy(() =>
  import("./screens/WorkScreen").then((m) => ({ default: m.WorkScreen })),
);
const WorkDetailScreen = lazy(() =>
  import("./screens/WorkDetailScreen").then((m) => ({ default: m.WorkDetailScreen })),
);
const CostScreen = lazy(() =>
  import("./screens/CostScreen").then((m) => ({ default: m.CostScreen })),
);

function ScreenFallback() {
  return (
    <div className="flex h-full items-center justify-center p-6 text-sm opacity-60">
      Loading…
    </div>
  );
}

export function App() {
  const hasToken = useAuthStore((s) => s.hasToken);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; url?: unknown } | null;
      if (!data || data.type !== "ai-team:notification-click") return;
      if (typeof data.url !== "string" || !data.url.startsWith("/")) return;
      const url = new URL(data.url, window.location.origin);
      if (url.origin !== window.location.origin) return;
      const target = `${url.pathname}${url.search}${url.hash}`;
      navigate(target);
      invalidateRouteTarget(queryClient, url.pathname);
    };

    navigator.serviceWorker?.addEventListener("message", onMessage);
    return () => navigator.serviceWorker?.removeEventListener("message", onMessage);
  }, [navigate, queryClient]);

  if (!hasToken) {
    return (
      <div className="desktop-frame mx-auto h-full max-w-[480px]">
        <TokenGate />
      </div>
    );
  }

  return (
    <EventStreamProvider>
      <Suspense fallback={<ScreenFallback />}>
        <Routes>
          {/* Full-screen detail — outside the bottom-nav shell. */}
          <Route path="/sessions/:id" element={<SessionDetailScreen />} />
          <Route path="/work/:id" element={<WorkDetailScreen />} />

          {/* Root tabs — inside the shell. */}
          <Route
            path="*"
            element={
              <MobileAppShell>
                <Routes>
                  <Route path="/work" element={<WorkScreen />} />
                  <Route path="/sessions" element={<SessionsScreen />} />
                  <Route path="/cost" element={<CostScreen />} />
                  <Route path="/tasks" element={<Navigate to="/system" replace />} />
                  <Route path="/system" element={<SystemScreen />} />
                  <Route path="*" element={<Navigate to="/sessions" replace />} />
                </Routes>
              </MobileAppShell>
            }
          />
        </Routes>
      </Suspense>
    </EventStreamProvider>
  );
}
