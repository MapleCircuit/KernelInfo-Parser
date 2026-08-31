"""core/StringWrangler.py - Text formatting, column alignment, and terminal boxing utilities."""
import textwrap
from typing import Any, Callable

try:
    from wcwidth import wcwidth
    __UNICODE_SUPPORT = True
except ImportError:
    print("***** Warning : Install package wcwidth to use unicode utilities *****")
    __UNICODE_SUPPORT = False


__STRING_WRANGLER_DEFAULT_TAG = "###"


def align_columns(rows: list[list[str]], padding: int = 2) -> list[list[str]]:
    """Align 2D matrix of cell strings into padded columnar format."""
    if not rows or not rows[0]:
        return []
    widths = [
        max(len(row[i]) for row in rows)
        for i in range(len(rows[0]))
    ]

    aligned = []
    for row in rows:
        aligned.append([
            cell.ljust(widths[i] + padding)
            for i, cell in enumerate(row)
        ])

    return aligned


def tag_lines(lines: list[str], prefix: str = __STRING_WRANGLER_DEFAULT_TAG) -> list[str]:
    """Prefix each line in a list with specified tag prefix."""
    return [f"{prefix}{line}" for line in lines]


def group_lines(lines: list[str], grouping_function: Callable[[str], bool]) -> list[list[str]]:
    """Group lines into sub-lists whenever grouping_function evaluates to True for a line."""
    groups = []
    current = []

    for line in lines:
        if grouping_function(line) and current:
            groups.append(current)
            current = []
        current.append(line)

    if current:
        groups.append(current)

    return groups


def normalize(text: str) -> str:
    """Remove redundant whitespace from string."""
    return " ".join(text.strip().split())


def listify(items: list[Any]) -> list[str]:
    """Convert a collection of items into a list of strings."""
    return [str(item) for item in items]


def __wrap_lines_no_split(lines: list[str], max_len: int) -> list[list[str]]:
    """Wrap lines without splitting whole words."""
    groups = []
    for line in lines:
        wrapped = textwrap.wrap(line, max_len)
        if not wrapped:
            wrapped = [line]
        groups.append(wrapped)
    return groups


def wrap_lines(lines: list[str], max_len: int, no_split: bool = True) -> list[list[str]]:
    """Wrap lines to maximum line length and return nested groups of strings."""
    assert isinstance(lines, list), "lines must be a list of string"
    assert lines, "Need at least 1 line"
    assert max_len > 0, "can't split string to less than 1 characters"

    if no_split:
        return __wrap_lines_no_split(lines, max_len)

    groups = []
    for line in lines:
        if not line:
            groups.append([""])
            continue

        wrapped = []
        remaining = line
        while remaining:
            wrapped.append(remaining[:max_len])
            remaining = remaining[max_len:]
        groups.append(wrapped)

    return groups


def render_with_indent(groups: list[list[str]], indent: str = __STRING_WRANGLER_DEFAULT_TAG) -> list[list[str]]:
    """Add indentation prefix to non-initial lines in each group of strings."""
    rendered = []
    for group in groups:
        if not group:
            rendered.append([])
            continue

        out = [group[0]]
        for line in group[1:]:
            out.append(indent + line)
        rendered.append(out)

    return rendered


def render_ansi_box(groups: list[list[str]]) -> list[list[str]]:
    """Render an ANSI Unicode box border around a group of strings."""
    rendered = []
    for group in groups:
        if not group:
            rendered.append([])
            continue

        width = max(len(line) for line in group)
        top = f"┌{'─' * (width + 2)}┐"
        bottom = f"└{'─' * (width + 2)}┘"

        boxed = [top]
        for line in group:
            boxed.append(f"│ {line.ljust(width)} │")
        boxed.append(bottom)
        rendered.append(boxed)

    return rendered


if __UNICODE_SUPPORT:
    def visible_len_unicode(text: str) -> int:
        """Return visible terminal character column width for Unicode text."""
        width = 0
        for ch in text:
            w = wcwidth(ch)
            if w > 0:
                width += w
        return width

    def pad_to_visible_width_unicode(text: str, target_width: int) -> str:
        """Pad string with trailing spaces to match visible terminal character width."""
        padding = target_width - visible_len_unicode(text)
        if padding > 0:
            return text + (" " * padding)
        return text

    def render_ansi_box_unicode(groups: list[list[str]]) -> list[list[str]]:
        """Render an ANSI Unicode box border supporting variable-width Unicode characters."""
        rendered = []
        for group in groups:
            if not group:
                rendered.append([])
                continue

            content_width = max(visible_len_unicode(line) for line in group)
            top = f"┌{'─' * (content_width + 2)}┐"
            bottom = f"└{'─' * (content_width + 2)}┘"

            boxed = [top]
            for line in group:
                padded = pad_to_visible_width_unicode(line, content_width)
                boxed.append(f"│ {padded} │")
            boxed.append(bottom)
            rendered.append(boxed)

        return rendered
else:
    def visible_len_unicode(text: str) -> int:
        """Fallback when wcwidth is unavailable."""
        return NotImplemented

    def pad_to_visible_width_unicode(text: str, target_width: int) -> str:
        """Fallback when wcwidth is unavailable."""
        return NotImplemented

    def render_ansi_box_unicode(groups: list[list[str]]) -> list[list[str]]:
        """Fallback when wcwidth is unavailable."""
        return NotImplemented
   
