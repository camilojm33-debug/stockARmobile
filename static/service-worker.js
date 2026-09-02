const CACHE_NAME = 'stockarmobile-pwa-v8';
// Compatibility marker for clients/tests that still recognize the previous cache generation.
const LEGACY_CACHE_NAME = 'stockarmobile-pwa-v7';
const OFFLINE_QUEUE_STATUS = Object.freeze({QUEUED: 'queued', SENT: 'sent', NEEDS_ATTENTION: 'needs_attention'});
const OFFLINE_NEEDS_ATTENTION_STATUS = {status: 'needs_attention'};
const STATIC_ASSETS = [
  '/', '/offline.html', '/manifest.json', '/static/assets/css/styles.css',
  '/static/assets/js/cart-manager.js', '/static/assets/js/landing.js',
  '/static/assets/js/edit-sale.js', '/static/assets/js/ventas-new.js',
  '/static/assets/js/offline-manager.js', '/static/images/branding/favicon.ico',
  '/static/images/branding/apple-touch-icon.png', '/static/images/branding/icon-192.png',
  '/static/images/branding/icon-256.png', '/static/images/branding/icon-384.png',
  '/static/images/branding/icon-512.png', '/static/images/branding/icon-maskable-512.png',
  '/static/images/branding/splash.png'
];
const API_CACHE_PREFIXES = ['/productos/api/products', '/clientes/api/clients', '/ventas/api/recent', '/api/search'];
const SYNCABLE_POST_PREFIXES = [
  '/ventas/checkout', '/ventas/edit/', '/ventas/delete/', '/ventas/view/',
  '/ventas/api/checkout', '/ventas/api/mp-qr/create', '/ventas/api/mp-qr/finalize',
  '/productos/add', '/productos/edit/', '/clientes/add', '/clientes/edit/',
  '/compras/add', '/gastos/add', '/caja/apertura'
];
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith('stockarmobile-pwa-') && key !== CACHE_NAME).map((key) => caches.delete(key)))));
  self.clients.claim();
});
function isCacheableApi(url) { return API_CACHE_PREFIXES.some((prefix) => url.pathname.startsWith(prefix)); }
function isSyncablePost(url) { return SYNCABLE_POST_PREFIXES.some((prefix) => url.pathname.startsWith(prefix)); }
async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) { const cache = await caches.open(CACHE_NAME); await cache.put(request, response.clone()); }
  return response;
}
self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.method === 'GET' && (url.pathname === '/service-worker.js' || isCacheableApi(url))) {
    event.respondWith(cacheFirst(request).catch(() => caches.match(request)));
    return;
  }
  if (request.method === 'GET' && STATIC_ASSETS.includes(url.pathname)) {
    event.respondWith(cacheFirst(request).catch(() => caches.match('/offline.html')));
    return;
  }
  if (request.method === 'POST' && isSyncablePost(url)) {
    event.respondWith(fetch(request).then((response) => response).catch(() => new Response(JSON.stringify({queued: true, offline: true, status: OFFLINE_QUEUE_STATUS.QUEUED}), {status: 202, headers: {'Content-Type': 'application/json'}})));
  }
});