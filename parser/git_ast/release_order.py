"""parser/git_ast/release_order.py - Release tag names and their order (pure, no I/O).

Two tag schemes, each its own family of chains:

- The kernel scheme (family `""`): `v3.1-rc2`, `v2.6.39.4`, the historical `2.4.0`, and `next-YYYYMMDD`.
- Prefixed releases (family = the prefix): `<prefix>X.Y[.Z][-rcN|-preN]`, e.g. `man-pages-6.9.1`, `foo-1.2.3`,
  `release_2.0-rc1`; X.Y is the series and Z its point release, compared as numbers (`man-pages-6.04` and
  `man-pages-6.4` name the same series). Their series is `(PREFIXED_SERIES, prefix, X, Y)`, so it never compares
  equal to a kernel series and series of different prefixes order by prefix.

`release_tag_names()` picks the release tags of a repository: the kernel scheme's when it has any, else every
prefixed one. Shared by the parser's release statistics step (parser/git_ast/release_stats.py, core/API.md
section 10) and the webapp's release index (webapp/backend/services/release_service.py, webapp/API.md section
2.17), so both cut the history into the same release ranges.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

TAG_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-(rc|pre)(\d+))?$")
NEXT_TAG_RE = re.compile(r"^next-(\d{4})(\d{2})(\d{2})$")
# <prefix>X.Y[.Z][-rcN|-preN]: the prefix starts with a letter and holds no dot
PREFIXED_TAG_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_+-]*?)(\d+)\.(\d+)(?:\.(\d+))?(?:-(rc|pre)(\d+))?$")
# The family of the kernel scheme (vX.Y, X.Y, next-YYYYMMDD)
KERNEL_FAMILY = ""
# First element of a prefixed series: (PREFIXED_SERIES, prefix, X, Y)
PREFIXED_SERIES = 1000
NEXT_SERIES = 999


def parse_tag(tag: str) -> dict[str, Any] | None:
    """`v3.1-rc2` -> {series: (3, 1, 0), stable: 0, pre: 2, kind: "mainline", family: ""}; None for other names.

    Kernel scheme: v2.6.x releases are (2, 6, x) with the fourth number as the stable update; from v3.0 on a
    release is (X, Y, 0) with the third number as the stable update. Next tags are `next-YYYYMMDD`.
    Prefixed: `<prefix>X.Y[.Z][-rcN]` is series (PREFIXED_SERIES, prefix, X, Y) with Z as the point release
    (kind "stable"), family `prefix`. A release candidate of a point release is None."""
    name = str(tag or "")
    m_next = NEXT_TAG_RE.match(name)
    if m_next:
        year, month, day = int(m_next.group(1)), int(m_next.group(2)), int(m_next.group(3))
        return {
            "series": (NEXT_SERIES, year, month),
            "stable": day,
            "pre": 0,
            "pre_kind": "",
            "kind": "next",
            "final": True,
            "family": KERNEL_FAMILY,
        }
    m = TAG_RE.match(name)
    if m:
        return _parse_kernel(m)
    m_pre = PREFIXED_TAG_RE.match(name)
    if not m_pre or m_pre.group(1) in ("v", "V"):
        return None
    prefix, major, minor, point, pre_kind, pre_num = m_pre.groups()
    stable = int(point) if point is not None else 0
    pre = int(pre_num) if pre_num is not None else 0
    if pre and stable:
        return None
    return {"series": (PREFIXED_SERIES, prefix, int(major), int(minor)), "stable": stable, "pre": pre,
            "pre_kind": pre_kind or "", "kind": "stable" if stable else "mainline",
            "final": not pre and not stable, "family": prefix}


def _parse_kernel(m: re.Match) -> dict[str, Any] | None:
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
            "kind": "stable" if stable else "mainline", "final": not pre and not stable, "family": KERNEL_FAMILY}


def family_of(tag: str) -> str | None:
    """The tag family of a release tag (`""` for the kernel scheme, else its prefix); None for other names."""
    p = parse_tag(tag)
    return None if p is None else p["family"]


def release_tag_names(names: Iterable[str]) -> list[str]:
    """The release tags among `names` (order kept): the kernel scheme's when any name uses it (prefixed names in
    a kernel tree, such as pull-request tags `for-linus-6.1`, are not releases), else every prefixed release."""
    parsed = [(n, p) for n in names if (p := parse_tag(n)) is not None]
    if any(p["family"] == KERNEL_FAMILY for _, p in parsed):
        return [n for n, p in parsed if p["family"] == KERNEL_FAMILY]
    return [n for n, _ in parsed]


def release_key(tag: str) -> tuple:
    """Sort key: release candidates before their release, the release before its stable updates.

    v3.0 < v3.0.1 < v3.0.2 < v3.1-rc1 < v3.1-rc2 < v3.1 < v3.1.1; foo-1.9 < foo-1.10-rc1 < foo-1.10 < foo-1.10.1;
    kernel tags before prefixed ones; names that are not releases sort first, by their dotted numbers then by
    name."""
    p = parse_tag(tag)
    if p is None:
        nums = tuple(int(n) for n in re.findall(r"\d+", str(tag or ""))[:6])
        return (0, nums, str(tag or ""))
    # (release series, 0 for -rc/-pre, 1 for the release and its updates, rc number, stable update)
    return (1, p["series"], 0 if p["pre"] else 1, p["pre"], p["stable"])


def series_name(series: tuple) -> str:
    """A name of the release opening `series`: `v3.1`, `v2.6.39`, `next-YYYYMM`, `<prefix>X.Y` (minor without
    leading zeros: `man-pages-6.4` for the series of `man-pages-6.04`; it parses back to the same series)."""
    if series[0] == PREFIXED_SERIES:
        return f"{series[1]}{series[2]}.{series[3]}"
    if series[0] == NEXT_SERIES:
        return f"next-{series[1]:04d}{series[2]:02d}"
    return f"v{series[0]}.{series[1]}.{series[2]}" if series[:2] == (2, 6) else f"v{series[0]}.{series[1]}"


def previous_in(tag: str, tags: Iterable[str]) -> str | None:
    """The release `tag` follows, among the `tags` of its family:

    v3.1 -> v3.0; v3.1-rc1 -> v3.0; v3.1-rc2 -> v3.1-rc1; v3.0.5 -> v3.0.4; v3.0.1 -> v3.0;
    v2.6.39 -> v2.6.38; foo-1.3 -> foo-1.2. (`release_ranges()` answers this for every tag at once.)"""
    p = parse_tag(tag)
    if p is None:
        return None
    parsed = [(t, parse_tag(t)) for t in tags]
    parsed = [(t, q) for t, q in parsed if q is not None and q["family"] == p["family"]]
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
    """The release a release candidate leads to (v3.1-rc6 -> v3.1), when it is tagged. A prefixed candidate leads
    to the final tag of its series whatever its spelling (`man-pages-6.0-rc1` -> `man-pages-6.00`)."""
    p = parse_tag(tag)
    if p is None:
        return None
    if p["kind"] == "next" or p["stable"] or not p["pre"]:
        return tag
    if p["family"] != KERNEL_FAMILY:
        hits = [t for t in tags if (q := parse_tag(t)) is not None and q["final"] and q["series"] == p["series"]]
        return min(hits) if hits else None
    name = series_name(p["series"])
    return name if name in tags else None


def release_ranges(tags: Iterable[str]) -> list[dict[str, Any]]:
    """Every release tag of `tags` in `release_key` order with the two ranges that end at it:

    - `segment_base`: the tag before it on its chain — the previous mainline tag of its family (release
      candidates included) for a mainline tag, `previous_in()` for a stable update. The segments of a chain are
      disjoint and add up to any range of that chain (v3.0..v3.1 = v3.0..v3.1-rc1 + … + v3.1-rc8..v3.1),
      the cut the webapp's release index uses.
    - `range_base`: `previous_in()` — the range a release report covers (a release: its whole cycle
      from the previous release; otherwise its segment).

    {tag, kind: "mainline" | "stable" | "next", final, segment_base, range_base}; bases are None for the first
    tag of a chain. Each family (`parse_tag()["family"]`) has chains of its own. Linear in the number of tags
    (`previous_in()` per tag would be quadratic)."""
    parsed = sorted(((t, p) for t in set(tags) if (p := parse_tag(t)) is not None), key=lambda tp: release_key(tp[0]))
    out: list[dict[str, Any]] = []
    prev_mainline: dict[str, str] = {}         # family -> newest mainline tag seen
    prev_next: str | None = None
    last_final: dict[str, str] = {}            # family -> newest final release seen so far (key order)
    last_pre: dict[tuple, str] = {}            # series -> newest release candidate seen
    last_in_series: dict[tuple, str] = {}      # series -> newest final or stable update seen
    for tag, p in parsed:
        series, fam = p["series"], p["family"]
        if p["kind"] == "next":
            base = prev_next or last_final.get(fam)
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
            # The last final release of an earlier series of the family (key order puts it before this one)
            final = last_final.get(fam)
            range_base = final if final is not None and parse_tag(final)["series"] < series else None
        out.append({"tag": tag, "kind": "mainline", "final": p["final"], "segment_base": prev_mainline.get(fam),
                    "range_base": range_base})
        prev_mainline[fam] = tag
        if p["pre"]:
            last_pre[series] = tag
        else:
            last_final[fam] = tag
            last_in_series[series] = tag
    return out
