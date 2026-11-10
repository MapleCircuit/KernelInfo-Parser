"""tests/test_webapp_theme_contrast.py - Theme tokens: WCAG contrast per theme and no stray literal colors (G08, webapp/API.md section 5.5)."""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "webapp", "frontend", "css")
JS = os.path.join(ROOT, "webapp", "frontend", "js")
HEX = re.compile(r"(?<![\w&#/])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
SYNTAX = ["--syn-keyword", "--syn-type", "--syn-number", "--syn-string", "--syn-comment", "--syn-punct", "--syn-preproc", "--syn-function",
          "--syn-identifier", "--syn-variable", "--syn-attribute", "--syn-struct", "--syn-macro", "--diff-add-fg", "--diff-del-fg", "--diff-rename-fg"]


def blocks(css: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for sel, body in re.findall(r"(:root(?:\[data-theme=\"[\w-]+\"\])?)\s*\{([^{}]*)\}", css):
        decls = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
        out.setdefault(sel, {}).update(decls)
    return out


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def themes() -> dict[str, dict[str, str]]:
    css = read(os.path.join(CSS, "variables.css"))
    b = blocks(css)
    base = b[":root"]
    return {"dark": base} | {t: {**base, **b[f':root[data-theme="{t}"]']} for t in ("light", "hc-dark", "hc-light")}


def luminance(hex_color: str) -> float:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class TestThemes(unittest.TestCase):
    def test_contrast(self) -> None:
        for name, t in themes().items():
            hc = name.startswith("hc-")
            need = 7.0 if hc else 4.5
            bg = t["--bg-primary"]
            for fg in ["--text-primary", "--text-secondary", *SYNTAX]:
                self.assertGreaterEqual(round(contrast(t[fg], bg), 2), need, f"{name}: {fg} {t[fg]} on {bg}")
            # De-emphasized text: 3:1 (large / non-essential) in the normal themes, 4.5:1 in high contrast
            self.assertGreaterEqual(round(contrast(t["--text-muted"], bg), 2), 4.5 if hc else 3.0, f"{name}: --text-muted")
            self.assertGreaterEqual(round(contrast(t["--text-primary"], t["--bg-secondary"]), 2), need, f"{name}: text on panels")

    def test_every_theme_defines_the_themed_tokens(self) -> None:
        b = blocks(read(os.path.join(CSS, "variables.css")))
        for t in ("light", "hc-dark", "hc-light"):
            for tok in ["--bg-primary", "--text-primary", "--accent-blue", *SYNTAX]:
                self.assertIn(tok, b[f':root[data-theme="{t}"]'], f"{t} misses {tok}")

    def test_no_literal_colors_outside_tokens(self) -> None:
        stray = []
        for f in sorted(os.listdir(CSS)):
            if not f.endswith(".css") or f == "variables.css":
                continue
            text = read(os.path.join(CSS, f))
            text = re.sub(r"/\* theme-fixed-start.*?theme-fixed-end \*/", "", text, flags=re.S)  # mconf TUI colors
            text = re.sub(r":where\(:root\)\s*\{[^}]*\}", "", text)  # token definitions (docs.css)
            text = re.sub(r"var\(--[\w-]+,\s*[^)]*\)", "", text)  # fallbacks
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            stray += [f"{f}: {m.group(0)}" for m in HEX.finditer(text)]
        for root, _, files in os.walk(JS):
            for f in files:
                if not f.endswith(".js"):
                    continue
                for line in read(os.path.join(root, f)).splitlines():
                    s = re.sub(r"var\(--[\w-]+,\s*[^)]*\)", "", line)
                    s = re.sub(r"token\(\"--[\w-]+\",\s*\"#[0-9a-fA-F]+\"\)", "", s)
                    if s.lstrip().startswith(("//", "*", "/*")):
                        continue
                    for m in HEX.finditer(s):
                        before = s[max(0, m.start() - 12):m.start()]
                        if re.search(r"[:= (\"'`,]\s*$", before) and not re.search(r"(href=|#L|#n|hash|url|\?|&)$", before):
                            stray.append(f"{f}: {m.group(0)} in {line.strip()[:80]}")
        self.assertEqual(stray, [])


if __name__ == "__main__":
    unittest.main()
