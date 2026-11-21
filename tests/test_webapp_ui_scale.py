"""tests/test_webapp_ui_scale.py - UI Scale tokens and static enforcement guard (webapp/API_FRONTEND.md §5.5)."""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "webapp", "frontend")
CSS = os.path.join(FRONTEND, "css")
JS = os.path.join(FRONTEND, "js")
INDEX_HTML = os.path.join(FRONTEND, "index.html")

FENCE_START = "ui-scale-exempt-start"
FENCE_END = "ui-scale-exempt-end"

# Only code/diff editor lines & gutters, NodeMap generated SVGs, and documented fixed-viewBox SVGs may use fences.
ALLOWED_FENCE_PATHS = {
    "css/codeview.css",
    "js/views/nodemap/nodemap_controller.js",
    "js/views/nodemap/node_renderer.js",
    "js/views/nodemap/nodemap_export.js",
    "js/views/kconfig/kconfig_view.js",
}

CSS_LITERAL_FONT_SIZE = re.compile(
    r"(?<![-\w])font-size\s*:\s*[^;{}]*(?:\d+(?:\.\d+)?|\.\d+)(?:px|rem)\b",
    re.IGNORECASE,
)
JS_INLINE_FONT_SIZE = re.compile(
    r"font-size\s*:\s*[^;\"'`\\]*(?:\d+(?:\.\d+)?|\.\d+)(?:px|rem)\b",
    re.IGNORECASE,
)
JS_STYLE_FONT_SIZE = re.compile(
    r"\.style\.fontSize\s*=\s*[\"'`][^\"'`]*?(?:\d+(?:\.\d+)?|\.\d+)(?:px|rem)\b",
    re.IGNORECASE,
)
SVG_LITERAL_FONT_SIZE = re.compile(
    r"<text\b[^>]*\bfont-size\s*=\s*[\"']\s*(?:\d+(?:\.\d+)?|\.\d+)(?:px|rem)?\s*[\"']",
    re.IGNORECASE,
)
VAR_TOKEN_FALLBACK = re.compile(r"var\(--(?:fs-[\w-]+|code-font-size)(?:,\s*[^)]+)?\)")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def strip_fences(text: str, rel_path: str, bad_fences: list[str]) -> str:
    starts = len(re.findall(re.escape(FENCE_START), text))
    ends = len(re.findall(re.escape(FENCE_END), text))
    if starts != ends:
        bad_fences.append(f"{rel_path}: mismatched {FENCE_START}/{FENCE_END} ({starts} vs {ends})")
    if (starts or ends) and rel_path not in ALLOWED_FENCE_PATHS:
        bad_fences.append(f"{rel_path}: {FENCE_START} is not permitted here")
    text = re.sub(r"/\*\s*ui-scale-exempt-start.*?\*/.*?/\*\s*ui-scale-exempt-end.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--\s*ui-scale-exempt-start.*?-->.*?<!--\s*ui-scale-exempt-end.*?-->", "", text, flags=re.S)
    return text


def scan_css_text(text: str, rel_path: str, bad_fences: list[str]) -> list[str]:
    cleaned = strip_fences(text, rel_path, bad_fences)
    cleaned = VAR_TOKEN_FALLBACK.sub("", cleaned)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.S)
    return [f"{rel_path}: {m.group(0).strip()}" for m in CSS_LITERAL_FONT_SIZE.finditer(cleaned)]


def scan_js_text(text: str, rel_path: str, bad_fences: list[str]) -> list[str]:
    hits: list[str] = []
    cleaned = strip_fences(text, rel_path, bad_fences)
    cleaned = VAR_TOKEN_FALLBACK.sub("", cleaned)
    for lineno, line in enumerate(cleaned.splitlines(), 1):
        if line.lstrip().startswith(("//", "*", "/*")):
            continue
        for rx in (JS_INLINE_FONT_SIZE, JS_STYLE_FONT_SIZE, SVG_LITERAL_FONT_SIZE):
            for m in rx.finditer(line):
                hits.append(f"{rel_path}:{lineno}: {m.group(0).strip()}")
    return hits


class TestUiScale(unittest.TestCase):
    def test_ui_scale_tokens_defined(self) -> None:
        css = read(os.path.join(CSS, "variables.css"))
        for tok in ("--ui-font-scale", "--ui-layout-scale", "--code-font-size", "--code-row-height"):
            self.assertIn(f"{tok}:", css, f"variables.css missing {tok}")
        for size in ("8", "9", "10", "11", "11-5", "12", "13", "13-5", "14", "15", "16", "16-5", "18", "20", "24", "28"):
            self.assertIn(f"--fs-{size}:", css, f"variables.css missing --fs-{size}")

    def test_no_literal_font_sizes_outside_tokens(self) -> None:
        stray: list[str] = []
        bad_fences: list[str] = []

        for f in sorted(os.listdir(CSS)):
            if not f.endswith(".css") or f == "variables.css":
                continue
            stray += scan_css_text(read(os.path.join(CSS, f)), f"css/{f}", bad_fences)

        for root, _, files in os.walk(JS):
            for f in sorted(files):
                if not f.endswith(".js"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, FRONTEND).replace(os.sep, "/")
                stray += scan_js_text(read(full), rel, bad_fences)

        html = strip_fences(read(INDEX_HTML), "index.html", bad_fences)
        html = VAR_TOKEN_FALLBACK.sub("", html)
        html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        for m in JS_INLINE_FONT_SIZE.finditer(html):
            stray.append(f"index.html: {m.group(0).strip()}")

        self.assertEqual(bad_fences, [], f"Invalid UI scale exemption fences: {bad_fences}")
        self.assertEqual(stray, [], f"Literal font-size outside --fs-* / --code-font-size tokens: {stray}")

    def test_code_font_tokens_scoped_to_codeview(self) -> None:
        stray: list[str] = []
        for f in sorted(os.listdir(CSS)):
            if not f.endswith(".css") or f in ("variables.css", "codeview.css"):
                continue
            text = re.sub(r"/\*.*?\*/", "", read(os.path.join(CSS, f)), flags=re.S)
            for tok in ("--code-font-size", "--code-row-height"):
                if tok in text:
                    stray.append(f"css/{f}: uses {tok}")
        self.assertEqual(stray, [], f"Code-view tokens leaked outside codeview.css: {stray}")

    def test_guard_rejects_violations(self) -> None:
        bad_fences: list[str] = []
        self.assertTrue(scan_css_text(".foo { font-size: 12px; }", "css/sections.css", bad_fences))
        self.assertTrue(scan_css_text(".foo { font-size: 1.5rem; }", "css/docs.css", bad_fences))
        self.assertEqual(scan_css_text(".foo { font-size: var(--fs-12, 12px); }", "css/sections.css", bad_fences), [])
        self.assertTrue(scan_js_text('el.innerHTML = `<span style="font-size: 13px">x</span>`;', "js/x.js", bad_fences))
        self.assertTrue(scan_js_text('el.style.fontSize = "11px";', "js/x.js", bad_fences))
        self.assertTrue(scan_js_text('const s = `<text font-size="10">a</text>`;', "js/x.js", bad_fences))
        self.assertEqual(scan_js_text('el.innerHTML = `<span style="font-size: var(--fs-13, 13px)">x</span>`;', "js/x.js", bad_fences), [])
        self.assertEqual(bad_fences, [])
        scan_css_text("/* ui-scale-exempt-start */ .x { font-size: 10px; } /* ui-scale-exempt-end */", "css/sections.css", bad_fences)
        self.assertEqual(len(bad_fences), 1)


if __name__ == "__main__":
    unittest.main()
