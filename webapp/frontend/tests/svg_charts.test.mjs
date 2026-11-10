import { test } from "node:test";
import assert from "node:assert/strict";
import { barChartSvg, hbarsHtml, kpiHtml, sparklineSvg } from "../js/utils/svg_charts.js";

test("charts are escaped SVG with the data", () => {
  const svg = barChartSvg([{ x: 0, value: 10 }, { x: 5, value: 20, label: "<d5>" }], { markers: [{ x: 3, label: "rc1" }] });
  assert.ok(svg.startsWith("<svg"));
  assert.equal((svg.match(/<rect/g) || []).length, 2);
  assert.ok(svg.includes("&lt;d5&gt;: 20"));
  assert.ok(svg.includes("rc1"));
  assert.equal(barChartSvg([]), "");
  assert.ok(sparklineSvg([1, 2, 3]).includes("<polyline"));
  assert.ok(hbarsHtml([{ label: "<b>", value: 5 }]).includes("&lt;b&gt;"));
  assert.ok(kpiHtml([["1", "x"]]).includes("sv-kpi"));
});
