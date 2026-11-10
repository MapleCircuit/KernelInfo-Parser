"""core/encoding.py - File encoding and coordinate utilities.

Provides safe line splitting compliant with parser/AGENTS.md Rule 5
and zero-allocation byte-level coordinate computation for large buffers.
"""
from __future__ import annotations


def safe_split_lines(text: str, keepends: bool = False) -> list[str]:
    """Split text into lines while preserving strict newlines.

    Enforces \\r\\n normalization (.replace("\\r\\n", "\\n").split("\\n"))
    and prevents splitting on form feed (\\x0c) or other unicode line breaks,
    satisfying parser/AGENTS.md Rule 5 to preserve libclang synchronization.
    """
    if keepends:
        if not text:
            return []
        norm = text.replace("\r\n", "\n")
        parts = norm.split("\n")
        if norm.endswith("\n"):
            return [p + "\n" for p in parts[:-1]]
        return [p + "\n" for p in parts[:-1]] + [parts[-1]]
    return text.replace("\r\n", "\n").split("\n")


def safe_byte_coordinates(raw_bytes: bytes) -> tuple[int, int]:
    """Calculate 1-based line count and last line character length directly from raw bytes.

    Uses count(b"\\n") and rfind(b"\\n") to avoid string allocations for large files.

    Returns:
        tuple[int, int]: (line_count, last_char_count) where both values are >= 1.
    """
    line_count = max(1, raw_bytes.count(b"\n") + 1)
    last_nl = raw_bytes.rfind(b"\n")
    if last_nl == -1:
        last_char_count = max(1, len(raw_bytes))
    else:
        last_char_count = max(1, len(raw_bytes) - (last_nl + 1))
    return line_count, last_char_count
