/**
 * tab_drag.js - The dragged-tab payload of the tab strip (`components/tabs.js`) as other drop targets
 * read it (`webapp/API_FRONTEND.md` §5.24.4, NodeMap canvas §5.3.5). Dependency-free.
 *
 * A tab drag carries `state.tabTransferPayload()` (`{tabId, sourcePaneId, sourceWinId, sourceSessionId,
 * sourceSessionName, tabData}`) as JSON under `TAB_DRAG_MIME`, and as `text/plain` behind
 * `TAB_DRAG_TEXT_PREFIX` (some browsers give a custom type only to the window the drag started in);
 * other windows also get it as the `tab:drag:start` announcement (`sessionManager.incomingTabDrag`).
 */
export const TAB_DRAG_MIME = "application/kip-tab";
export const TAB_DRAG_TEXT_PREFIX = "kip-tab:";

/**
 * Whether a drag (`dataTransfer`, readable only by type during `dragover`) may carry a tab: the tab type
 * is present, or another window announced a tab drag (`incoming`) and no files are dragged in.
 */
export function mayCarryTab(dataTransfer, incoming = null) {
  const types = Array.from(dataTransfer?.types || []);
  if (types.includes(TAB_DRAG_MIME)) return true;
  if (types.includes("Files")) return false;
  return Boolean(incoming?.tabData);
}

/** The tab payload of a drop: the data transfer's JSON or text form, else the announcement; null. */
export function tabDragPayload(dataTransfer, incoming = null) {
  for (const [type, prefix] of [[TAB_DRAG_MIME, ""], ["text/plain", TAB_DRAG_TEXT_PREFIX]]) {
    try {
      const raw = dataTransfer?.getData?.(type) || "";
      if (raw.startsWith(prefix) && raw.length > prefix.length) {
        const parsed = JSON.parse(raw.slice(prefix.length));
        if (parsed && parsed.tabData) return parsed;
      }
    } catch {
      // not a tab payload (another application's drag), or withheld by the browser
    }
  }
  const types = Array.from(dataTransfer?.types || []);
  if (types.includes("Files")) return null;
  return incoming?.tabData ? incoming : null;
}
