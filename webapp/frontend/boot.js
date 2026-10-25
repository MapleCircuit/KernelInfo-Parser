/**
 * boot.js - Loads the application modules of the frontend build the shell belongs to.
 *
 * index.html references every asset under /b/<build>/ (immutable, cached forever). When the shell is
 * older than the server's build (cached by the service worker or the HTTP cache) and a module is not
 * cached, the server answers 409: the shell and asset caches are dropped and the page reloads once
 * into the current build. Kept as a classic external script (the CSP forbids inline scripts).
 */
(function () {
  "use strict";
  var script = document.currentScript;
  var entry = script && script.getAttribute("data-entry");
  var meta = document.querySelector('meta[name="kip-build"]');
  var build = meta ? meta.getAttribute("content") : "";
  var GUARD = "kip-stale-build-reload";
  window.KIP_BUILD = build;

  function clearShellCaches() {
    if (!window.caches) return Promise.resolve();
    return caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k.indexOf("kip-shell") === 0 || k.indexOf("kip-assets") === 0; })
        .map(function (k) { return caches.delete(k); }));
    });
  }

  function showFailure(err) {
    console.error("KernelInfo failed to load:", err);
    var box = document.createElement("div");
    box.style.cssText = "position:fixed;inset:auto 16px 16px 16px;padding:12px 16px;border-radius:6px;background:#3d1d1d;color:#f0f0f0;font:13px sans-serif;z-index:99999";
    box.textContent = "KernelInfo could not load its interface. Reload the page; if this persists, clear the site data.";
    document.body.appendChild(box);
  }

  function recover(err) {
    var tried = null;
    try { tried = sessionStorage.getItem(GUARD); } catch (e) { /* storage unavailable */ }
    if (tried === build) {
      showFailure(err);
      return;
    }
    try { sessionStorage.setItem(GUARD, build); } catch (e) { /* storage unavailable */ }
    var reload = function () { location.reload(); };
    clearShellCaches().then(reload, reload);
  }

  window.KIP_RELOAD_TO_LATEST = function () {
    var reload = function () { location.reload(); };
    return clearShellCaches().then(reload, reload);
  };

  import(entry).then(function () {
    try { sessionStorage.removeItem(GUARD); } catch (e) { /* storage unavailable */ }
  }, recover);
})();
