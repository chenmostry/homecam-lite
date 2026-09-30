const CACHE_NAME = 'homecam-static-v1';
const STATIC_PATHS = Object.freeze([
  '/',
  '/index.html',
  '/styles.css',
  '/app.js',
  '/ice-queue.mjs',
  '/manifest.webmanifest',
  '/icon.svg',
]);

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(STATIC_PATHS);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name.startsWith('homecam-static-') && name !== CACHE_NAME).map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET' || request.url.startsWith('ws:') || request.url.startsWith('wss:')) return;
  const requestUrl = new URL(request.url);
  if (requestUrl.origin !== self.location.origin || !STATIC_PATHS.includes(requestUrl.pathname)) return;
  const staticUrl = new URL(requestUrl.pathname, self.location.origin).href;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(staticUrl);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok && response.type === 'basic') await cache.put(staticUrl, response.clone());
    return response;
  })());
});
