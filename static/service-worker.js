const CACHE_NAME = 'stockarmobile-pwa-v4';
const STATIC_ASSETS = [
  '/',
  '/offline.html',
  '/manifest.json',
  '/static/assets/css/styles.css',
  '/static/assets/js/cart-manager.js',
  '/static/assets/icons/icon-192.png',
  '/static/assets/icons/icon-512.png'
];
const API_CACHE_PREFIXES = ['/productos/api/products', '/clientes/api/clients', '/ventas/api/recent'];
const SYNCABLE_POST_PREFIXES = ['/ventas/api/checkout'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET') {
    if (SYNCABLE_POST_PREFIXES.some(prefix => url.pathname.startsWith(prefix))) {
      event.respondWith(queueWhenOffline(request));
      return;
    }
    event.respondWith(fetch(request));
    return;
  }
  if (API_CACHE_PREFIXES.some(prefix => url.pathname.startsWith(prefix))) {
    event.respondWith(networkFirst(request));
    return;
  }
  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request, true));
    return;
  }
  event.respondWith(cacheFirst(request));
});

self.addEventListener('sync', event => {
  if (event.tag === 'stockarmobile-sync') {
    event.waitUntil(flushQueue());
  }
});

self.addEventListener('message', event => {
  if (event.data && event.data.type === 'FLUSH_QUEUE') {
    event.waitUntil(flushQueue());
  }
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, response.clone());
  }
  return response;
}

async function networkFirst(request, offlineFallback = false) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    if (offlineFallback) return cache.match('/offline.html');
    return new Response(JSON.stringify({ offline: true, error: 'Sin conexion' }), { status: 503, headers: { 'Content-Type': 'application/json' } });
  }
}

async function queueWhenOffline(request) {
  try {
    return await fetch(request.clone());
  } catch (error) {
    const body = await request.clone().text();
    await queueRequest({ url: request.url, method: request.method, headers: Array.from(request.headers.entries()), body, createdAt: Date.now() });
    if ('sync' in self.registration) {
      await self.registration.sync.register('stockarmobile-sync');
    }
    return new Response(JSON.stringify({ queued: true }), { status: 202, headers: { 'Content-Type': 'application/json' } });
  }
}

function openQueueDB() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('stockarmobile-offline', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('requests', { keyPath: 'id', autoIncrement: true });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function queueRequest(payload) {
  const db = await openQueueDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('requests', 'readwrite');
    tx.objectStore('requests').add(payload);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function readQueue() {
  const db = await openQueueDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('requests', 'readonly');
    const request = tx.objectStore('requests').getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}

async function deleteQueued(id) {
  const db = await openQueueDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('requests', 'readwrite');
    tx.objectStore('requests').delete(id);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function flushQueue() {
  const queued = await readQueue();
  for (const item of queued) {
    try {
      const response = await fetch(item.url, { method: item.method, headers: item.headers, body: item.body || undefined });
      if (response.ok) {
        await deleteQueued(item.id);
      }
    } catch (error) {
      // Keep queued item for next background sync attempt.
    }
  }
}
