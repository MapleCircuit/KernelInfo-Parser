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

test("barChartSvg highlights selected bar and places markers inside the right edge", () => {
  const pts = [{ x: 0, value: 5 }, { x: 10, value: 15 }, { x: 50, value: 8 }];
  const svg = barChartSvg(pts, {
    x0: 0,
    x1: 50,
    highlightX: 10,
    markers: [
      { x: 14, label: "v7.3-rc1" },
      { x: 49.5, label: "d49.5", color: "var(--accent-green)", dash: "", width: 1.5, y: 26 },
    ],
  });
  assert.ok(svg.includes('data-x="10" class="sv-bar-active"'));
  assert.ok(svg.includes('fill="var(--accent-green)"'));
  assert.ok(svg.includes('text-anchor="end">d49.5</text>'));
  assert.ok(svg.includes('y="26"'));
});
