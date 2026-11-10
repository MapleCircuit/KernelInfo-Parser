/**
 * tour_player.js - Plays a code tour (G05): a floating panel with step k/N, the step's text and Prev / Next;
 * each step opens its target in the active pane.
 */
import { escapeHtml } from "../../components/modal.js";
import { renderMarkdownFragment } from "../docs/md_renderer.js";
import { targetLabel } from "./bookmark_model.js";
import { openTarget } from "./bookmark_store.js";

let player = null;

export function playTour(tour, start = 0) {
  player?.remove();
  const el = document.createElement("div");
  el.className = "tour-player";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", `Tour: ${tour.title}`);
  document.body.appendChild(el);
  player = el;
  let idx = Math.max(0, Math.min(start, tour.steps.length - 1));
  const show = () => {
    const step = tour.steps[idx];
    el.innerHTML = `<div class="tp-head"><strong>${escapeHtml(tour.title)}</strong><span class="tp-pos">${idx + 1} / ${tour.steps.length}</span><button type="button" data-tp="close" title="Close the tour">✕</button></div>
      <div class="tp-title">${escapeHtml(step.title || targetLabel(step.target))}</div>
      <div class="tp-target"><a href="#" data-tp="open">${escapeHtml(targetLabel(step.target))}</a></div>
      <div class="tp-body kdoc-body">${step.markdown ? renderMarkdownFragment(step.markdown) : ""}</div>
      <div class="tp-nav"><button type="button" data-tp="prev" ${idx === 0 ? "disabled" : ""}>← Prev</button><button type="button" data-tp="next" ${idx === tour.steps.length - 1 ? "disabled" : ""}>Next →</button></div>`;
    openTarget(step.target);
  };
  el.addEventListener("click", (e) => {
    const act = e.target.closest("[data-tp]")?.dataset.tp;
    if (!act) return;
    e.preventDefault();
    if (act === "close") {
      el.remove();
      player = null;
    } else if (act === "prev" && idx > 0) {
      idx--;
      show();
    } else if (act === "next" && idx < tour.steps.length - 1) {
      idx++;
      show();
    } else if (act === "open") {
      openTarget(tour.steps[idx].target);
    }
  });
  if (!tour.steps.length) {
    el.innerHTML = `<div class="tp-head"><strong>${escapeHtml(tour.title)}</strong><button type="button" data-tp="close">✕</button></div><div class="tp-body">This tour has no steps yet.</div>`;
    return;
  }
  show();
}
