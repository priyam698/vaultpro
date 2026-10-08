const CACHE_NAME = 'zephyr-cache-v1';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Let network requests pass through normally for dynamic encrypted data
  event.respondWith(fetch(event.request));
});