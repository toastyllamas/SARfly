// SARfly ground-station service worker: caches the app shell so the UI loads
// offline in the field. Only ever runs in a secure context (see the
// registration guard in index.html) -- over plain HTTP it's never registered.
//
// Strategy:
//   - Live data (/api/, /ws, /tiles/) is NEVER handled here: the API must be
//     fresh, and map tiles already have their own server-side cache. Letting
//     those fall through to the network avoids serving stale detections or
//     shadowing the tile cache.
//   - The app shell (/, and /static/ assets incl. vendored Leaflet) is
//     cache-first so it opens instantly and works with no connectivity.
const CACHE = 'sarfly-shell-v1';
const SHELL = [
  '/',
  '/static/vendor/leaflet/leaflet.js',
  '/static/vendor/leaflet/leaflet.css',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // Never intercept live data or tiles -- let them hit the network directly.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws')
      || url.pathname.startsWith('/tiles/')) {
    return;
  }
  // App shell: cache-first, but refresh the cache in the background when online.
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const fetched = fetch(e.request)
        .then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return resp;
        })
        .catch(() => cached);
      return cached || fetched;
    })
  );
});
