/**
 * toast.js - Toast notification display.
 */
class ToastManager {
  constructor() {
    this.container = document.getElementById("toast-container");
    if (!this.container) {
      this.container = document.createElement("div");
      this.container.id = "toast-container";
      document.body.appendChild(this.container);
    }
  }

  show(message, type = "info", duration = 3000) {
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;

    this.container.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = "0";
      toast.style.transform = "translateY(8px)";
      toast.style.transition = "opacity 0.2s ease, transform 0.2s ease";
      setTimeout(() => toast.remove(), 200);
    }, duration);
  }

  success(msg) { this.show(msg, "success"); }
  error(msg) { this.show(msg, "error", 4500); }
  info(msg) { this.show(msg, "info"); }
}

export const toast = new ToastManager();
