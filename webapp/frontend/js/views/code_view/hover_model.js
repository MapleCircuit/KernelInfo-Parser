/**
 * hover_model.js - DOM-free parts of the hover cards (G04): settings, the timing state machine, labels.
 */
export const HOVER_KEYS = { enabled: "kip-hover-cards", delay: "kip-hover-delay" };

export function hoverSettings(storage = globalThis.localStorage) {
  let enabled = true;
  let delay = 450;
  try {
    enabled = storage?.getItem(HOVER_KEYS.enabled) !== "0";
    const d = parseInt(storage?.getItem(HOVER_KEYS.delay) || "", 10);
    if (d >= 300 && d <= 1000) delay = d;
  } catch {
    // storage unavailable: defaults
  }
  return { enabled, delay };
}

/**
 * When a card shows and hides. `rest(target)` on every pointer move over a token (the timer restarts while the
 * pointer moves), `leave()` when it leaves the token, `enterCard()` / `leaveCard()` for the card itself, `hide()`
 * (Escape, scroll, click) and `suppress(on)` (a menu or the inspector is open, a drag or a selection is on).
 */
export class HoverIntent {
  constructor({ delay = 450, grace = 150, onShow = () => {}, onHide = () => {}, setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (t) => clearTimeout(t) } = {}) {
    Object.assign(this, { delay, grace, onShow, onHide, setTimer, clearTimer });
    this.target = null;
    this.shown = null;
    this.timer = null;
    this.hideTimer = null;
    this.suppressed = false;
  }

  _clear() {
    if (this.timer) this.clearTimer(this.timer);
    this.timer = null;
  }

  _clearHide() {
    if (this.hideTimer) this.clearTimer(this.hideTimer);
    this.hideTimer = null;
  }

  rest(target) {
    if (this.suppressed) return;
    if (this.shown && target === this.shown) {
      this._clearHide();
      return;
    }
    this.target = target;
    this._clear();
    this.timer = this.setTimer(() => {
      this.timer = null;
      if (this.suppressed || this.target !== target) return;
      this._clearHide();
      if (this.shown && this.shown !== target) this.onHide(this.shown);
      this.shown = target;
      this.onShow(target);
    }, this.delay);
  }

  leave() {
    this._clear();
    this.target = null;
    if (this.shown) this._scheduleHide();
  }

  enterCard() {
    this._clearHide();
  }

  leaveCard() {
    if (this.shown) this._scheduleHide();
  }

  _scheduleHide() {
    this._clearHide();
    this.hideTimer = this.setTimer(() => {
      this.hideTimer = null;
      this.hide();
    }, this.grace);
  }

  hide() {
    this._clear();
    this._clearHide();
    this.target = null;
    if (this.shown) {
      const was = this.shown;
      this.shown = null;
      this.onHide(was);
    }
  }

  suppress(on) {
    this.suppressed = Boolean(on);
    if (on) this.hide();
  }
}

const SKIP_TYPES = new Set(["2", "87", "142", "143", "78"]);
const KIND_LABEL = {
  C_functionprotodecl: "function", C_functionproto: "function", FunctionDecl: "function", CPPro_define_macro: "macro",
  C_structdecl: "struct", C_uniondecl: "union", C_enumdecl: "enum", C_enumequal: "enum constant", C_SCtypedef: "typedef",
  C_vardecl: "variable", kconfig: "Kconfig",
  Rust_Fn: "fn", Rust_Struct: "struct", Rust_Enum: "enum", Rust_Union: "union", Rust_Trait: "trait",
  Rust_Type: "type", Rust_Const: "const", Rust_Static: "static", Rust_MacroDef: "macro",
  ASM_Label: "label", ASM_Macro: "asm macro", ASM_Instruction: "instruction", ASM_Directive: "directive",
};
export const kindLabel = (kind) => KIND_LABEL[kind] || String(kind || "symbol").replace(/^C_|^CPPro_/, "").replace(/_/g, " ");

/** Whether a token can have a card (not comments, includes or preprocessor structure). */
export function hoverable(el) {
  if (!el || el.classList.contains("tok-comment") || el.classList.contains("ast-include")) return false;
  if (SKIP_TYPES.has(el.dataset.typeId || "")) return false;
  const name = el.dataset.name || el.textContent || "";
  return /^[A-Za-z_]\w*$/.test(name) && name.length > 1;
}
