/**
 * dts.js - DeviceTree Source syntax grammar (.dts, .dtsi).
 */

export const DTS_KEYWORDS = new Set([
  "compatible", "model", "status", "reg", "interrupts", "clocks",
  "clock-names", "dmas", "dma-names", "pinctrl-names", "pinctrl-0",
  "phandle", "ranges", "interrupt-parent"
]);

export const DTS_DIRECTIVES = new Set([
  "/dts-v1/", "/plugin/", "/memreserve/", "/delete-node/", "/delete-property/"
]);

export const dtsGrammar = {
  id: "dts",
  name: "DeviceTree",
  ftypes: [],
  extensions: [".dts", ".dtsi"],
  filenames: [],
  shebangs: [],
  keywords: DTS_KEYWORDS,
  types: DTS_DIRECTIVES,
  hasPreprocessor: true,
  supportsInclude: true,
  isCommentLine(trimmed) {
    return trimmed.startsWith("//") || trimmed.startsWith("/*") || (trimmed.startsWith("*") && !trimmed.startsWith("*="));
  },
  isType(val) {
    return DTS_DIRECTIVES.has(val);
  }
};

