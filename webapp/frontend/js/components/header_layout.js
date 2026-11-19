/**
 * header_layout.js - Measurement-driven header collapse & reduced version selector (webapp/API_FRONTEND.md §5.13).
 *
 * The header never relies on fixed viewport breakpoints: on every relevant change (header resize, window
 * resize, UI Scale, selection/label updates) the layout is measured from the full mode downwards and the
 * first collapse tier whose content fits `#app-header` is applied as cumulative `hdr-collapse-<n>` classes
 * (plus `data-collapse-tier`). Measuring runs under `hdr-measuring` (transitions off), so every width read is
 * the settled width of the tier being tried, never a frame of an animating padding/font-size; expanding to a
 * lower tier additionally needs `expandMargin` of slack (hysteresis), so the layout cannot oscillate.
 *
 * Tiers (cumulative):
 *   1. hide `#global-search-btn kbd` and the version selector labels, trailing `#offline-badge` dot
 *   2. icon-only session picker, search button and offline badge, tighter header gaps
 *   3. `.version-selector-container` reduced to a summary trigger opening an anchored popover
 *   4. hide `.brand-title`
 *
 * The popover is the existing `#version-selector-controls` block (real IDs and listeners, never cloned)
 * shown as a fixed-position panel under the trigger.
 */

import { onUiScaleChanged, scaleUiPx } from "../utils/ui_scale.js";

export const MAX_HEADER_TIER = 4;
/** First tier in which the version selector is reduced to its summary trigger. */
export const REDUCED_VERSION_TIER = 3;
export const TIER_CLASS_PREFIX = "hdr-collapse-";
export const POPOVER_OPEN_CLASS = "popover-open";
/** Set on `#app-header` while tiers are measured: disables transitions so widths are read settled. */
export const MEASURING_CLASS = "hdr-measuring";
/** Popups opened from inside the popover (branchPicker and refPicker both use `.ref-picker`). */
export const CHILD_POPUP_SELECTOR = ".ref-picker";

/**
 * First tier (0..maxTier) whose required width fits `available` (minus `margin`); `maxTier` when none fits.
 * `measure(tier)` returns the width the header content needs in that tier. A tier below `current` (expanding)
 * must also leave `expandMargin` free, so a width sitting on a tier boundary keeps the current tier.
 */
export function selectHeaderTier({ available, measure, maxTier = MAX_HEADER_TIER, margin = 0, current = 0, expandMargin = 0 }) {
  const room = Number(available) - Number(margin || 0);
  if (!Number.isFinite(room) || room <= 0) return maxTier;
  for (let tier = 0; tier <= maxTier; tier++) {
    const limit = tier < current ? room - Number(expandMargin || 0) : room;
    if (measure(tier) <= limit) return tier;
  }
  return maxTier;
}

/** Cumulative collapse classes of `tier` (`hdr-collapse-1` .. `hdr-collapse-<tier>`). */
export function tierClasses(tier) {
  const out = [];
  for (let t = 1; t <= Math.min(tier, MAX_HEADER_TIER); t++) out.push(`${TIER_CLASS_PREFIX}${t}`);
  return out;
}

function clean(value) {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
}

/**
 * Summary of the header selection for the reduced trigger.
 * `{ project, branch, tag, release, commit, detached }` → `{ text, title }`:
 * `Project / <revision>` where the revision is the commit while detached, else the selected release
 * (when it differs from the tag), else the tag.
 */
export function formatVersionSummary({ project = "", branch = "", tag = "", release = "", commit = "", detached = false } = {}) {
  const p = clean(project);
  const b = clean(branch);
  const t = clean(tag);
  const r = clean(release);
  const c = clean(commit);
  const placeholder = (v) => !v || v === "—" || /^— .* —$/.test(v);
  const tagName = placeholder(t) ? "" : t;
  const releaseName = placeholder(r) || r === tagName ? "" : r;
  const commitName = placeholder(c) ? "" : c;
  const revision = detached
    ? (commitName || releaseName || tagName)
    : (releaseName || tagName || commitName);
  const text = [p, revision].filter(Boolean).join(" / ") || "Select revision";
  const lines = [
    `Project: ${p || "—"}`,
    `Branch: ${b || "—"}`,
    `Tag: ${tagName || "—"}`,
  ];
  if (releaseName) lines.push(`Release: ${releaseName}`);
  lines.push(`Commit: ${commitName || "—"}${detached ? " (detached)" : ""}`);
  lines.push("Click to edit the revision selection");
  return { text, title: lines.join("\n") };
}

/**
 * Position of a popover of `popSize` under `anchorRect`, clamped into `viewport` with `margin`:
 * `{ left, top, maxWidth, maxHeight }` (pixels). Opens above the anchor when there is no room below.
 */
export function clampPopoverPosition(anchorRect, popSize, viewport, { gap = 6, margin = 8 } = {}) {
  const vw = Math.max(0, Number(viewport?.width) || 0);
  const vh = Math.max(0, Number(viewport?.height) || 0);
  const maxWidth = Math.max(0, vw - 2 * margin);
  const width = Math.min(Number(popSize?.width) || 0, maxWidth);
  const height = Number(popSize?.height) || 0;
  let left = Number(anchorRect?.left) || 0;
  left = Math.max(margin, Math.min(left, vw - margin - width));
  const below = (Number(anchorRect?.bottom) || 0) + gap;
  let top = below;
  if (below + height > vh - margin) {
    const above = (Number(anchorRect?.top) || 0) - gap - height;
    top = above >= margin ? above : Math.max(margin, Math.min(below, vh - margin - height));
  }
  const maxHeight = Math.max(0, vh - margin - top);
  return { left: Math.round(left), top: Math.round(top), maxWidth: Math.round(maxWidth), maxHeight: Math.round(maxHeight) };
}

function byId(root, id) {
  return root?.getElementById ? root.getElementById(id) : root?.querySelector?.(`#${id}`) || null;
}

function selectedText(select) {
  if (!select) return "";
  const opt = select.options ? select.options[select.selectedIndex] : null;
  return clean(opt?.textContent || select.value || "");
}

/** Read the summary inputs from the header controls under `root` (`document` by default). */
export function readVersionSelection(root = globalThis.document) {
  const proj = byId(root, "project-select");
  const tag = byId(root, "version-select");
  const rc = byId(root, "rc-select");
  const commitBtn = byId(root, "commit-picker-btn");
  const project = selectedText(proj).replace(/\s+\(git only\)$/, "").replace(/\s+— missing$/, "");
  const tagText = selectedText(tag).replace(/\s+\(rc only\)$/, "");
  return {
    project,
    branch: clean(byId(root, "branch-picker-label")?.textContent || ""),
    tag: tagText,
    release: rc && !rc.hidden ? clean(rc.value || "") : "",
    commit: clean(byId(root, "commit-picker-label")?.textContent || ""),
    detached: Boolean(commitBtn?.classList?.contains("detached")),
  };
}

function cssPx(style, prop) {
  const n = parseFloat(style?.[prop] ?? style?.getPropertyValue?.(prop) ?? "");
  return Number.isFinite(n) ? n : 0;
}

function rowWidth(el, win) {
  if (!el) return 0;
  const style = win.getComputedStyle ? win.getComputedStyle(el) : null;
  const gap = cssPx(style, "columnGap") || cssPx(style, "gap");
  let sum = 0;
  let count = 0;
  for (const child of Array.from(el.children || [])) {
    const w = child.getBoundingClientRect ? child.getBoundingClientRect().width : 0;
    if (w > 0) {
      sum += w;
      count += 1;
    }
  }
  return sum + Math.max(0, count - 1) * gap + cssPx(style, "paddingLeft") + cssPx(style, "paddingRight");
}

/** Width the header content needs: paddings + `.header-left` row + gap + `.header-right` row (flex-shrink: 0 items). */
export function measureHeaderRequired(header, left, right, win = globalThis) {
  const style = win.getComputedStyle ? win.getComputedStyle(header) : null;
  const gap = cssPx(style, "columnGap") || cssPx(style, "gap");
  return cssPx(style, "paddingLeft") + cssPx(style, "paddingRight") + rowWidth(left, win) + gap + rowWidth(right, win);
}

export class HeaderLayout {
  constructor() {
    this.els = null;
    this.env = null;
    this.tier = 0;
    this.open = false;
    this._raf = null;
    this._forceNext = false;
    this._lastAvailable = null;
    this._cleanups = [];
  }

  /** Wire the live header (`document`): observers, window/UI Scale listeners, trigger and popover handlers. */
  init(doc = globalThis.document) {
    if (this.els || !doc) return this;
    const header = byId(doc, "app-header");
    const container = header?.querySelector?.(".version-selector-container");
    if (!header || !container) return this;
    return this.attach({
      header,
      left: header.querySelector(".header-left"),
      right: header.querySelector(".header-right"),
      container,
      controls: byId(doc, "version-selector-controls"),
      trigger: byId(doc, "version-summary-btn"),
      summaryLabel: byId(doc, "version-summary-label"),
      applyBtn: byId(doc, "version-apply-btn"),
      newTabBtn: byId(doc, "version-newtab-btn"),
    }, { doc, win: globalThis });
  }

  /**
   * Attach to explicit elements. `env`: `{ doc, win, raf, measure(header, left, right), available(header),
   * readSelection(), observe: false }` (all optional; tests inject fakes).
   */
  attach(els, env = {}) {
    this.els = els;
    const win = env.win || globalThis;
    this.env = {
      doc: env.doc || globalThis.document,
      win,
      raf: env.raf || ((fn) => (typeof win.requestAnimationFrame === "function" ? win.requestAnimationFrame(fn) : setTimeout(fn, 0))),
      measure: env.measure || ((h, l, r) => measureHeaderRequired(h, l, r, win)),
      available: env.available || ((h) => h.clientWidth || h.getBoundingClientRect?.().width || 0),
      readSelection: env.readSelection || (() => readVersionSelection(this.env.doc)),
    };
    const { trigger, container, applyBtn, newTabBtn } = els;
    const listen = (target, type, fn, opts) => {
      if (!target?.addEventListener) return;
      target.addEventListener(type, fn, opts);
      this._cleanups.push(() => target.removeEventListener(type, fn, opts));
    };

    listen(trigger, "click", (e) => {
      e?.stopPropagation?.();
      this.togglePopover();
    });
    // A programmatic .click() on a control hidden in the closed popover (command palette "Select
    // branch…" / "Select commit…") opens the popover first, so the picker anchors to a visible button.
    listen(container, "click", (e) => {
      if (!this.isReduced() || this.open) return;
      const t = e?.target;
      if (t && t !== trigger && !trigger?.contains?.(t) && els.controls?.contains?.(t)) this.openPopover();
    }, true);
    const closeAfter = () => this.closePopover();
    listen(applyBtn, "click", closeAfter);
    listen(newTabBtn, "click", closeAfter);
    listen(this.env.doc, "keydown", (e) => this.onKeyDown(e), true);
    listen(this.env.doc, "mousedown", (e) => this.onOutsidePointer(e), true);
    listen(win, "resize", () => this.schedule({ force: true }));

    if (env.observe !== false) {
      this._cleanups.push(onUiScaleChanged(() => this.schedule({ force: true })));
      if (typeof win.ResizeObserver === "function") {
        const ro = new win.ResizeObserver(() => this.schedule());
        for (const el of [els.header, els.left, els.right, container, ...Array.from(els.left?.children || []), ...Array.from(els.right?.children || [])]) {
          if (el) ro.observe(el);
        }
        this._cleanups.push(() => ro.disconnect());
      }
      if (typeof win.MutationObserver === "function") {
        // Label/option/hidden changes of the selector, session label and offline badge text
        const mo = new win.MutationObserver(() => this.schedule());
        const opts = { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["hidden", "class"] };
        for (const el of [els.controls, byId(this.env.doc, "session-selector-container"), els.right]) {
          if (el) mo.observe(el, opts);
        }
        this._cleanups.push(() => mo.disconnect());
      }
    }
    this.update({ force: true });
    return this;
  }

  detach() {
    for (const fn of this._cleanups.splice(0)) {
      try { fn(); } catch { /* already gone */ }
    }
    this.els = null;
    this.env = null;
  }

  isReduced() {
    return this.tier >= REDUCED_VERSION_TIER;
  }

  /** Coalesce re-evaluations into one animation frame; `force` re-measures even while the popover is open. */
  schedule({ force = false } = {}) {
    if (!this.els) return;
    if (force) this._forceNext = true;
    if (this._raf !== null) return;
    this._raf = this.env.raf(() => {
      this._raf = null;
      const f = this._forceNext;
      this._forceNext = false;
      this.update({ force: f });
    });
  }

  applyTier(tier) {
    const header = this.els.header;
    const want = new Set(tierClasses(tier));
    for (let t = 1; t <= MAX_HEADER_TIER; t++) {
      const cls = `${TIER_CLASS_PREFIX}${t}`;
      const has = header.classList.contains(cls);
      if (want.has(cls) && !has) header.classList.add(cls);
      else if (!want.has(cls) && has) header.classList.remove(cls);
    }
  }

  /** Synchronous re-evaluation: summary, tier (measured from full mode downwards) and popover position. */
  update({ force = false } = {}) {
    if (!this.els) return this.tier;
    this.syncSummary();
    const { header, left, right } = this.els;
    const available = this.env.available(header);
    // While the popover is open, content changes do not re-measure (toggling tiers would hide the
    // controls under a focused native <select>); a different header width or a forced update does.
    const skip = this.open && !force && available === this._lastAvailable;
    if (!skip) {
      // Transitions (`transition: 0.15s ease` on the search, session and badge buttons) would report
      // the previous tier's padding/font-size mid-animation; with them off every read is settled.
      header.classList.add(MEASURING_CLASS);
      try {
        const tier = selectHeaderTier({
          available,
          margin: scaleUiPx(4),
          current: this.tier,
          expandMargin: scaleUiPx(16),
          measure: (t) => {
            this.applyTier(t);
            return this.env.measure(header, left, right);
          },
        });
        this.applyTier(tier);
        // Flush the final tier's styles while transitions are still off, so nothing animates afterwards
        this.env.measure(header, left, right);
        this._lastAvailable = available;
        this.setTier(tier);
      } finally {
        header.classList.remove(MEASURING_CLASS);
      }
    }
    if (this.open) this.positionPopover();
    return this.tier;
  }

  setTier(tier) {
    this.tier = tier;
    const header = this.els.header;
    if (header.dataset) header.dataset.collapseTier = String(tier);
    if (tier < REDUCED_VERSION_TIER && this.open) this.closePopover({ silent: true });
  }

  syncSummary() {
    const { trigger, summaryLabel } = this.els;
    if (!trigger) return;
    const { text, title } = formatVersionSummary(this.env.readSelection());
    if (summaryLabel && summaryLabel.textContent !== text) summaryLabel.textContent = text;
    if (trigger.title !== title) trigger.title = title;
  }

  openPopover() {
    if (!this.els || this.open || !this.isReduced()) return false;
    this.open = true;
    this.els.container.classList.add(POPOVER_OPEN_CLASS);
    this.els.trigger?.setAttribute?.("aria-expanded", "true");
    this.positionPopover();
    return true;
  }

  closePopover({ silent = false } = {}) {
    if (!this.els || !this.open) return false;
    this.open = false;
    this.els.container.classList.remove(POPOVER_OPEN_CLASS);
    this.els.trigger?.setAttribute?.("aria-expanded", "false");
    const style = this.els.controls?.style;
    if (style) {
      style.left = "";
      style.top = "";
      style.maxWidth = "";
      style.maxHeight = "";
    }
    if (!silent) this.schedule({ force: true });
    return true;
  }

  togglePopover() {
    return this.open ? this.closePopover() : this.openPopover();
  }

  positionPopover() {
    const { trigger, container, controls } = this.els;
    if (!controls?.style) return;
    const anchor = (trigger || container).getBoundingClientRect();
    const rect = controls.getBoundingClientRect();
    const win = this.env.win;
    const pos = clampPopoverPosition(anchor, { width: rect.width, height: rect.height },
      { width: win.innerWidth || 0, height: win.innerHeight || 0 },
      { gap: scaleUiPx(6), margin: scaleUiPx(8) });
    controls.style.left = `${pos.left}px`;
    controls.style.top = `${pos.top}px`;
    controls.style.maxWidth = `${pos.maxWidth}px`;
    controls.style.maxHeight = `${pos.maxHeight}px`;
  }

  /** A child picker or dialog is open above the popover (it handles Escape and clicks itself). */
  childPopupOpen() {
    return Boolean(this.env.doc?.querySelector?.(CHILD_POPUP_SELECTOR));
  }

  onKeyDown(e) {
    if (!this.open || e?.key !== "Escape") return;
    if (this.childPopupOpen()) return;
    this.closePopover();
    this.els.trigger?.focus?.();
  }

  onOutsidePointer(e) {
    if (!this.open) return;
    const t = e?.target;
    if (!t) return;
    if (this.els.container.contains?.(t)) return;
    if (t.closest?.(CHILD_POPUP_SELECTOR)) return;
    this.closePopover();
  }
}

export const headerLayout = new HeaderLayout();
