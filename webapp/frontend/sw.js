/**
 * sw.js - Service Worker for KernelInfo-Parser Developer Web Application.
 *
 * Caches (all named "kip-*"; older caches are deleted on activation):
 * - kip-shell: the app shell ("/") and /boot.js, cache-first. The page asks the server for a new
 *   frontend build at most hourly (js/sync.js); when there is one it drops this cache and reloads.
 * - kip-assets: /b/<build>/... files, cache-first (immutable per build). Entries of other builds are
 *   pruned when the page tells the worker its build ({type: "prune", build}).
 * - kip-api: API responses, network-first with cache fallback for offline use. Bounded: past
 *   API_CACHE_LIMIT entries the oldest are dropped (the page keeps its own IndexedDB cache too).
 */
const SHELL_CACHE = "kip-shell";
const ASSET_CACHE = "kip-assets";
const API_CACHE = "kip-api";
const SHELL_PATHS = new Set(["/", "/app", "/webapp", "/boot.js"]);
const API_CACHE_LIMIT = 300;
// Entries are trimmed once every this many writes (listing the cache's keys is not free)
const API_TRIM_EVERY = 25;
let apiWrites = 0;

/** Drop the oldest API responses beyond API_CACHE_LIMIT (Cache Storage lists keys in insertion order). */
function trimApiCache(cache) {
  return cache.keys().then((requests) => Promise.all(
    requests.slice(0, Math.max(0, requests.length - API_CACHE_LIMIT)).map((req) => cache.delete(req))
  ));
}

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => !key.startsWith("kip-")).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "prune" && data.build) {
    const keep = `/b/${data.build}/`;
    event.waitUntil(
      caches.open(ASSET_CACHE).then((cache) => cache.keys().then((requests) => Promise.all(
        requests.filter((req) => !new URL(req.url).pathname.startsWith(keep)).map((req) => cache.delete(req))
      )))
    );
  }
});

function cacheFirst(request, cacheName, cacheKey = request) {
  return caches.open(cacheName).then((cache) => cache.match(cacheKey).then((cached) => {
    if (cached) return cached;
    return fetch(request).then((response) => {
      if (response && response.status === 200) cache.put(cacheKey, response.clone());
      return response;
    });
  }));
}

/**
 * Copy of a cached response carrying `X-KIP-Offline: 1`, so the page can tell a reply that came from
 * this cache after a failed request from one the server actually sent (js/api.js, the header badge).
 */
async function offlineCopy(cached) {
  const headers = new Headers(cached.headers);
  headers.set("X-KIP-Offline", "1");
  return new Response(await cached.blob(), { status: cached.status, statusText: cached.statusText, headers });
}

function apiCacheKey(request) {
  const repo = request.headers.get("X-KI-Repo") || "default";
  const url = new URL(request.url);
  url.searchParams.set("__kip_repo", repo);
  return url.toString();
}

function networkFirst(request, cacheName) {
  const cacheKey = apiCacheKey(request);
  return fetch(request)
    .then((response) => {
      if (response && response.status === 200) {
        const copy = response.clone();
        caches.open(cacheName).then((cache) => cache.put(cacheKey, copy).then(() => {
          if (++apiWrites % API_TRIM_EVERY === 0) return trimApiCache(cache);
          return null;
        }));
      }
      return response;
    })
    .catch(() => caches.open(cacheName).then((cache) => cache.match(cacheKey)).then((cached) => cached ? offlineCopy(cached) : new Response(
      JSON.stringify({ error: "Offline - data not cached yet" }),
      { headers: { "Content-Type": "application/json", "X-KIP-Offline": "1" }, status: 503 }
    )));
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate" && SHELL_PATHS.has(url.pathname)) {
    // One shell entry whatever the shell path or query (?repo=, deep links)
    event.respondWith(cacheFirst(request, SHELL_CACHE, "/"));
  } else if (url.pathname === "/boot.js") {
    event.respondWith(cacheFirst(request, SHELL_CACHE));
  } else if (url.pathname.startsWith("/b/")) {
    event.respondWith(cacheFirst(request, ASSET_CACHE));
  } else if (url.pathname.startsWith("/api/")) {
    if (url.pathname === "/api/sync") return; // always asks the server
    event.respondWith(networkFirst(request, API_CACHE));
  }
  // Anything else (legacy /static/ URLs, manifest, sw.js) goes to the network untouched
});
