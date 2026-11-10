"""parser/git_ast/release_order.py - Kernel release tag names and their order (pure, no I/O).

Shared by the parser's release statistics step (parser/git_ast/release_stats.py, core/API.md section 10)
and the webapp's release index (webapp/backend/services/release_service.py, webapp/API.md section 2.17),
so both cut the history into the same release ranges.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

TAG_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-(rc|pre)(\d+))?$")
NEXT_TAG_RE = re.compile(r"^next-(\d{4})(\d{2})(\d{2})$")


def parse_tag(tag: str) -> dict[str, Any] | None:
    """`v3.1-rc2` -> {series: (3, 1, 0), stable: 0, pre: 2, kind: "mainline"}; None for other names.

    Series: v2.6.x releases are (2, 6, x) with the fourth number as the stable update; from v3.0 on a
    release is (X, Y, 0) with the third number as the stable update. Next tags are `next-YYYYMMDD`."""
    m_next = NEXT_TAG_RE.match(str(tag or ""))
    if m_next:
        year, month, day = int(m_next.group(1)), int(m_next.group(2)), int(m_next.group(3))
        return {
            "series": (999, year, month),
            "stable": day,
            "pre": 0,
            "pre_kind": "",
            "kind": "next",
            "final": True,
        }
    m = TAG_RE.match(str(tag or ""))
    if not m:
        return None
    a, b, c, d, pre_kind, pre_num = m.groups()
    a, b = int(a), int(b)
    if a == 2 and b == 6:
        if c is None:
            return None
        series = (2, 6, int(c))
        stable = int(d) if d is not None else 0
    elif a == 2 and b < 6:
        series = (a, b, 0)
        stable = int(c) if c is not None else 0
    else:
        if d is not None:
            return None
        series = (a, b, 0)
        stable = int(c) if c is not None else 0
    pre = int(pre_num) if pre_num is not None else 0
    if pre and stable:
        return None  # a stable update has no release candidates
    return {"series": series, "stable": stable, "pre": pre, "pre_kind": pre_kind or "",
            "kind": "stable" if stable else "mainline", "final": not pre and not stable}


def release_key(tag: str) -> tuple:
    """Sort key: release candidates before their release, the release before its stable updates.

    v3.0 < v3.0.1 < v3.0.2 < v3.1-rc1 < v3.1-rc2 < v3.1 < v3.1.1; names that are not releases sort
    first, by their dotted numbers then by name."""
    p = parse_tag(tag)
    if p is None:
        nums = tuple(int(n) for n in re.findall(r"\d+", str(tag or ""))[:6])
        return (0, nums, str(tag or ""))
    # (release series, 0 for -rc/-pre, 1 for the release and its updates, rc number, stable update)
    return (1, p["series"], 0 if p["pre"] else 1, p["pre"], p["stable"])


def series_name(series: tuple[int, int, int]) -> str:
    if series[0] == 999:
        return f"next-{series[1]:04d}{series[2]:02d}"
    return f"v{series[0]}.{series[1]}.{series[2]}" if series[:2] == (2, 6) else f"v{series[0]}.{series[1]}"


def previous_in(tag: str, tags: Iterable[str]) -> str | None:
    """The release `tag` follows, among `tags`:

    v3.1 -> v3.0; v3.1-rc1 -> v3.0; v3.1-rc2 -> v3.1-rc1; v3.0.5 -> v3.0.4; v3.0.1 -> v3.0;
    v2.6.39 -> v2.6.38. (`release_ranges()` answers this for every tag at once.)"""
    p = parse_tag(tag)
    if p is None:
        return None
    parsed = [(t, parse_tag(t)) for t in tags]
    parsed = [(t, q) for t, q in parsed if q is not None]
    key = release_key(tag)
    if p["kind"] == "next":
        same_next = [t for t, q in parsed if q["kind"] == "next" and release_key(t) < key]
        if same_next:
            return max(same_next, key=release_key)
        finals = [t for t, q in parsed if q["final"] and release_key(t) < key and q["series"] < p["series"]]
        return max(finals, key=release_key) if finals else None
    if p["stable"]:
        same = [t for t, q in parsed if q["series"] == p["series"] and not q["pre"] and q["stable"] < p["stable"]]
        return max(same, key=release_key) if same else None
    if p["pre"] > 1:
        earlier = [t for t, q in parsed if q["series"] == p["series"] and 0 < q["pre"] < p["pre"]]
        if earlier:
            return max(earlier, key=release_key)
    # A release or its first release candidate follows the last earlier release
    finals = [t for t, q in parsed if q["final"] and release_key(t) < key and q["series"] < p["series"]]
    return max(finals, key=release_key) if finals else None


def final_of(tag: str, tags: set[str]) -> str | None:
    """The release a release candidate leads to (v3.1-rc6 -> v3.1), when it is tagged."""
    p = parse_tag(tag)
    if p is None:
        return None
    if p["kind"] == "next" or p["stable"] or not p["pre"]:
        return tag
    name = series_name(p["series"])
    return name if name in tags else None


def release_ranges(tags: Iterable[str]) -> list[dict[str, Any]]:
    """Every release tag of `tags` in `release_key` order with the two ranges that end at it:

    - `segment_base`: the tag before it on its chain — the previous mainline tag (release candidates
      included) for a mainline tag, `previous_in()` for a stable update. The segments of a chain are
      disjoint and add up to any range of that chain (v3.0..v3.1 = v3.0..v3.1-rc1 + … + v3.1-rc8..v3.1),
      the cut the webapp's release index uses.
    - `range_base`: `previous_in()` — the range a release report covers (a release: its whole cycle
      from the previous release; otherwise its segment).

    {tag, kind: "mainline" | "stable", final, segment_base, range_base}; bases are None for the first
    tag of a chain. Linear in the number of tags (`previous_in()` per tag would be quadratic)."""
    parsed = sorted(((t, p) for t in set(tags) if (p := parse_tag(t)) is not None), key=lambda tp: release_key(tp[0]))
    out: list[dict[str, Any]] = []
    prev_mainline: str | None = None
    prev_next: str | None = None
    last_final: str | None = None              # the newest final release seen so far (key order)
    last_pre: dict[tuple, str] = {}            # series -> newest release candidate seen
    last_in_series: dict[tuple, str] = {}      # series -> newest final or stable update seen
    for tag, p in parsed:
        series = p["series"]
        if p["kind"] == "next":
            base = prev_next or last_final
            out.append({"tag": tag, "kind": "next", "final": True, "segment_base": base, "range_base": base})
            prev_next = tag
            continue
        if p["kind"] == "stable":
            base = last_in_series.get(series)
            out.append({"tag": tag, "kind": "stable", "final": False, "segment_base": base, "range_base": base})
            last_in_series[series] = tag
            continue
        if p["pre"] > 1 and series in last_pre:
            range_base = last_pre[series]
        else:
            # The last final release of an earlier series (key order puts it before this one)
            range_base = last_final if last_final is not None and parse_tag(last_final)["series"] < series else None
        out.append({"tag": tag, "kind": "mainline", "final": p["final"], "segment_base": prev_mainline,
                    "range_base": range_base})
        prev_mainline = tag
        if p["pre"]:
            last_pre[series] = tag
        else:
            last_final = tag
            last_in_series[series] = tag
    return out
