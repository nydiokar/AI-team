const CACHE = "ai-team-shell-v6";
const SHELL = ["/", "/index.html"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Never intercept cross-origin
  if (url.origin !== self.location.origin) return;

  // Hashed assets: cache-first
  if (url.pathname.startsWith("/assets/")) {
    e.respondWith(
      caches.match(e.request).then((hit) => {
        if (hit) return hit;
        return fetch(e.request).then((res) => {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
          return res;
        });
      })
    );
    return;
  }

  // API calls: network-only (no stale data)
  if (url.pathname.startsWith("/api/")) return;

  // Navigations + everything else: network-first, fall back to cached shell
  e.respondWith(
    fetch(e.request).catch(() =>
      caches.match("/index.html").then((shell) => shell || Response.error())
    )
  );
});

// --- Web Push (#21) ---
// Payload shape (sanitized by the backend PushService): { title, body, url, task_id, session_id }
self.addEventListener("push", (e) => {
  let data = {};
  try {
    data = e.data ? e.data.json() : {};
  } catch {
    data = { title: "AI-Team", body: e.data ? e.data.text() : "" };
  }
  const title = data.title || "AI-Team";
  const options = {
    body: data.body || "",
    // Large icon shown in the notification body (colored app mark).
    icon: "/icons/icon-192.png",
    // Small status-bar icon (Android). Must be a monochrome/transparent PNG —
    // the system tints it. This replaces the generic browser bell.
    badge: "/icons/badge-96.png",
    tag: data.session_id || data.task_id || "ai-team",
    data: { url: data.url || "/" },
  };
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(
    self.clients.matchAll({ type: "window" }).then((clients) => {
      // Prefer handing the deep link to an already-running app. The React app
      // performs in-SPA navigation and immediate query invalidation, so a tap
      // does not wait for the next poll cycle before showing fresh state.
      for (const client of clients) {
        if ("focus" in client) {
          client.postMessage &&
            client.postMessage({ type: "ai-team:notification-click", url: target });
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
