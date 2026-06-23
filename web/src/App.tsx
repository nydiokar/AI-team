/**
 * App — routes the four root tabs + the session detail route. Gated on a token
 * (TokenGate) before any /api/* call. Session detail is full-screen (no bottom
 * nav) per the back-stack model (spec §6.3).
 */
import { Routes, Route, Navigate } from "react-router-dom";
import { MobileAppShell } from "./components/shell/MobileAppShell";
import { TokenGate } from "./components/shell/TokenGate";
import { SessionsScreen } from "./screens/SessionsScreen";
import { SystemScreen } from "./screens/SystemScreen";
import { TasksScreen } from "./screens/TasksScreen";
import { FilesScreen } from "./screens/FilesScreen";
import { SessionDetailScreen } from "./screens/SessionDetailScreen";
import { useAuthStore } from "./stores/authStore";

export function App() {
  const hasToken = useAuthStore((s) => s.hasToken);

  if (!hasToken) {
    return (
      <div className="mx-auto h-full max-w-[480px]">
        <TokenGate />
      </div>
    );
  }

  return (
    <Routes>
      {/* Full-screen detail — outside the bottom-nav shell. */}
      <Route path="/sessions/:id" element={<SessionDetailScreen />} />

      {/* Root tabs — inside the shell. */}
      <Route
        path="*"
        element={
          <MobileAppShell>
            <Routes>
              <Route path="/sessions" element={<SessionsScreen />} />
              <Route path="/tasks" element={<TasksScreen />} />
              <Route path="/files" element={<FilesScreen />} />
              <Route path="/system" element={<SystemScreen />} />
              <Route path="*" element={<Navigate to="/sessions" replace />} />
            </Routes>
          </MobileAppShell>
        }
      />
    </Routes>
  );
}
