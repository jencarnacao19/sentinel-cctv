/*
 * One-time retirement worker for Sentinel's former offline/PWA mode.
 *
 * CCTV state must never be served from an old browser cache. Existing clients
 * still ask for /sw.js, so this replacement activates immediately, removes
 * only caches owned by this origin, unregisters itself and reloads controlled
 * pages once. New builds do not register a service worker.
 */
self.addEventListener('install', () => self.skipWaiting())

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const cacheNames = await caches.keys()
      await Promise.all(cacheNames.map((name) => caches.delete(name)))
      await self.registration.unregister()

      const clients = await self.clients.matchAll({ type: 'window' })
      await Promise.all(clients.map((client) => client.navigate(client.url)))
    })(),
  )
})
