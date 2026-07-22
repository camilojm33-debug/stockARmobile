const CACHE_NAME = 'stockarmobile-pwa-v6';
const STATIC_ASSETS = [
  '/',
  '/offline.html',
  '/manifest.json',
  '/static/assets/css/styles.css',
  '/static/assets/js/cart-manager.js',
  '/static/assets/js/landing.js',
  '/static/assets/js/edit-sale.js',
  '/static/assets/js/ventas-new.js',
  '/static/assets/js/offline-manager.js',
  '/static/images/branding/favicon.ico',
  '/static/images/branding/apple-touch-icon.png',
  '/static/images/branding/icon-192.png',
  '/static/images/branding/icon-256.png',
  '/static/images/branding/icon-384.png',
  '/static/images/branding/icon-512.png',
  '/static/images/branding/icon-maskable-512.png',
  '/static/images/branding/splash.png'
];
const API_CACHE_PREFIXES = ['/productos/api/products', '/clientes/api/clients', '/ventas/api/recent', '/api/search'];
const SYNCABLE_POST_PREFIXES = ['/ventas/api/checkout', '/productos/add', '/productos/edit/', '/clientes/add', '/clientes/edit/', '/compras/', '/gastos/', '/caja/'];
const DB_NAME = 'stockarmobile-offline';
const DB_VERSION = 2;
const REQUEST_STORE = 'requests';
const SNAPSHOT_STORE = 'snapshots';
const META_STORE = 'meta';
const LAST_SYNC_KEY = 'lastSyncAt';
const LAST_ERROR_KEY = 'lastError';

function nowIso() {
  return new Date().toISOString();
}

function generateUuid() {
  if (self.crypto && typeof self.crypto.randomUUID === 'function') {
    return self.crypto.randomUUID();
  }
  return `offline_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

function inferOperationType(pathname, method) {
  if (pathname.startsWith('/ventas/api/checkout')) return 'sale_create';
  if (pathname.startsWith('/productos/add') || pathname.startsWith('/productos/edit/')) return 'product_write';
  if (pathname.startsWith('/clientes/add') || pathname.startsWith('/clientes/edit/')) return 'client_write';
  if (pathname.startsWith('/compras/')) return 'purchase_write';
  if (pathname.startsWith('/gastos/')) return 'expense_write';
  if (pathname.startsWith('/caja/')) return 'cash_write';
  return `${method.toLowerCase()}_${pathname.split('/').filter(Boolean)[0] || 'request'}`;
}

function openOfflineDB() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(REQUEST_STORE)) {
        db.createObjectStore(REQUEST_STORE, { keyPath: 'id', autoIncrement: true });
      }
      if (!db.objectStoreNames.contains(SNAPSHOT_STORE)) {
        db.createObjectStore(SNAPSHOT_STORE, { keyPath: 'key' });
      }
      if (!db.objectStoreNames.contains(META_STORE)) {
        db.createObjectStore(META_STORE, { keyPath: 'key' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function dbGet(storeName, key) {
  const db = await openOfflineDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const request = tx.objectStore(storeName).get(key);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
  });
}

async function dbPut(storeName, value) {
  const db = await openOfflineDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    tx.objectStore(storeName).put(value);
    tx.oncomplete = () => resolve(true);
    tx.onerror = () => reject(tx.error);
  });
}

async function dbAdd(storeName, value) {
  const db = await openOfflineDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    const request = tx.objectStore(storeName).add(value);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function dbReadAll(storeName) {
  const db = await openOfflineDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const request = tx.objectStore(storeName).getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}

async function dbDelete(storeName, key) {
  const db = await openOfflineDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    tx.objectStore(storeName).delete(key);
    tx.oncomplete = () => resolve(true);
    tx.onerror = () => reject(tx.error);
  });
}

async function updateMeta(key, value) {
  await dbPut(META_STORE, { key, value });
}

async function getQueueStatus() {
  const requests = await dbReadAll(REQUEST_STORE);
  const lastSync = await dbGet(META_STORE, LAST_SYNC_KEY);
  const lastError = await dbGet(META_STORE, LAST_ERROR_KEY);
  return {
    pendingCount: requests.length,
    lastSyncAt: lastSync ? lastSync.value : null,
    lastError: lastError ? lastError.value : '',
    online: self.navigator ? self.navigator.onLine : true,
  };
}

async function broadcastQueueStatus(requestId = null) {
  const status = await getQueueStatus();
  const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const client of clients) {
    client.postMessage({ type: 'OFFLINE_QUEUE_STATUS', requestId, status });
  }
  return status;
}

async function cacheJsonSnapshot(request, response) {
  if (!response || !response.ok) return;
  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  if (!contentType.includes('application/json')) return;
  try {
    await dbPut(SNAPSHOT_STORE, {
      key: request.url,
      body: await response.clone().text(),
      headers: Array.from(response.headers.entries()),
      status: response.status,
      updatedAt: nowIso(),
    });
  } catch (error) {
    // Snapshot caching is best-effort.
  }
}

async function readJsonSnapshot(requestUrl) {
  const snapshot = await dbGet(SNAPSHOT_STORE, requestUrl);
  if (!snapshot) return null;
  return new Response(snapshot.body, {
    status: snapshot.status || 200,
    headers: snapshot.headers || [['Content-Type', 'application/json; charset=utf-8']],
  });
}

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(STATIC_ASSETS);
    await updateMeta(LAST_SYNC_KEY, nowIso());
    await updateMeta(LAST_ERROR_KEY, '');
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)));
    await self.clients.claim();
    await broadcastQueueStatus();
  })());
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
    event.respondWith(networkFirstApi(request));
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
  const data = event.data || {};
  if (data.type === 'FLUSH_QUEUE') {
    event.waitUntil(flushQueue().then(() => broadcastQueueStatus(data.requestId || null)));
  }
  if (data.type === 'CLEAR_OFFLINE_QUEUE') {
    event.waitUntil(clearQueue().then(() => broadcastQueueStatus(data.requestId || null)));
  }
  if (data.type === 'GET_OFFLINE_STATUS') {
    event.waitUntil((async () => {
      const status = await broadcastQueueStatus(data.requestId || null);
      if (event.source) {
        event.source.postMessage({ type: 'OFFLINE_QUEUE_STATUS', requestId: data.requestId || null, status });
      }
    })());
  }
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    return new Response(JSON.stringify({ offline: true, error: 'Sin conexion' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  }
}

async function networkFirst(request, offlineFallback = false) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response.ok) {
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    if (offlineFallback) {
      return (await cache.match('/offline.html')) || new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
    }
    return new Response(JSON.stringify({ offline: true, error: 'Sin conexion' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  }
}

async function networkFirstApi(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      await cacheJsonSnapshot(request, response);
    }
    return response;
  } catch (error) {
    const snapshot = await readJsonSnapshot(request.url);
    if (snapshot) return snapshot;
    return new Response(JSON.stringify({ offline: true, error: 'Sin conexion' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  }
}

async function queueWhenOffline(request) {
  try {
    const response = await fetch(request.clone());
    if (response.ok) {
      await updateMeta(LAST_SYNC_KEY, nowIso());
      await updateMeta(LAST_ERROR_KEY, '');
    }
    return response;
  } catch (error) {
    const body = await request.clone().text();
    await dbAdd(REQUEST_STORE, {
      uuid: generateUuid(),
      operationType: inferOperationType(new URL(request.url).pathname, request.method),
      url: request.url,
      method: request.method,
      headers: Array.from(request.headers.entries()),
      body,
      createdAt: nowIso(),
      attempts: 0,
      status: 'pending',
    });
    await updateMeta(LAST_ERROR_KEY, 'Operación en cola por falta de conexión.');
    await broadcastQueueStatus();
    if ('sync' in self.registration) {
      await self.registration.sync.register('stockarmobile-sync');
    }
    return new Response(JSON.stringify({ queued: true }), {
      status: 202,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  }
}

async function flushQueue() {
  const queued = await dbReadAll(REQUEST_STORE);
  for (const item of queued) {
    try {
      const response = await fetch(item.url, {
        method: item.method,
        headers: item.headers,
        body: item.body || undefined,
        credentials: 'same-origin',
      });
      if (response.ok || [401, 403, 409, 412].includes(response.status)) {
        await dbDelete(REQUEST_STORE, item.id);
        await updateMeta(LAST_SYNC_KEY, nowIso());
        await updateMeta(LAST_ERROR_KEY, '');
      } else {
        await dbPut(REQUEST_STORE, { ...item, attempts: (item.attempts || 0) + 1, status: 'pending' });
        await updateMeta(LAST_ERROR_KEY, `Error sincronizando ${item.operationType || item.url}: HTTP ${response.status}`);
      }
    } catch (error) {
      await dbPut(REQUEST_STORE, { ...item, attempts: (item.attempts || 0) + 1, status: 'error' });
      await updateMeta(LAST_ERROR_KEY, `Error sincronizando ${item.operationType || item.url}: ${error.message || 'sin detalle'}`);
    }
  }
  await broadcastQueueStatus();
}

async function clearQueue() {
  const db = await openOfflineDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(REQUEST_STORE, 'readwrite');
    tx.objectStore(REQUEST_STORE).clear();
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  }).then(async () => {
    await updateMeta(LAST_ERROR_KEY, '');
    await broadcastQueueStatus();
  });
}
