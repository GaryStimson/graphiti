/* Memory Manager service worker.
 *
 * Caches only the static app shell so the app installs and opens quickly. It never
 * touches /api or /auth: memories are always fetched live and are never stored by the
 * service worker, so nothing private is readable offline or after sign-out.
 */
const VERSION = 'v2';
const CACHE = `memory-ui-${VERSION}`;
const SHELL = [
  '/',
  '/assets/style.css',
  '/assets/app.js',
  '/assets/vendor/cytoscape.min.js',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png',
  '/icons/favicon-64.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('memory-ui-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/') || url.pathname === '/healthz') return;

  // Network first, so a new release is picked up on the next load; the cached copy is
  // only used when the server cannot be reached (offline start of the installed app).
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && request.mode !== 'navigate') {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((cached) => cached || (request.mode === 'navigate' ? caches.match('/') : Response.error())))
  );
});
