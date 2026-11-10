/**
 * active_views.js - Tab id -> live view instance, for components that act on a tab's view without
 * importing it (the palette's ":name" mode reads the active code tab's outline, §5.15).
 */
const VIEWS = new Map();

export function registerView(tabId, view) {
  if (tabId) VIEWS.set(tabId, view);
}

export function unregisterView(tabId, view) {
  if (tabId && VIEWS.get(tabId) === view) VIEWS.delete(tabId);
}

export function viewOf(tabId) {
  return (tabId && VIEWS.get(tabId)) || null;
}
