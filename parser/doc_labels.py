"""parser/doc_labels.py - `.. _label:` targets of a Documentation `.rst` blob (`m_blob_facts` family 5, pure).

`[[label, line, title], ...]` in file order: the label normalized like the webapp's label index
(backquotes stripped, lower-case) and the title of the section it precedes. The window a title is read
from is the one `git grep -n -A6` gives the webapp's Git path (`DocsService.get_label_index()`): the lines
after the label up to 6 lines past the last label of its run, so both paths give the same index.
"""
from __future__ import annotations

import json
import re
import zlib
from typing import Any

FAMILY = 5
FORMAT = 1
CONTEXT = 6

ADORNMENT_RE = re.compile(r"""^([=\-`:.'"~^_*+#<>!$%&,;?@\[\]\\/|{}()])\1{2,}\s*$""")
LABEL_RE = re.compile(r"^\.\.\s+_([^:`][^:]*|`[^`]+`):\s*$")
# The webapp's `git grep -E '^\.\.[[:space:]]+_[^:]+:[[:space:]]*$'`
GREP_RE = re.compile(r"^\.\.\s+_[^:\n]+:\s*$")
GREP_RE_M = re.compile(r"^\.\.\s+_[^:\n]+:\s*$", re.M)


def applies(path: str) -> bool:
    return path.startswith("Documentation/") and path.endswith(".rst")


def title_after(lines: list[str]) -> str:
    """Section title following a label (skips blank lines, further labels, and overlines)."""
    rest = lines
    i = 0
    while i < len(rest) and (not rest[i].strip() or LABEL_RE.match(rest[i])):
        i += 1
    if i < len(rest) and ADORNMENT_RE.match(rest[i]):
        i += 1
    if i + 1 < len(rest) and rest[i].strip() and ADORNMENT_RE.match(rest[i + 1]):
        return rest[i].strip()
    return ""


def extract(text: str) -> list[list[Any]]:
    if not text or ".. _" not in text:
        return []
    matches = list(GREP_RE_M.finditer(text))
    if not matches:
        return []

    newlines = [i for i, c in enumerate(text) if c == "\n"]
    total_lines = len(newlines) + (0 if text.endswith("\n") else 1)

    import bisect
    line_indices = [bisect.bisect_right(newlines, m.start()) for m in matches]

    # Runs of matches whose -A6 contexts touch form one group; a group ends CONTEXT lines past its last match
    group_end: dict[int, int] = {}
    run: list[int] = []
    for i in line_indices:
        if run and i > run[-1] + CONTEXT + 1:
            for j in run:
                group_end[j] = min(total_lines - 1, run[-1] + CONTEXT)
            run = []
        run.append(i)
    for j in run:
        group_end[j] = min(total_lines - 1, run[-1] + CONTEXT)

    out: list[list[Any]] = []
    for m, i in zip(matches, line_indices):
        line_text = m.group(0).strip("\r\n")
        m_label = LABEL_RE.match(line_text)
        if not m_label:
            continue
        label = m_label.group(1).strip("`").strip().lower()

        target_end_line = group_end[i]
        if target_end_line >= i + 1:
            start_off = newlines[i] + 1 if i < len(newlines) else len(text)
            end_off = newlines[target_end_line] if target_end_line < len(newlines) else len(text)
            context_slice = text[start_off:end_off]
            context_lines = context_slice.split("\n")
            title = title_after(context_lines)
        else:
            title = ""
        out.append([label, i + 1, title])
    return out


def encode(record: list[list[Any]] | None) -> bytes:
    if not record:
        return b""
    return zlib.compress(json.dumps(record, separators=(",", ":")).encode(), 3)


def decode(data: bytes | bytearray | memoryview | None) -> list[tuple[str, int, str]] | None:
    if data is None:
        return None
    raw = bytes(data)
    if not raw:
        return []
    try:
        return [(str(a), int(b), str(c)) for a, b, c in json.loads(zlib.decompress(raw))]
    except (zlib.error, ValueError, TypeError):
        return None
