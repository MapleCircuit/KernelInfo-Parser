/**
 * tour_dialog.js - "Add this line to a tour…" (G05): pick a tour (or name a new one) and a step title.
 */
import { toast } from "../../components/toast.js";
import { openModal, escapeHtml } from "../../components/modal.js";
import { targetLabel } from "./bookmark_model.js";
import { listTours, saveTour } from "./bookmark_store.js";

export async function addToTourDialog(target) {
  const tours = await listTours();
  const modal = openModal({ title: "Add to a code tour", subtitle: targetLabel(target), width: "520px",
    footerHtml: `<button class="code-btn active" id="tour-add" type="button">Add step</button>` });
  modal.body.innerHTML = `<div class="ki-form-row"><label>Tour <select id="tour-pick">${tours.map((t) => `<option value="${escapeHtml(t.key)}">${escapeHtml(t.data.title)} (${t.data.steps.length})</option>`).join("")}<option value="">New tour…</option></select></label></div>
    <div class="ki-form-row" id="tour-new-row" ${tours.length ? "hidden" : ""}><label>New tour title <input id="tour-new-title" type="text" /></label></div>
    <div class="ki-form-row"><label>Step title <input id="tour-step-title" type="text" value="${escapeHtml(targetLabel(target))}" /></label></div>
    <div class="ki-form-row"><label>Text (Markdown)<textarea id="tour-step-md" class="ki-textarea" rows="4"></textarea></label></div>`;
  const pick = modal.body.querySelector("#tour-pick");
  pick.onchange = () => { modal.body.querySelector("#tour-new-row").hidden = Boolean(pick.value); };
  modal.footer.querySelector("#tour-add").onclick = async () => {
    const step = { target, title: modal.body.querySelector("#tour-step-title").value.trim(), markdown: modal.body.querySelector("#tour-step-md").value };
    try {
      const rec = tours.find((t) => t.key === pick.value);
      if (rec) {
        const data = structuredClone(rec.data);
        data.steps.push(step);
        await saveTour(data, rec);
        toast.success(`Step ${data.steps.length} added to “${data.title}”`);
      } else {
        const title = modal.body.querySelector("#tour-new-title").value.trim();
        if (!title) {
          toast.info("Name the new tour");
          return;
        }
        await saveTour({ title, description: "", steps: [step] });
        toast.success(`Tour “${title}” created`);
      }
      modal.close();
    } catch (err) {
      toast.error(err.message);
    }
  };
}
