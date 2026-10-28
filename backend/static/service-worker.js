const CACHE = "nexalert-ui-v2";
const OLD_CACHE_PREFIX = "nexalert-";
const SHELL = [
  "/", "/phone", "/dashboard",
  "/static/css/phone.css", "/static/css/dashboard.css",
  "/static/js/phone.js", "/static/js/dashboard.js", "/static/js/socket-client.js",
];
const OPTIONAL = [];

self.addEventListener("install", event => event.waitUntil((async () => {
  const cache = await caches.open(CACHE);
  await cache.addAll(SHELL);
  await Promise.all(OPTIONAL.map(async path => {
    try {
      const response = await fetch(path, {credentials: "same-origin"});
      if (response.ok) await cache.put(path, response);
    } catch (_) { /* Offline shell still installs if optional live features are absent. */ }
  }));
  await self.skipWaiting();
})()));

self.addEventListener("activate", event => event.waitUntil((async () => {
  const keys = await caches.keys();
  await Promise.all(keys.filter(key => key.startsWith(OLD_CACHE_PREFIX) && key !== CACHE).map(key => caches.delete(key)));
  await self.clients.claim();
})()));

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) return;
  const url = new URL(request.url);
  const cacheable = request.mode === "navigate" || url.pathname.startsWith("/static/");
  if (!cacheable) return; // API data is always fetched live; errors must stay visible.
  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response.ok) event.waitUntil(caches.open(CACHE).then(cache => cache.put(request, response.clone())));
      return response;
    } catch (_) {
      const cached = await caches.match(request);
      if (cached) return cached;
      if (request.mode === "navigate") {
        const shell = await caches.match("/");
        if (shell) return shell;
      }
      return Response.error();
    }
  })());
});
