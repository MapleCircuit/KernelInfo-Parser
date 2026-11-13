"""Detection of version-wide (systemic) parse failures."""
from core.globalstuff import (
    COLOR,
)
import os

import re
from collections import Counter
from pipeline.state import logger




# One error message on at least this many files, and on at least this share of the files parsed so far,
# stops the version: the parser or its environment (libclang, Python packages) is broken, not the sources
SYSTEMIC_ERROR_MIN_FILES = 5


SYSTEMIC_ERROR_FRACTION = 0.5


_TB_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)', re.MULTILINE)


class SystemicParseError(RuntimeError):
    """Many files of a version failed with the same error, so the version is not recorded."""


def _error_origin(tb: str) -> str:
    """Where a worker traceback was raised: its innermost frame, and the innermost frame in this repository.

    'clang/cindex.py:1629 in inner, called from parser/c_ast/cursor_tree.py:2607 in exec_punctuation'
    """
    frames = _TB_FRAME_RE.findall(tb or "")
    if not frames:
        return ""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep  # repository root

    def _fmt(frame: tuple[str, str, str]) -> str:
        path, line, func = frame
        return f"{path.removeprefix(root)}:{line} in {func}"

    own = next((fr for fr in reversed(frames) if fr[0].startswith(root)), None)
    if own is None or own == frames[-1]:
        return _fmt(frames[-1])
    return f"{_fmt(frames[-1])}, called from {_fmt(own)}"


def _raise_if_systemic(label: str, errors: list[tuple[str, str, str]], files_seen: int) -> None:
    """Raise SystemicParseError when one error message accounts for most of the files parsed so far."""
    counts = Counter(err for _f, err, _tb in errors)
    if not counts:
        return
    message, count = counts.most_common(1)[0]
    if count < SYSTEMIC_ERROR_MIN_FILES or count < SYSTEMIC_ERROR_FRACTION * files_seen:
        return
    tb = next((tb for _f, err, tb in errors if err == message and tb), "")
    origin = _error_origin(tb)
    examples = [f.split("\t")[-1] for f, err, _tb in errors if err == message][:3]
    text = (
        f"{label}: {count} of {files_seen} files failed with the same error {message!r}"
        f"{f' raised at {origin}' if origin else ''} (e.g. {', '.join(examples)}). The same error on this many "
        "files comes from the parser or its environment (libclang, Python packages), not from the sources; "
        "the version is stopped before it is recorded."
    )
    logger.critical(COLOR.red(text))
    if tb:
        logger.error(f"First traceback of that error:\n{tb}")
    raise SystemicParseError(text)
