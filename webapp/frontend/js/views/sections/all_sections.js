/**
 * all_sections.js - Imports every section of the Kernel API and Insights tabs, so they register
 * before the first tab renders (webapp/API.md §5.29, §5.30).
 */
import "../insights/stable_section.js";
import "../insights/release_sections.js";
import "../insights/security_section.js";
import "../kernel_api/api_sections.js";
import "../kernel_api/devices_section.js";
