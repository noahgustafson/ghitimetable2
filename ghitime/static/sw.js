/* GHI-TIME service worker.
 * Offline scope is ONLY the capture module: /capture and its assets are
 * precached and served cache-first; every other page is network-only by
 * design (they show the browser offline page / banner). Requires HTTPS —
 * tailscale serve terminates TLS on the MagicDNS hostname.
 */
var CACHE = "ghitime-capture-v1";
var PRECACHE = [
  "/capture",
  "/static/capture.js",
  "/static/style.css",
  "/static/htmx.min.js",
  "/manifest.webmanifest",
  "/static/icon-192.png",
  "/static/icon-512.png"
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) { return c.addAll(PRECACHE); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== "GET") return; // sync POSTs always hit the network
  var isCaptureAsset = PRECACHE.indexOf(url.pathname) !== -1;
  if (!isCaptureAsset) return; // everything else: network-only, no offline
  e.respondWith(
    caches.match(e.request).then(function (hit) {
      var refresh = fetch(e.request).then(function (resp) {
        if (resp.ok) caches.open(CACHE).then(function (c) { c.put(e.request, resp.clone()); });
        return resp;
      }).catch(function () { return hit; });
      return hit || refresh;
    })
  );
});
