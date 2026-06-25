import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { createSyncStoragePersister } from "@tanstack/query-sync-storage-persister";
import { App } from "./App";
import "./index.css";

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js", { scope: "/" });
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1500,
      refetchOnWindowFocus: true,
      // Keep cached data in memory for a day so navigating away from a chat and
      // back doesn't GC the messages → no blank-spinner reload on return.
      gcTime: 1000 * 60 * 60 * 24,
    },
  },
});

// localStorage persister. The `buster` is the build version, so any deploy
// invalidates the whole persisted cache — we never rehydrate a stale shape.
const persister = createSyncStoragePersister({
  storage: window.localStorage,
  key: "ai-team-rq-cache",
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <PersistQueryClientProvider
      client={queryClient}
      persistOptions={{
        persister,
        // 24h: stale persisted entries past this are discarded on rehydrate.
        maxAge: 1000 * 60 * 60 * 24,
        buster: __BUILD_VERSION__,
        dehydrateOptions: {
          // Only persist the data the user wants instantly on reopen — the
          // sessions list (incl. closed) and per-session conversations. The
          // high-churn polled lists (tasks/jobs/events/approvals) re-fetch live.
          shouldDehydrateQuery: (query) => {
            const root = query.queryKey[0];
            return root === "sessions" || root === "session-messages";
          },
        },
      }}
    >
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </PersistQueryClientProvider>
  </React.StrictMode>,
);
