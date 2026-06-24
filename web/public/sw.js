const CACHE = "ai-team-shell-v1";
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
