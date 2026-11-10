/**
 * insights_view.js - Insights tab (tab type `insights`, S05, webapp/API.md §5.30): reports over the
 * history (release stats, merge window, subsystem health, stable backports, security).
 * Sections register themselves with `registerInsightsSection()`.
 */
import { SectionedView } from "../sections/sectioned_view.js";

const SECTIONS = [];

export function registerInsightsSection(section) {
  const i = SECTIONS.findIndex((s) => s.id === section.id);
  if (i >= 0) SECTIONS[i] = section;
  else SECTIONS.push(section);
  SECTIONS.sort((a, b) => (a.order ?? 99) - (b.order ?? 99));
}

export function insightsSections() {
  return SECTIONS;
}

export class InsightsView extends SectionedView {
  constructor() {
    super({ type: "insights", title: "Insights", sections: SECTIONS });
  }
}
