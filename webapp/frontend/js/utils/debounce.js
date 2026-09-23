/**
 * debounce.js - High-performance function debouncer.
 */
export function debounce(fn, delayMs = 250) {
  let timer = null;
  return function (...args) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      fn.apply(this, args);
      timer = null;
    }, delayMs);
  };
}
