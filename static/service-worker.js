const CACHE_NAME = 'stockarmobile-pwa-v15';
const STATIC_ASSETS = [
  '/offline.html',
  '/manifest.json',
  '/static/assets/css/styles.css',
  '/static/assets/css/landing.css?v=20260923',
  '/static/assets/css/landing-v3.css?v=20260923',
  '/static/assets/js/cart-manager.js?v=20261028-pos-wholesale-pricing',
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
const SYNCABLE_POST_PREFIXES = [
  '/ventas/checkout',
  '/ventas/edit/',
  '/ventas/delete/',
  '/ventas/view/',
  '/ventas/api/checkout',
  '/ventas/api/mp-qr/create',
  '/ventas/api/mp-qr/finalize',
  '/productos/add',
  '/productos/edit/',
  '/productos/delete/',
  '/productos/import',
  '/clientes/add',
  '/clientes/post',
  '/clientes/edit/',
  '/clientes/delete/',
  '/clientes/api/quick-create',
  '/compras/',
  '/compras/proveedores',
  '/gastos/',
  '/caja/',
];
const ONLINE_NAVIGATION_POST_PREFIXES = [
  '/admin/subscription/mercadopago/create',
  '/admin/subscription/ai-agent/checkout',
];
const DB_NAME = 'stockarmobile-offline';
const DB_VERSION = 4;
const REQUEST_STORE = 'requests';
const SNAPSHOT_STORE = 'snapshots';
const META_STORE = 'meta';
const LAST_SYNC_KEY = 'lastSyncAt';
const LAST_ERROR_KEY = 'lastError';
const LAST_SYNC_SUMMARY_KEY = 'lastSyncSummary';
const ACTIVE_IDENTITY_KEY = 'activeIdentity';
const PERMANENT_HTTP_STATUSES = new Set([400, 401, 403, 404, 405, 409, 410, 412, 413, 415, 422]);
let syncState = null;

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
    request.onupgradeneeded = event => {
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
      // Snapshots from old schema versions were keyed only by URL, not by
      // tenant/user. They are cache data, so drop them during migration rather
      // than allow an old private response to remain in IndexedDB.
      if (event.oldVersion > 0 && event.oldVersion < DB_VERSION) {
        event.target.transaction.objectStore(SNAPSHOT_STORE).clear();
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

async function readMetaValue(key, fallback = null) {
  const meta = await dbGet(META_STORE, key);
  return meta ? meta.value : fallback;
}

function normalizeOfflineIdentity(value) {
  if (!value || typeof value !== 'object') return null;
  const userId = String(value.userId || '').trim();
  const companyId = String(value.companyId || '').trim();
  if (!userId || !companyId) return null;
  return { userId, companyId };
}

function sameOfflineIdentity(left, right) {
  const a = normalizeOfflineIdentity(left);
  const b = normalizeOfflineIdentity(right);
  return Boolean(a && b && a.userId === b.userId && a.companyId === b.companyId);
}

async function setActiveOfflineIdentity(value) {
  const identity = normalizeOfflineIdentity(value);
  await updateMeta(ACTIVE_IDENTITY_KEY, identity);
  return identity;
}

function requestOfflineIdentity(request) {
  return normalizeOfflineIdentity({
    userId: request.headers.get('X-Offline-User-Id'),
    companyId: request.headers.get('X-Offline-Company-Id'),
  });
}

function offlineJsonError(message, status = 409) {
  return new Response(JSON.stringify({ queued: false, error: message }), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}

function isAuthenticationRedirect(response) {
  if (!response || !response.redirected || !response.url) return false;
  try {
    const redirected = new URL(response.url);
    if (redirected.origin !== self.location.origin) return false;
    return redirected.pathname === '/auth/login' || redirected.pathname === '/login';
  } catch (error) {
    return false;
  }
}

async function setSyncSummary(summary) {
  await updateMeta(LAST_SYNC_SUMMARY_KEY, summary);
}

async function getQueueStatus() {
  const requests = await dbReadAll(REQUEST_STORE);
  const lastSync = await dbGet(META_STORE, LAST_SYNC_KEY);
  const lastError = await dbGet(META_STORE, LAST_ERROR_KEY);
  const lastSyncSummary = await dbGet(META_STORE, LAST_SYNC_SUMMARY_KEY);
  const pendingCount = requests.length;
  return {
    pendingCount,
    syncedCount: syncState ? syncState.syncedCount : 0,
    errorCount: syncState ? syncState.errorCount : 0,
    totalCount: syncState ? syncState.totalCount : pendingCount,
    progress: syncState ? syncState.progress : (pendingCount === 0 ? 100 : 0),
    syncing: Boolean(syncState),
    currentOperation: syncState ? syncState.currentOperation : '',
    lastSyncAt: lastSync ? lastSync.value : null,
    lastError: lastError ? lastError.value : '',
    lastSyncSummary: lastSyncSummary ? lastSyncSummary.value : null,
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

async function broadcastSyncProgress() {
  await broadcastQueueStatus(syncState ? syncState.requestId : null);
}

async function findQueuedRequestByUuid(uuid) {
  if (!uuid) return null;
  const requests = await dbReadAll(REQUEST_STORE);
  return requests.find((item) => item.uuid === uuid) || null;
}

function extractRequestUuid(request) {
  return request.headers.get('X-Offline-Request-Id') || request.headers.get('X-Request-Id') || null;
}

function identitySnapshotKey(requestUrl, identity) {
  const normalized = normalizeOfflineIdentity(identity);
  return normalized ? `${normalized.companyId}:${normalized.userId}:${requestUrl}` : null;
}

async function cacheJsonSnapshot(request, response, requestIdentity) {
  if (!response || !response.ok) return;
  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  if (!contentType.includes('application/json')) return;
  const identity = normalizeOfflineIdentity(requestIdentity);
  const activeIdentity = normalizeOfflineIdentity(await readMetaValue(ACTIVE_IDENTITY_KEY, null));
  const key = identitySnapshotKey(request.url, identity);
  if (!key || !sameOfflineIdentity(identity, activeIdentity)) return;
  try {
    await dbPut(SNAPSHOT_STORE, {
      key,
      identity,
      body: await response.clone().text(),
      headers: Array.from(response.headers.entries()),
      status: response.status,
      updatedAt: nowIso(),
    });
  } catch (error) {
    // Snapshot caching is best-effort.
  }
}

async function readJsonSnapshot(requestUrl, requestIdentity) {
  const identity = normalizeOfflineIdentity(requestIdentity);
  const activeIdentity = normalizeOfflineIdentity(await readMetaValue(ACTIVE_IDENTITY_KEY, null));
  const key = identitySnapshotKey(requestUrl, identity);
  if (!key || !sameOfflineIdentity(identity, activeIdentity)) return null;
  const snapshot = await dbGet(SNAPSHOT_STORE, key);
  if (!snapshot || !sameOfflineIdentity(snapshot.identity, identity)) return null;
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
    if (ONLINE_NAVIGATION_POST_PREFIXES.some(prefix => url.pathname.startsWith(prefix))) {
      return;
    }
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
  if (data.type === 'SET_ACTIVE_IDENTITY') {
    event.waitUntil(setActiveOfflineIdentity(data.identity).then(() => broadcastQueueStatus(data.requestId || null)));
  }
  if (data.type === 'FLUSH_QUEUE') {
    event.waitUntil((async () => {
      const identity = await setActiveOfflineIdentity(data.identity);
      await flushQueue(identity);
      await broadcastQueueStatus(data.requestId || null);
    })());
  }
  if (data.type === 'CLEAR_OFFLINE_QUEUE') {
    event.waitUntil(clearQueue().then(() => broadcastQueueStatus(data.requestId || null)));
  }
  if (data.type === 'CLEAR_PRIVATE_CACHE') {
    event.waitUntil((async () => {
      // During a session switch, queue replay stays disabled until the newly
      // authenticated page explicitly establishes its identity.
      await setActiveOfflineIdentity(null);
      let success = true;
      try {
        await clearPrivateCache();
      } catch (error) {
        success = false;
        console.warn('No se pudo limpiar toda la cache privada al cambiar de usuario.', error);
      }
      try {
        await broadcastQueueStatus(data.requestId || null);
      } catch (error) {
        // La limpieza de cache no depende de que pueda leerse el estado de la cola.
      }
      if (event.source) {
        event.source.postMessage({
          type: 'PRIVATE_CACHE_CLEARED',
          requestId: data.requestId || null,
          success,
        });
      }
    })());
  }
  if (data.type === 'GET_OFFLINE_STATUS') {
    event.waitUntil((async () => {
      await setActiveOfflineIdentity(data.identity);
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
    const contentType = (response.headers.get('content-type') || '').toLowerCase();
    if (response.ok && !contentType.includes('text/html')) {
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
  // Never persist navigated HTML: authenticated pages are private even when
  // addressed by the same URL on another user/tenant session.
  const cache = await caches.open(CACHE_NAME);
  try {
    return await fetch(request);
  } catch (error) {
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
  const requestIdentity = normalizeOfflineIdentity(await readMetaValue(ACTIVE_IDENTITY_KEY, null));
  try {
    const response = await fetch(request);
    if (response.ok) {
      await cacheJsonSnapshot(request, response, requestIdentity);
    }
    return response;
  } catch (error) {
    const snapshot = await readJsonSnapshot(request.url, requestIdentity);
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
    const identity = requestOfflineIdentity(request);
    if (!identity) {
      return offlineJsonError(
        'No se guardó la operación offline porque falta la identidad de usuario y empresa. Iniciá sesión y volvé a cargar la operación.',
      );
    }
    const body = await request.clone().text();
    const requestUuid = extractRequestUuid(request) || generateUuid();
    const existing = await findQueuedRequestByUuid(requestUuid);
    if (existing && !sameOfflineIdentity(existing.identity, identity)) {
      return offlineJsonError(
        'La operación coincide con una cola creada bajo otra identidad. No se reemplazó ni se envió; revisá la operación pendiente.',
      );
    }
    const queued = {
      uuid: requestUuid,
      identity,
      operationType: inferOperationType(new URL(request.url).pathname, request.method),
      url: request.url,
      method: request.method,
      headers: Array.from(request.headers.entries()),
      body,
      createdAt: nowIso(),
      attempts: 0,
      status: 'pending',
    };
    if (existing) {
      queued.id = existing.id;
      await dbPut(REQUEST_STORE, queued);
    } else {
      await dbAdd(REQUEST_STORE, queued);
    }
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

async function flushQueue(identityInput) {
  const currentIdentity = identityInput === undefined
    ? normalizeOfflineIdentity(await readMetaValue(ACTIVE_IDENTITY_KEY, null))
    : normalizeOfflineIdentity(identityInput);
  if (!currentIdentity) {
    await updateMeta(LAST_ERROR_KEY, 'No se sincronizó la cola: falta una sesión autenticada con empresa activa.');
    await broadcastQueueStatus();
    return;
  }
  const queued = await dbReadAll(REQUEST_STORE);
  syncState = {
    requestId: generateUuid(),
    startedAt: nowIso(),
    totalCount: queued.length,
    syncedCount: 0,
    errorCount: 0,
    currentOperation: '',
    progress: queued.length === 0 ? 100 : 0,
  };
  await broadcastSyncProgress();
  for (const item of queued) {
    syncState.currentOperation = item.operationType || item.url;
    if (item.status === 'needs_attention') {
      syncState.errorCount += 1;
      syncState.progress = syncState.totalCount > 0 ? Math.round(((syncState.syncedCount + syncState.errorCount) / syncState.totalCount) * 100) : 100;
      await broadcastSyncProgress();
      continue;
    }
    const latestIdentity = normalizeOfflineIdentity(await readMetaValue(ACTIVE_IDENTITY_KEY, null));
    if (!sameOfflineIdentity(latestIdentity, currentIdentity)) {
      // Stop a running batch as soon as a logout or user-switch is observed.
      break;
    }
    const queuedIdentity = normalizeOfflineIdentity(item.identity);
    if (!queuedIdentity || !sameOfflineIdentity(queuedIdentity, currentIdentity)) {
      await dbPut(REQUEST_STORE, {
        ...item,
        status: 'needs_attention',
        attentionReason: queuedIdentity ? 'identity_mismatch' : 'identity_unverified',
        lastAttemptAt: nowIso(),
      });
      await updateMeta(
        LAST_ERROR_KEY,
        queuedIdentity
          ? 'Hay una operación de otra empresa/usuario en la cola. No se envió; revisá y volvé a cargarla con la sesión correcta.'
          : 'Hay una operación offline antigua sin identidad verificable. No se envió; revisá y volvé a cargarla.',
      );
      syncState.errorCount += 1;
      syncState.progress = syncState.totalCount > 0 ? Math.round(((syncState.syncedCount + syncState.errorCount) / syncState.totalCount) * 100) : 100;
      await broadcastSyncProgress();
      continue;
    }
    try {
      const response = await fetch(item.url, {
        method: item.method,
        headers: item.headers,
        body: item.body || undefined,
        credentials: 'same-origin',
      });
      if (isAuthenticationRedirect(response)) {
        await dbPut(REQUEST_STORE, {
          ...item,
          attempts: (item.attempts || 0) + 1,
          status: 'needs_attention',
          attentionReason: 'session_required',
          lastAttemptAt: nowIso(),
        });
        await updateMeta(LAST_ERROR_KEY, 'La sesión venció antes de sincronizar una operación offline. Iniciá sesión y revisá la operación; no se volverá a enviar automáticamente.');
        syncState.errorCount += 1;
      } else if (response.ok) {
        await dbDelete(REQUEST_STORE, item.id);
        await updateMeta(LAST_SYNC_KEY, nowIso());
        await updateMeta(LAST_ERROR_KEY, '');
        syncState.syncedCount += 1;
      } else if (PERMANENT_HTTP_STATUSES.has(response.status)) {
        await dbPut(REQUEST_STORE, {
          ...item,
          attempts: (item.attempts || 0) + 1,
          status: 'needs_attention',
          attentionReason: 'permanent_http_error',
          lastHttpStatus: response.status,
          lastAttemptAt: nowIso(),
        });
        await updateMeta(LAST_ERROR_KEY, `Operación ${item.operationType || item.url} requiere atención: HTTP ${response.status}. No se volverá a intentar automáticamente.`);
        syncState.errorCount += 1;
      } else {
        await dbPut(REQUEST_STORE, {
          ...item,
          attempts: (item.attempts || 0) + 1,
          status: 'pending',
          lastHttpStatus: response.status,
          lastAttemptAt: nowIso(),
        });
        await updateMeta(LAST_ERROR_KEY, `Error temporal sincronizando ${item.operationType || item.url}: HTTP ${response.status}`);
        syncState.errorCount += 1;
      }
    } catch (error) {
      await dbPut(REQUEST_STORE, { ...item, attempts: (item.attempts || 0) + 1, status: 'error' });
      await updateMeta(LAST_ERROR_KEY, `Error sincronizando ${item.operationType || item.url}: ${error.message || 'sin detalle'}`);
      syncState.errorCount += 1;
    }
    syncState.progress = syncState.totalCount > 0 ? Math.round(((syncState.syncedCount + syncState.errorCount) / syncState.totalCount) * 100) : 100;
    await broadcastSyncProgress();
  }
  await setSyncSummary({
    completedAt: nowIso(),
    totalCount: syncState.totalCount,
    syncedCount: syncState.syncedCount,
    errorCount: syncState.errorCount,
  });
  syncState = null;
  await broadcastQueueStatus();
}

async function clearPrivateCache() {
  const cache = await caches.open(CACHE_NAME);
  const requests = await cache.keys();
  await Promise.all(requests.map(async request => {
    const pathname = new URL(request.url).pathname;
    if (pathname === '/offline.html') return;
    const response = await cache.match(request);
    const contentType = (response && response.headers.get('content-type') || '').toLowerCase();
    if (request.mode === 'navigate' || contentType.includes('text/html')) {
      await cache.delete(request);
    }
  }));

  const db = await openOfflineDB();
  await new Promise((resolve, reject) => {
    const tx = db.transaction([SNAPSHOT_STORE], 'readwrite');
    tx.objectStore(SNAPSHOT_STORE).clear();
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
    tx.onerror = () => {
      const error = tx.error;
      db.close();
      reject(error);
    };
    tx.onabort = () => {
      const error = tx.error || new Error('No se pudo limpiar la cache offline.');
      db.close();
      reject(error);
    };
  });
}

async function clearQueue() {
  await setActiveOfflineIdentity(null);
  const db = await openOfflineDB();
  await new Promise((resolve, reject) => {
    const tx = db.transaction([REQUEST_STORE, SNAPSHOT_STORE, META_STORE], 'readwrite');
    tx.objectStore(REQUEST_STORE).clear();
    tx.objectStore(SNAPSHOT_STORE).clear();
    tx.objectStore(META_STORE).clear();
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
  try {
    await caches.delete(CACHE_NAME);
  } catch (error) {
    // Cache deletion is best-effort during logout.
  }
  await broadcastQueueStatus();
}
