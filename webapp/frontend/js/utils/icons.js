/**
 * icons.js - Shared inline SVG icons.
 *
 * COLLAPSE_ICON is the default icon of every control that folds a panel away (the sidebar's
 * `#sidebar-collapse`, the commits list...): a chevron against a bar, pointing at the side the panel
 * folds to. Use it inside a `.collapse-btn` (css/layout.css); `.collapse-btn.expand` mirrors it, for
 * the control that unfolds the panel again.
 */
export const COLLAPSE_ICON =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10.47 3.72a.75.75 0 0 1 0 1.06L7.31 8l3.16 3.22a.75.75 0 1 1-1.07 1.06l-3.69-3.75a.75.75 0 0 1 0-1.06l3.69-3.75a.75.75 0 0 1 1.07 0z"/><path d="M13.25 2.5a.75.75 0 0 1 .75.75v9.5a.75.75 0 0 1-1.5 0v-9.5a.75.75 0 0 1 .75-.75z"/></svg>';
