/**
 * sw.js - Service Worker for KernelInfo-Parser Developer Web Application.
 * Provides offline caching for application shell, static assets, and API requests.
 */
const CACHE_NAME = "kernel-ide-v2";
const STATIC_ASSETS = [
  "/",
  "/app",
  "/manifest.json",
  "/static/assets/icon.svg",
  "/static/vendor/two.min.js",
  "/static/css/variables.css",
  "/static/css/layout.css",
  "/static/css/tabs.css",
  "/static/css/codeview.css",
  "/static/css/nodemap.css",
  "/static/css/kconfig.css",
  "/static/css/maintainers.css",
  "/static/css/commits.css",
  "/static/css/tools.css",
  "/static/css/modals.css",
  "/static/js/api.js",
  "/static/js/db.js",
  "/static/js/state.js",
  "/static/js/url_sync.js",
  "/static/js/app.js",
  "/static/js/components/tabs.js",
  "/static/js/components/context_menu.js",
  "/static/js/utils/clipboard.js",
  "/static/js/components/toast.js",
  "/static/js/views/code_view/virtual_editor.js",
  "/static/js/views/code_view/fidelity_clipboard.js",
  "/static/js/views/code_view/ast_overlay.js",
  "/static/js/views/code_view/blame_view.js",
  "/static/js/views/kconfig/kconfig_engine.js",
  "/static/js/views/kconfig/kconfig_parser.js",
  "/static/js/views/kconfig/menuconfig_tui.js",
  "/static/js/views/kconfig/directive_evaluator.js",
  "/static/js/views/nodemap/nodemap_controller.js",
  "/static/js/views/nodemap/node_model.js",
  "/static/js/views/nodemap/node_renderer.js",
  "/static/js/views/nodemap/edge_router.js",
  "/static/js/views/maintainers/maintainers_view.js",
  "/static/js/views/commits/commits_view.js",
  "/static/js/views/pahole/pahole_view.js",
  "/static/js/views/callgraph/callgraph_view.js",
  "/static/js/views/diff/diff_view.js"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch((err) => {
        console.warn("ServiceWorker pre-cache incomplete:", err);
      });
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // For API calls: Network-First with Cache Fallback
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => {
          return caches.match(event.request).then((cached) => {
            if (cached) return cached;
            return new Response(JSON.stringify({ error: "Offline - data not cached yet" }), {
              headers: { "Content-Type": "application/json" },
              status: 503
            });
          });
        })
    );
    return;
  }

  // For static assets: Cache-First, fallback to Network
  event.respondWith(
    caches.match(event.request).then((cached) => {
      return cached || fetch(event.request).then((response) => {
        if (response && response.status === 200) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      });
    })
  );
});
