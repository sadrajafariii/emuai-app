// bud Service Worker — enables PWA offline support and install prompt
const CACHE  = 'bud-v1';
const ASSETS = ['/', '/static/logo.svg', '/static/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Network-first for API calls, cache-first for static assets
  if (e.request.url.includes('/api/') || e.request.url.includes('/ws')) return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
