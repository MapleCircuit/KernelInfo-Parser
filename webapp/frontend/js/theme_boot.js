/**
 * theme_boot.js - Applies the saved theme (G08) before the stylesheets paint, so a light theme never flashes
 * dark. A classic, render-blocking script in <head> (the CSP forbids inline scripts); utils/theme.js does the rest.
 */
(function () {
  "use strict";
  var t = "dark";
  try {
    var saved = localStorage.getItem("kip-theme");
    if (["auto", "dark", "light", "hc-dark", "hc-light"].indexOf(saved) >= 0) t = saved;
  } catch (e) {
    /* storage unavailable: dark */
  }
  document.documentElement.setAttribute("data-theme", t);
})();
