/**
 * kernel_api_view.js - Kernel API tab (tab type `api`, S05, webapp/API.md §5.29): one section per
 * catalog (syscalls, exported symbols, parameters, tracepoints, modules, device tables). Sections
 * register themselves with `registerApiSection()`.
 */
import { SectionedView } from "../sections/sectioned_view.js";

const SECTIONS = [];

export function registerApiSection(section) {
  const i = SECTIONS.findIndex((s) => s.id === section.id);
  if (i >= 0) SECTIONS[i] = section;
  else SECTIONS.push(section);
  SECTIONS.sort((a, b) => (a.order ?? 99) - (b.order ?? 99));
}

export function apiSections() {
  return SECTIONS;
}

export class KernelApiView extends SectionedView {
  constructor() {
    super({ type: "api", title: "Kernel API", sections: SECTIONS });
  }
}
