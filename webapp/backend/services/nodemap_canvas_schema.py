"""webapp/backend/services/nodemap_canvas_schema.py - Canonical NodeMap canvas schema and normalization.

Mirrors `webapp/frontend/js/views/nodemap/canvas_model.js` (`schema: "kip-nodemap-canvas"`, `version: 1`)
across all 16 `NODE_TYPES`, `SIZE_LIMITS`, `LOCK_KINDS`, `EDGE_PRESETS`, `default_title()`, `viewport`,
`settings`, `layers`, `tours`, sub-canvases (`depth < 8`), dangling/self-loop edge pruning, and
`groupId` cycle pruning.
"""
from __future__ import annotations

import copy
import json
import math
import random
import re
import time
from collections.abc import Iterable
from typing import Any

CANVAS_SCHEMA = "kip-nodemap-canvas"
CANVAS_VERSION = 1
STORAGE_PREFIX = "kip-nodemap-canvas:"
USER_CANVAS_KIND = "nodemap-canvas"

NODE_TYPES: list[str] = [
    "struct",
    "symbol",
    "source",
    "man",
    "tab",
    "note",
    "group",
    "diff",
    "mail",
    "kconfig",
    "maintainer",
    "cve",
    "kapi",
    "doc",
    "history",
    "commit",
]

NODE_BADGES: dict[str, str] = {
    "struct": "struct",
    "symbol": "symbol",
    "source": "source",
    "man": "man",
    "tab": "tab",
    "note": "note",
    "group": "group",
    "diff": "diff",
    "mail": "mail",
    "kconfig": "kconfig",
    "maintainer": "maint",
    "cve": "cve",
    "kapi": "kapi",
    "doc": "doc",
    "history": "hist",
    "commit": "commit",
}

SIDES: list[str] = ["auto", "top", "right", "bottom", "left"]
ARROWS: list[str] = ["none", "forward", "backward", "both"]
LINE_STYLES: list[str] = ["solid", "dashed", "dotted"]
CURVES: list[str] = ["bezier", "straight", "orthogonal"]
SOURCE_MODES: list[str] = ["file", "range", "symbol"]
SYMBOL_KINDS: list[str] = ["", "struct", "union", "function", "macro", "enum"]
DIFF_MODES: list[str] = ["file", "commit"]
KAPI_CATALOGS: list[str] = ["summary", "syscalls", "exports", "params", "tracepoints", "modules", "devices"]
DOC_MODES: list[str] = ["auto", "rst", "kernel-doc"]
HISTORY_MODES: list[str] = ["symbol", "blame"]
ALIGN_MODES: list[str] = ["left", "center", "right", "top", "middle", "bottom"]
STRUCT_STYLES: list[str] = ["list", "table", "layout"]
LOCK_KINDS: list[str] = ["spinlock", "mutex", "rwsem", "rcu", "atomic", "seqlock", "lock-order"]

AUTOFIT_TYPES: list[str] = [
    "struct",
    "symbol",
    "note",
    "source",
    "man",
    "diff",
    "mail",
    "kconfig",
    "maintainer",
    "cve",
    "kapi",
    "doc",
    "history",
    "commit",
]

TAB_OPENABLE_TYPES: list[str] = [
    "struct",
    "source",
    "man",
    "tab",
    "symbol",
    "diff",
    "mail",
    "kconfig",
    "maintainer",
    "cve",
    "kapi",
    "doc",
    "history",
    "commit",
]

SCALE_MIN = 0.2
SCALE_MAX = 4.0
AUTOFIT_MAX: dict[str, int] = {"w": 4000, "h": 8000}

EDGE_PRESETS: list[dict[str, str]] = [
    {"id": "", "label": "Custom"},
    {"id": "calls", "label": "calls", "arrow": "forward", "style": "solid", "color": "blue"},
    {"id": "points to", "label": "points to", "arrow": "forward", "style": "solid", "color": "cyan"},
    {"id": "embeds", "label": "embeds", "arrow": "forward", "style": "dashed", "color": "purple"},
    {"id": "locks", "label": "locks / RCU", "arrow": "forward", "style": "dashed", "color": "orange"},
    {"id": "depends on", "label": "depends on", "arrow": "forward", "style": "solid", "color": "yellow"},
    {"id": "fixes", "label": "fixes", "arrow": "forward", "style": "solid", "color": "green"},
    {"id": "replies to", "label": "replies to", "arrow": "forward", "style": "solid", "color": "pink"},
]

ACCENTS: list[dict[str, str]] = [
    {"id": "", "label": "Default"},
    {"id": "blue", "label": "Blue"},
    {"id": "cyan", "label": "Cyan"},
    {"id": "green", "label": "Green"},
    {"id": "purple", "label": "Purple"},
    {"id": "orange", "label": "Orange"},
    {"id": "red", "label": "Red"},
    {"id": "yellow", "label": "Yellow"},
    {"id": "pink", "label": "Pink"},
]
ACCENT_IDS: frozenset[str] = frozenset(a["id"] for a in ACCENTS)
HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$", re.IGNORECASE)

SIZE_LIMITS: dict[str, dict[str, int]] = {
    "struct": {"w": 300, "h": 320, "minW": 180, "minH": 90, "maxW": 1400, "maxH": 2400},
    "symbol": {"w": 220, "h": 120, "minW": 140, "minH": 60, "maxW": 900, "maxH": 1600},
    "source": {"w": 560, "h": 360, "minW": 220, "minH": 120, "maxW": 2400, "maxH": 2400},
    "man": {"w": 560, "h": 420, "minW": 260, "minH": 160, "maxW": 2400, "maxH": 2400},
    "tab": {"w": 720, "h": 480, "minW": 280, "minH": 200, "maxW": 3000, "maxH": 2400},
    "note": {"w": 240, "h": 140, "minW": 120, "minH": 60, "maxW": 1600, "maxH": 1600},
    "group": {"w": 520, "h": 360, "minW": 160, "minH": 100, "maxW": 6000, "maxH": 6000},
    "diff": {"w": 620, "h": 420, "minW": 260, "minH": 120, "maxW": 2400, "maxH": 2400},
    "mail": {"w": 480, "h": 360, "minW": 240, "minH": 120, "maxW": 1800, "maxH": 2400},
    "kconfig": {"w": 340, "h": 300, "minW": 200, "minH": 100, "maxW": 1400, "maxH": 2000},
    "maintainer": {"w": 380, "h": 320, "minW": 220, "minH": 100, "maxW": 1400, "maxH": 2000},
    "cve": {"w": 420, "h": 360, "minW": 240, "minH": 140, "maxW": 1600, "maxH": 2400},
    "kapi": {"w": 400, "h": 340, "minW": 240, "minH": 120, "maxW": 1600, "maxH": 2400},
    "doc": {"w": 480, "h": 380, "minW": 260, "minH": 140, "maxW": 2000, "maxH": 2400},
    "history": {"w": 440, "h": 340, "minW": 240, "minH": 140, "maxW": 1800, "maxH": 2400},
    "commit": {"w": 460, "h": 420, "minW": 260, "minH": 140, "maxW": 1800, "maxH": 2400},
}

GRID_DEFAULT = 20
ZOOM_MIN = 0.15
ZOOM_MAX = 3.0
RENDER_QUALITIES: list[str] = ["auto", "1", "1.5", "2"]
SUPERSAMPLE_MAX = 2
DEFAULT_EMBED_SESSION: dict[str, str] = {"id": "nms-1", "name": "Canvas Session 1"}
EMBED_SESSIONS_MAX = 16
MAIL_BODY_MAX = 200_000
MAIL_PIN_MAX = 12
GROUP_LAYOUT_PAD: dict[str, int] = {"x": 24, "top": 44, "bottom": 24}

MAX_SUBCANVAS_DEPTH = 8
MAX_NODES = 5000
MAX_EDGES = 20000
MAX_LAYERS = 64
MAX_TOURS = 32
MAX_TOUR_STEPS = 200
MAX_STEP_TARGETS = 500
MAX_NODE_LOCKS = 32
MAX_FIELD_LOCKS = 512
MAX_WAYPOINTS = 64
MAX_CONSTITUENTS = 2000

ID_RE = re.compile(r"^[\w-]{1,64}$")
_COMMIT_HASH_RE = re.compile(r"^[0-9a-f]{4,64}$", re.IGNORECASE)
_EMBED_VIEWSTATE_MAX = 32768
_VS_SCROLL_MAX = 24
_VS_FIELDS_MAX = 48
_VS_FIELD_RE = re.compile(r"^[A-Za-z_$][\w$]{0,63}$")
_VS_SKIP_FIELDS = frozenset({"id", "type", "repo", "version", "title", "path", "scrollPositions", "forceNew", "pinned"})
_VS_SKIP_PREFIX = re.compile(r"^(initial|pending|transfer|__)")
_VS_RESERVED = frozenset({"__proto__", "constructor", "prototype", "toString", "valueOf", "hasOwnProperty", "toJSON"})

_id_counter = 0


def _base36(num: int) -> str:
    if num <= 0:
        return "0"
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    chars: list[str] = []
    while num:
        num, rem = divmod(num, 36)
        chars.append(alphabet[rem])
    return "".join(reversed(chars))


def new_id(prefix: str = "n") -> str:
    """Generate a fresh NodeMap identifier matching `ID_RE` (`n-...`, `e-...`, etc.)."""
    global _id_counter
    _id_counter = (_id_counter + 1) % 1_000_000
    ts = _base36(int(time.time() * 1000))
    cnt = _base36(_id_counter)
    rand = "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyz", k=4))
    candidate = f"{prefix}-{ts}{cnt}{rand}"
    return candidate[:64]


def is_valid_id(val: Any) -> bool:
    return isinstance(val, str) and bool(ID_RE.match(val))


def scalable(node_type: str) -> bool:
    return node_type != "group"


def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


def _num(v: Any, default: float | int) -> float | int:
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)) and math.isfinite(v):
        return v
    return default


def _js_round(v: float | int) -> int:
    """Round half toward +Infinity like JavaScript `Math.round()`."""
    return int(math.floor(float(v) + 0.5))


def _str(v: Any, default: str = "", max_len: int = 4096) -> str:
    return v[:max_len] if isinstance(v, str) else default


def _pick(v: Any, allowed: Iterable[str], default: str) -> str:
    return v if isinstance(v, str) and v in allowed else default


def _clamp(v: float | int, lo: float | int, hi: float | int) -> float | int:
    return min(hi, max(lo, v))


def _pos_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and v > 0:
        return v
    return None


def _bool_map(m: Any) -> dict[str, bool]:
    out: dict[str, bool] = {}
    if not _is_obj(m):
        return out
    for k, v in m.items():
        if isinstance(k, str) and len(k) <= 256 and isinstance(v, bool):
            out[k] = v
    return out


def _true_map(m: Any) -> dict[str, bool]:
    return {k: True for k, v in _bool_map(m).items() if v}


def is_custom_color(c: Any) -> bool:
    return isinstance(c, str) and bool(HEX_COLOR_RE.match(c))


def normalize_color(c: Any) -> str:
    if is_custom_color(c):
        return str(c).lower()
    return c if isinstance(c, str) and c in ACCENT_IDS else ""


def normalize_field_locks(m: Any) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not _is_obj(m):
        return out
    for k, v in list(m.items())[:MAX_FIELD_LOCKS]:
        if not isinstance(k, str) or not k or len(k) > 256:
            continue
        if isinstance(v, str) and v in LOCK_KINDS:
            out[k] = {"kind": v, "lockName": ""}
        elif _is_obj(v) and isinstance(v.get("kind"), str) and v["kind"] in LOCK_KINDS:
            out[k] = {"kind": v["kind"], "lockName": _str(v.get("lockName"), "", 128).strip()}
    return out


def normalize_node_locks(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for x in raw[:MAX_NODE_LOCKS]:
        if not _is_obj(x) or not isinstance(x.get("kind"), str) or x["kind"] not in LOCK_KINDS:
            continue
        out.append(
            {
                "kind": x["kind"],
                "lockName": _str(x.get("lockName"), "", 128).strip(),
                "note": _str(x.get("note"), "", 256).strip(),
            }
        )
    return out


def _pinned_mail_bodies(m: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not _is_obj(m):
        return out
    total = 0
    n = 0
    for k, v in m.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k or len(k) > 512 or n >= MAIL_PIN_MAX:
            continue
        body = v[:MAIL_BODY_MAX]
        if total + len(body) > MAIL_BODY_MAX * 2:
            continue
        out[k] = body
        total += len(body)
        n += 1
    return out


def _vs_primitive(v: Any, max_len: int) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and math.isfinite(v):
        return v
    if isinstance(v, str) and len(v) <= max_len:
        return v
    return None


def normalize_embed_view_state(raw: Any) -> dict[str, Any] | None:
    if not _is_obj(raw):
        return None
    out: dict[str, Any] = {}
    raw_scroll = raw.get("scroll")
    if _is_obj(raw_scroll):
        scroll: dict[str, list[int]] = {}
        n = 0
        for k, v in raw_scroll.items():
            if n >= _VS_SCROLL_MAX:
                break
            if not isinstance(k, str) or len(k) > 512 or not isinstance(v, list) or len(v) != 2:
                continue
            try:
                top = _js_round(float(v[0]))
                left = _js_round(float(v[1]))
            except (TypeError, ValueError):
                continue
            if not (0 <= top <= 10_000_000 and 0 <= left <= 10_000_000) or (top == 0 and left == 0):
                continue
            scroll[k] = [top, left]
            n += 1
        if n:
            out["scroll"] = scroll

    raw_fields = raw.get("fields")
    if _is_obj(raw_fields):
        fields: dict[str, Any] = {}
        n = 0
        for k, v in raw_fields.items():
            if n >= _VS_FIELDS_MAX:
                break
            if (
                not isinstance(k, str)
                or not _VS_FIELD_RE.match(k)
                or k in _VS_SKIP_FIELDS
                or _VS_SKIP_PREFIX.match(k)
                or k in _VS_RESERVED
            ):
                continue
            if isinstance(v, list):
                if len(v) > 200:
                    continue
                vals = [_vs_primitive(x, 256) for x in v]
                if any(x is None for x in vals):
                    continue
                val: Any = vals
            else:
                val = _vs_primitive(v, 2000)
                if val is None:
                    continue
            fields[k] = val
            n += 1
        if n:
            out["fields"] = fields

    if "scroll" not in out and "fields" not in out:
        return None
    if len(json.dumps(out, separators=(",", ":"))) > _EMBED_VIEWSTATE_MAX:
        out.pop("fields", None)
        if "scroll" not in out or len(json.dumps(out, separators=(",", ":"))) > _EMBED_VIEWSTATE_MAX:
            return None
    return out


def normalize_data(node_type: str, raw: Any, depth: int = 0) -> dict[str, Any]:
    """Normalize type-specific `node.data` across all 16 NodeMap node types."""
    d = raw if _is_obj(raw) else {}
    if node_type == "struct":
        return {
            "name": _str(d.get("name"), "", 256),
            "kind": _pick(d.get("kind"), ("struct", "union"), "struct"),
            "path": _str(d.get("path"), "", 1024),
            "hint": _str(d.get("hint"), "", 1024),
            "line": _pos_int(d.get("line")),
            "showTypes": d["showTypes"] if isinstance(d.get("showTypes"), bool) else True,
            "showGuards": d["showGuards"] if isinstance(d.get("showGuards"), bool) else True,
            "typeOverrides": _bool_map(d.get("typeOverrides")),
            "collapsed": _bool_map(d.get("collapsed")),
            "style": _pick(d.get("style"), STRUCT_STYLES, "list"),
            "collapsedGuards": _bool_map(d.get("collapsedGuards")),
            "hiddenItems": _true_map(d.get("hiddenItems")),
            "fieldLocks": normalize_field_locks(d.get("fieldLocks")),
            "disableKconfigEngine": bool(d.get("disableKconfigEngine")),
        }
    if node_type == "symbol":
        raw_const = d.get("constituents")
        constituents: list[dict[str, str]] = []
        if isinstance(raw_const, list):
            for c in raw_const[:MAX_CONSTITUENTS]:
                cname = _str(c.get("name") if _is_obj(c) else c, "", 512)
                if cname:
                    constituents.append({"name": cname})
        ast_id = d.get("astId")
        return {
            "name": _str(d.get("name"), "", 256),
            "kind": _str(d.get("kind"), "symbol", 32),
            "astId": ast_id if isinstance(ast_id, int) and not isinstance(ast_id, bool) else None,
            "expanded": bool(d.get("expanded")),
            "constituents": constituents,
        }
    if node_type == "source":
        start = _pos_int(d.get("start"))
        end = _pos_int(d.get("end"))
        ctx = d.get("context")
        ctx_int = ctx if isinstance(ctx, int) and not isinstance(ctx, bool) else 0
        return {
            "path": _str(d.get("path"), "", 1024),
            "mode": _pick(d.get("mode"), SOURCE_MODES, "file"),
            "start": start,
            "end": start if (start and end and end < start) else end,
            "symbol": _str(d.get("symbol"), "", 256),
            "symbolKind": _pick(d.get("symbolKind"), SYMBOL_KINDS, ""),
            "context": int(_clamp(ctx_int, 0, 200)),
        }
    if node_type == "man":
        return {
            "repo": _str(d.get("repo"), "", 128),
            "version": _str(d.get("version"), "", 256),
            "path": _str(d.get("path"), "", 1024),
            "name": _str(d.get("name"), "", 128),
            "section": _str(d.get("section"), "", 16),
            "mode": _pick(d.get("mode"), ("web", "term", ""), ""),
        }
    if node_type == "tab":
        embed_sid = d.get("embedSessionId")
        return {
            "tabType": _str(d.get("tabType"), "", 32),
            "hash": _str(d.get("hash"), "", 4096).lstrip("#"),
            "embedSessionId": embed_sid if is_valid_id(embed_sid) else "",
            "frozen": d.get("frozen") is True,
            "viewState": normalize_embed_view_state(d.get("viewState")),
        }
    if node_type == "diff":
        raw_ch = str(d.get("commitHash") or "")
        commit_hash = raw_ch.lower() if _COMMIT_HASH_RE.match(raw_ch) else ""
        mode = _pick(d.get("mode"), DIFF_MODES, "commit" if d.get("commitHash") else "file")
        return {
            "mode": mode,
            "versionA": _str(d.get("versionA"), "", 256),
            "versionB": _str(d.get("versionB"), "", 256),
            "path": _str(d.get("path"), "", 1024),
            "oldPath": _str(d.get("oldPath"), "", 1024),
            "commitHash": commit_hash,
        }
    if node_type == "mail":
        mid_raw = _str(d.get("mid"), "", 512)
        mid_clean = re.sub(r"^<|>$", "", mid_raw)
        single = bool(d.get("single")) and bool(mid_raw)
        tid = d.get("tid")
        return {
            "tid": tid if isinstance(tid, int) and not isinstance(tid, bool) and tid > 0 else None,
            "mid": mid_clean,
            "subject": _str(d.get("subject"), "", 512),
            "openMids": _true_map(d.get("openMids")),
            "fullMids": _true_map(d.get("fullMids")),
            "pinnedBodies": {} if single else _pinned_mail_bodies(d.get("pinnedBodies")),
            "single": single,
            "fullBody": _str(d.get("fullBody"), "", MAIL_BODY_MAX) if single else "",
        }
    if node_type == "kconfig":
        sym = _str(d.get("symbol"), "", 256)
        if sym.startswith("CONFIG_"):
            sym = sym[len("CONFIG_") :]
        return {
            "symbol": sym,
            "arch": _str(d.get("arch"), "", 64) or "x86_64",
        }
    if node_type == "maintainer":
        return {
            "secId": _pos_int(d.get("secId")),
            "title": _str(d.get("title"), "", 512),
            "query": _str(d.get("query"), "", 256),
            "path": _str(d.get("path"), "", 1024),
        }
    if node_type == "cve":
        return {
            "cveId": _str(d.get("cveId"), "", 64).strip().upper(),
            "release": _str(d.get("release"), "", 128).strip(),
            "filePath": _str(d.get("filePath"), "", 1024).strip(),
        }
    if node_type == "kapi":
        return {
            "catalog": _pick(d.get("catalog"), KAPI_CATALOGS, "summary"),
            "query": _str(d.get("query"), "", 256).strip(),
            "symbol": _str(d.get("symbol"), "", 256).strip(),
            "arch": _str(d.get("arch"), "", 64).strip() or "x86_64",
        }
    if node_type == "doc":
        return {
            "path": _str(d.get("path"), "", 1024).strip(),
            "mode": _pick(d.get("mode"), DOC_MODES, "auto"),
            "functions": _str(d.get("functions"), "", 256).strip(),
        }
    if node_type == "history":
        ast_id = d.get("astId")
        return {
            "mode": _pick(d.get("mode"), HISTORY_MODES, "symbol"),
            "path": _str(d.get("path"), "", 1024).strip(),
            "line": _pos_int(d.get("line")) or 1,
            "name": _str(d.get("name"), "", 256).strip(),
            "astId": ast_id if isinstance(ast_id, int) and not isinstance(ast_id, bool) else None,
        }
    if node_type == "commit":
        raw_ch = str(d.get("commitHash") or "")
        return {
            "commitHash": raw_ch.lower() if _COMMIT_HASH_RE.match(raw_ch) else "",
            "subject": _str(d.get("subject"), "", 512).strip(),
        }
    if node_type == "note":
        return {
            "text": _str(d.get("text"), "", 20000),
            "markdown": d.get("markdown") is not False,
        }
    if node_type == "group":
        if depth < MAX_SUBCANVAS_DEPTH and _is_obj(d.get("subCanvas")):
            return {"subCanvas": normalize_canvas(d["subCanvas"], depth + 1)}
        return {}
    return {}


def default_title(node_type: str, data: dict[str, Any]) -> str:
    """Default title of a node from its normalized payload (`canvas_model.js:defaultTitle`)."""
    d = data or {}
    if node_type == "struct":
        return f"{d.get('kind', 'struct')} {d['name']}" if d.get("name") else "struct"
    if node_type == "symbol":
        name = d.get("name") or ""
        if not name:
            return "Symbol"
        return f"{name}()" if d.get("kind") == "func" and not name.endswith("()") else name
    if node_type == "source":
        path = d.get("path") or ""
        file_name = path.split("/")[-1] if path else "Source"
        if d.get("mode") == "symbol" and d.get("symbol"):
            return f"{file_name} · {d['symbol']}"
        if d.get("mode") == "range" and d.get("start"):
            start = d["start"]
            end = d.get("end")
            suffix = f"-{end}" if end and end != start else ""
            return f"{file_name}:{start}{suffix}"
        return file_name
    if node_type == "man":
        if d.get("name"):
            return f"{d['name']}({d.get('section') or '?'})"
        path = d.get("path") or ""
        return path.split("/")[-1] if path else "Man page"
    if node_type == "tab":
        return f"{d['tabType']} tab" if d.get("tabType") else "KernelInfo tab"
    if node_type == "note":
        return "Note"
    if node_type == "group":
        return "Sub-Map" if d.get("subCanvas") else "Group"
    if node_type == "diff":
        path = d.get("path") or ""
        base = path.split("/")[-1] if path else ""
        if d.get("mode") == "commit":
            ch = d.get("commitHash") or ""
            if not ch:
                return "Commit diff"
            prefix = f"{base} @ " if base else ""
            return f"{prefix}{ch[:12]}"
        if path:
            return f"{base} · {d.get('versionA') or '?'}..{d.get('versionB') or '?'}"
        return "Diff"
    if node_type == "mail":
        if d.get("subject"):
            return str(d["subject"])
        if d.get("mid"):
            return f"<{d['mid']}>"
        if d.get("tid"):
            return f"Thread {d['tid']}"
        return "Mail thread"
    if node_type == "kconfig":
        return f"CONFIG_{d['symbol']}" if d.get("symbol") else "Kconfig symbol"
    if node_type == "maintainer":
        return str(d.get("title") or d.get("path") or d.get("query") or "Maintainers")
    if node_type == "cve":
        if d.get("cveId"):
            return str(d["cveId"])
        fp = d.get("filePath") or ""
        return f"CVEs · {fp.split('/')[-1]}" if fp else "CVE Record"
    if node_type == "kapi":
        if d.get("symbol"):
            return f"KAPI · {d['symbol']}"
        if d.get("query"):
            return f"KAPI · {d['query']}"
        return f"Kernel API ({d.get('catalog') or 'summary'})"
    if node_type == "doc":
        path = d.get("path") or ""
        return path.split("/")[-1] if path else "Kernel Docs"
    if node_type == "history":
        path = d.get("path") or ""
        base = path.split("/")[-1] if path else ""
        if d.get("mode") == "blame":
            return f"Blame · {base}" if path else "File Blame"
        if d.get("name"):
            return f"History · {d['name']}"
        if path:
            return f"History · {base}:{d.get('line') or 1}"
        return "Symbol History"
    if node_type == "commit":
        ch = d.get("commitHash") or ""
        if not ch:
            return "Commit"
        return f"{ch[:12]} {d['subject']}" if d.get("subject") else f"Commit {ch[:12]}"
    return "Node"


def display_title(node: dict[str, Any]) -> str:
    return str(node.get("customTitle") or node.get("title") or default_title(str(node.get("type") or ""), node.get("data") or {}))


def normalize_node(raw: Any, depth: int = 0) -> dict[str, Any] | None:
    """Normalize a single NodeMap node (`canvas_model.js:normalizeNode`)."""
    if not _is_obj(raw):
        return None
    node_type = raw.get("type")
    if not isinstance(node_type, str) or node_type not in NODE_TYPES:
        return None
    lim = SIZE_LIMITS[node_type]
    data = normalize_data(node_type, raw.get("data"), depth)
    auto_fit = node_type in AUTOFIT_TYPES and raw.get("autoFit") is True
    w = _js_round(_clamp(_num(raw.get("w"), lim["w"]), lim["minW"], lim["maxW"]))
    h = _js_round(_clamp(_num(raw.get("h"), lim["h"]), lim["minH"], lim["maxH"]))
    base_w = _js_round(_clamp(_num(raw.get("baseW"), w), 20, AUTOFIT_MAX["w"]))
    base_h = _js_round(_clamp(_num(raw.get("baseH"), h), 20, AUTOFIT_MAX["h"]))
    scale_raw = float(_clamp(_num(raw.get("scale"), 1), SCALE_MIN, SCALE_MAX)) if scalable(node_type) else 1.0
    if auto_fit:
        w = _js_round(base_w * scale_raw)
        h = _js_round(base_h * scale_raw)
    scale_val = _js_round(scale_raw * 1000) / 1000
    if scale_val == int(scale_val):
        scale_out: int | float = int(scale_val)
    else:
        scale_out = scale_val

    raw_id = raw.get("id")
    raw_gid = raw.get("groupId")
    raw_lid = raw.get("layerId")
    return {
        "id": raw_id if is_valid_id(raw_id) else new_id("n"),
        "type": node_type,
        "x": _js_round(_num(raw.get("x"), 80)),
        "y": _js_round(_num(raw.get("y"), 80)),
        "w": w,
        "h": h,
        "title": _str(raw.get("title"), "", 512) or default_title(node_type, data),
        "customTitle": _str(raw.get("customTitle"), "", 512),
        "color": normalize_color(raw.get("color")),
        "bgColor": normalize_color(raw.get("bgColor")),
        "hideHeader": raw.get("hideHeader") is True,
        "groupId": raw_gid if is_valid_id(raw_gid) else None,
        "layerId": raw_lid if is_valid_id(raw_lid) else "",
        "version": _str(raw.get("version"), "", 256),
        "repo": _str(raw.get("repo"), "", 128),
        "locks": normalize_node_locks(raw.get("locks")),
        "data": data,
        "autoFit": auto_fit,
        "baseW": base_w if auto_fit else w,
        "baseH": base_h if auto_fit else h,
        "scale": scale_out,
    }


def create_node(node_type: str, opts: dict[str, Any] | None = None) -> dict[str, Any] | None:
    payload = dict(opts or {})
    payload["type"] = node_type
    payload["id"] = payload.get("id") or new_id("n")
    return normalize_node(payload)


def _normalize_waypoints(raw: Any) -> list[dict[str, int]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, int]] = []
    for w in raw[:MAX_WAYPOINTS]:
        if not _is_obj(w):
            continue
        x = w.get("x")
        y = w.get("y")
        if isinstance(x, bool) or isinstance(y, bool):
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and math.isfinite(x) and math.isfinite(y):
            out.append({"x": _js_round(x), "y": _js_round(y)})
    return out


def _normalize_call_site(raw: Any) -> dict[str, Any] | None:
    if not _is_obj(raw):
        return None
    path = _str(raw.get("path"), "", 1024)
    if not path:
        return None
    line = _pos_int(raw.get("line")) or 1
    return {"path": path, "line": line}


def apply_edge_preset(edge: dict[str, Any], preset_id: str) -> bool:
    """Apply a semantic edge preset (`EDGE_PRESETS`) to an edge dictionary."""
    if not _is_obj(edge) or not preset_id:
        return False
    preset = next((p for p in EDGE_PRESETS if p["id"] == preset_id), None)
    if not preset or not preset["id"]:
        return False
    edge["label"] = preset["label"]
    edge["arrow"] = preset["arrow"]
    edge["style"] = preset["style"]
    edge["color"] = preset["color"]
    if preset_id == "locks" and not edge.get("lockKind"):
        edge["lockKind"] = "rcu"
    return True


def normalize_edge(raw: Any, node_ids: set[str] | None = None) -> dict[str, Any] | None:
    """Normalize an edge, rejecting self-loops and dangling references (`canvas_model.js:normalizeEdge`)."""
    if not _is_obj(raw):
        return None
    src = raw.get("from")
    dst = raw.get("to")
    if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst or src == dst:
        return None
    if node_ids is not None and (src not in node_ids or dst not in node_ids):
        return None
    raw_id = raw.get("id")
    raw_lid = raw.get("layerId")
    edge = {
        "id": raw_id if is_valid_id(raw_id) else new_id("e"),
        "from": src,
        "to": dst,
        "fromSide": _pick(raw.get("fromSide"), SIDES, "auto"),
        "toSide": _pick(raw.get("toSide"), SIDES, "auto"),
        "fromField": _str(raw.get("fromField"), "", 600) or None,
        "toField": _str(raw.get("toField"), "", 256) or None,
        "label": _str(raw.get("label"), "", 512),
        "arrow": _pick(raw.get("arrow"), ARROWS, "forward"),
        "style": _pick(raw.get("style"), LINE_STYLES, "solid"),
        "curve": _pick(raw.get("curve"), CURVES, "bezier"),
        "color": normalize_color(raw.get("color")),
        "waypoints": _normalize_waypoints(raw.get("waypoints")),
        "layerId": raw_lid if is_valid_id(raw_lid) else "",
        "lockKind": _pick(raw.get("lockKind"), ["", *LOCK_KINDS], ""),
        "callSite": _normalize_call_site(raw.get("callSite")),
    }
    preset_id = raw.get("preset")
    if isinstance(preset_id, str) and preset_id:
        preset = next((p for p in EDGE_PRESETS if p["id"] == preset_id), None)
        if preset and preset["id"]:
            if not raw.get("label"):
                edge["label"] = preset["label"]
            if "arrow" not in raw:
                edge["arrow"] = preset["arrow"]
            if "style" not in raw:
                edge["style"] = preset["style"]
            if not raw.get("color"):
                edge["color"] = preset["color"]
            if preset_id == "locks" and not edge["lockKind"]:
                edge["lockKind"] = "rcu"
    return edge


def create_edge(opts: dict[str, Any] | None = None, node_ids: set[str] | None = None) -> dict[str, Any] | None:
    payload = dict(opts or {})
    payload["id"] = payload.get("id") or new_id("e")
    return normalize_edge(payload, node_ids)


def normalize_layers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lyr in raw[:MAX_LAYERS]:
        if not _is_obj(lyr):
            continue
        lid = lyr.get("id")
        if not is_valid_id(lid) or lid in seen:
            continue
        seen.add(lid)
        out.append(
            {
                "id": lid,
                "name": _str(lyr.get("name"), "", 128).strip() or lid,
                "visible": lyr.get("visible") is not False,
                "color": normalize_color(lyr.get("color")) or "blue",
            }
        )
    return out


def normalize_embed_sessions(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for item in raw[:EMBED_SESSIONS_MAX]:
            if not _is_obj(item):
                continue
            sid = item.get("id")
            if not is_valid_id(sid) or sid in seen:
                continue
            seen.add(sid)
            out.append({"id": sid, "name": _str(item.get("name"), "", 128).strip() or sid})
    return out if out else [dict(DEFAULT_EMBED_SESSION)]


def normalize_settings(raw: Any) -> dict[str, Any]:
    st = raw if _is_obj(raw) else {}
    embed_sessions = normalize_embed_sessions(st.get("embedSessions"))
    follow = st.get("followSession")
    valid_follow = isinstance(follow, str) and (
        follow == "" or any(x["id"] == follow for x in embed_sessions)
    )
    return {
        "snap": bool(st.get("snap")),
        "grid": int(_clamp(_js_round(_num(st.get("grid"), GRID_DEFAULT)), 4, 200)),
        "editMode": st.get("editMode") is True,
        "kconfigCollapse": st.get("kconfigCollapse") is not False,
        "kconfigEngine": _str(st.get("kconfigEngine"), "", 256),
        "bgColor": normalize_color(st.get("bgColor")),
        "embedSessions": embed_sessions,
        "followSession": follow if valid_follow else embed_sessions[0]["id"],
        "layers": normalize_layers(st.get("layers")),
        "renderQuality": _pick(st.get("renderQuality"), RENDER_QUALITIES, "auto"),
    }


def normalize_tours(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in raw[:MAX_TOURS]:
        if not _is_obj(t):
            continue
        tid = t.get("id")
        if not is_valid_id(tid) or tid in seen:
            continue
        seen.add(tid)
        steps: list[dict[str, Any]] = []
        step_seen: set[str] = set()
        raw_steps = t.get("steps")
        for s in (raw_steps[:MAX_TOUR_STEPS] if isinstance(raw_steps, list) else []):
            if not _is_obj(s):
                continue
            raw_sid = s.get("id")
            sid = raw_sid if (is_valid_id(raw_sid) and raw_sid not in step_seen) else new_id("step")
            step_seen.add(sid)
            vp = s.get("viewport") if _is_obj(s.get("viewport")) else {}
            raw_nids = s.get("nodeIds")
            raw_eids = s.get("edgeIds")
            node_ids = [x for x in (raw_nids if isinstance(raw_nids, list) else []) if is_valid_id(x)][:MAX_STEP_TARGETS]
            edge_ids = [x for x in (raw_eids if isinstance(raw_eids, list) else []) if is_valid_id(x)][:MAX_STEP_TARGETS]
            zoom_val = float(_clamp(_num(vp.get("zoom"), 1), ZOOM_MIN, ZOOM_MAX))
            zoom_out: int | float = int(zoom_val) if zoom_val == int(zoom_val) else zoom_val
            steps.append(
                {
                    "id": sid,
                    "title": _str(s.get("title"), "", 256) or "Step",
                    "caption": _str(s.get("caption"), "", 2000),
                    "nodeIds": node_ids,
                    "edgeIds": edge_ids,
                    "viewport": {
                        "x": _js_round(_num(vp.get("x"), 0)),
                        "y": _js_round(_num(vp.get("y"), 0)),
                        "zoom": zoom_out,
                    },
                }
            )
        out.append(
            {
                "id": tid,
                "title": _str(t.get("title"), "", 128).strip() or "Walkthrough",
                "steps": steps,
            }
        )
    return out


def empty_canvas() -> dict[str, Any]:
    return {
        "schema": CANVAS_SCHEMA,
        "version": CANVAS_VERSION,
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "settings": normalize_settings({}),
        "tours": [],
    }


def _group_cycle(by_id: dict[str, dict[str, Any]], node: dict[str, Any]) -> bool:
    seen: set[str] = {node["id"]}
    cur: dict[str, Any] | None = node
    while cur and cur.get("groupId"):
        gid = cur["groupId"]
        if gid in seen:
            return True
        seen.add(gid)
        cur = by_id.get(gid)
    return False


def normalize_canvas(raw: Any, depth: int = 0, *, strict: bool = True) -> dict[str, Any] | None:
    """Repair and validate a canvas document into canonical `kip-nodemap-canvas` v1 shape.

    When `strict=True` (the default, matching `canvas_model.js:normalizeCanvas`), `raw` must carry
    `schema == "kip-nodemap-canvas"`, `version == 1`, and a `nodes` list. When `strict=False`,
    missing `schema`/`version`/`nodes` default to an empty canvas shell before normalizing.
    """
    if not _is_obj(raw):
        return None
    if strict:
        if (
            raw.get("schema") != CANVAS_SCHEMA
            or raw.get("version") != CANVAS_VERSION
            or isinstance(raw.get("version"), bool)
            or not isinstance(raw.get("nodes"), list)
        ):
            return None
    raw_nodes = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in raw_nodes[:MAX_NODES]:
        node = normalize_node(n, depth)
        if not node or node["id"] in seen:
            continue
        seen.add(node["id"])
        nodes.append(node)

    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        gid = n.get("groupId")
        g = by_id.get(gid) if gid else None
        if not g or g.get("type") != "group" or g["id"] == n["id"]:
            n["groupId"] = None
    for n in nodes:
        if n.get("groupId") and _group_cycle(by_id, n):
            n["groupId"] = None

    raw_edges = raw.get("edges") if isinstance(raw.get("edges"), list) else []
    edges: list[dict[str, Any]] = []
    edge_ids: set[str] = set()
    for e in raw_edges[:MAX_EDGES]:
        edge = normalize_edge(e, seen)
        if not edge or edge["id"] in edge_ids:
            continue
        edge_ids.add(edge["id"])
        edges.append(edge)

    vp = raw.get("viewport") if _is_obj(raw.get("viewport")) else {}
    zoom_val = float(_clamp(_num(vp.get("zoom"), 1), ZOOM_MIN, ZOOM_MAX))
    zoom_out: int | float = int(zoom_val) if zoom_val == int(zoom_val) else zoom_val
    return {
        "schema": CANVAS_SCHEMA,
        "version": CANVAS_VERSION,
        "nodes": nodes,
        "edges": edges,
        "viewport": {
            "x": _num(vp.get("x"), 0),
            "y": _num(vp.get("y"), 0),
            "zoom": zoom_out,
        },
        "settings": normalize_settings(raw.get("settings")),
        "tours": normalize_tours(raw.get("tours")),
    }


def serialize_canvas(canvas: dict[str, Any]) -> dict[str, Any]:
    """Deep-copied JSON-serializable canonical canvas document (`canvas_model.js:serializeCanvas`)."""
    return copy.deepcopy(
        {
            "schema": CANVAS_SCHEMA,
            "version": CANVAS_VERSION,
            "nodes": canvas.get("nodes") or [],
            "edges": canvas.get("edges") or [],
            "viewport": canvas.get("viewport") or {"x": 0, "y": 0, "zoom": 1},
            "settings": canvas.get("settings") or normalize_settings({}),
            "tours": canvas.get("tours") or [],
        }
    )


def is_layer_visible(settings: dict[str, Any] | None, layer_id: str | None) -> bool:
    if not layer_id:
        return True
    layers = (settings or {}).get("layers") or []
    layer = next((l for l in layers if _is_obj(l) and l.get("id") == layer_id), None)
    return layer.get("visible") is not False if layer else True


def visible_canvas_slice(canvas: dict[str, Any]) -> dict[str, Any]:
    """Filter canvas nodes and edges to only those visible in active visibility layers."""
    if not _is_obj(canvas):
        return empty_canvas()
    st = canvas.get("settings") or {}
    if not st.get("layers"):
        return canvas
    vis_nodes = [n for n in (canvas.get("nodes") or []) if is_layer_visible(st, n.get("layerId"))]
    vis_ids = {n["id"] for n in vis_nodes}
    vis_edges = [
        e
        for e in (canvas.get("edges") or [])
        if e.get("from") in vis_ids and e.get("to") in vis_ids and is_layer_visible(st, e.get("layerId"))
    ]
    return {**canvas, "nodes": vis_nodes, "edges": vis_edges}


def paint_order(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Groups first (outer before inner), then non-group nodes in document order (`canvas_model.js:paintOrder`)."""
    depth_map: dict[str, int] = {}
    by_id = {n["id"]: n for n in nodes if _is_obj(n) and "id" in n}

    def depth_of(n: dict[str, Any], guard: int = 0) -> int:
        nid = n["id"]
        if nid in depth_map:
            return depth_map[nid]
        gid = n.get("groupId")
        g = by_id.get(gid) if gid else None
        d = depth_of(g, guard + 1) + 1 if (g and guard < 64) else 0
        depth_map[nid] = d
        return d

    groups = sorted([n for n in nodes if n.get("type") == "group"], key=lambda n: depth_of(n))
    non_groups = [n for n in nodes if n.get("type") != "group"]
    return [*groups, *non_groups]


def bounds_of(nodes: list[dict[str, Any]]) -> dict[str, int] | None:
    if not nodes:
        return None
    x1 = min(int(n.get("x", 0)) for n in nodes)
    y1 = min(int(n.get("y", 0)) for n in nodes)
    x2 = max(int(n.get("x", 0)) + int(n.get("w", 0)) for n in nodes)
    y2 = max(int(n.get("y", 0)) + int(n.get("h", 0)) for n in nodes)
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def fit_viewport(
    bounds: dict[str, Any] | None,
    width: int = 1280,
    height: int = 800,
    pad: int = 40,
) -> dict[str, Any]:
    """Viewport `{x, y, zoom}` that fits `bounds` into `width`x`height` with `pad` margin."""
    if not bounds or not width or not height:
        return {"x": 0, "y": 0, "zoom": 1}
    bw = max(1, float(bounds.get("w", 1)))
    bh = max(1, float(bounds.get("h", 1)))
    raw_zoom = min((width - pad * 2) / bw, (height - pad * 2) / bh)
    zoom = float(_clamp(raw_zoom, ZOOM_MIN, 1.5))
    zoom_rounded = round(zoom, 3)
    cx = float(bounds.get("x", 0)) + bw / 2.0
    cy = float(bounds.get("y", 0)) + bh / 2.0
    return {
        "x": _js_round(width / 2.0 - cx * zoom_rounded),
        "y": _js_round(height / 2.0 - cy * zoom_rounded),
        "zoom": int(zoom_rounded) if zoom_rounded == int(zoom_rounded) else zoom_rounded,
    }


def fit_group_to_members(
    group: dict[str, Any],
    members: list[dict[str, Any]],
    pad: dict[str, int] = GROUP_LAYOUT_PAD,
) -> dict[str, Any]:
    """Grow or shrink `group` so `members` fit inside its frame with `GROUP_LAYOUT_PAD`."""
    b = bounds_of(members)
    if not group or not b:
        return group
    lim = SIZE_LIMITS["group"]
    group["w"] = _js_round(_clamp(b["x"] + b["w"] + pad["x"] - group["x"], lim["minW"], lim["maxW"]))
    group["h"] = _js_round(_clamp(b["y"] + b["h"] + pad["bottom"] - group["y"], lim["minH"], lim["maxH"]))
    if not group.get("autoFit"):
        group["baseW"] = group["w"]
        group["baseH"] = group["h"]
    return group


def group_descendants(nodes: list[dict[str, Any]], group_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    queue = [group_id]
    seen = {group_id}
    while queue:
        gid = queue.pop(0)
        for n in nodes:
            if n.get("groupId") == gid and n["id"] not in seen:
                seen.add(n["id"])
                out.append(n)
                if n.get("type") == "group":
                    queue.append(n["id"])
    return out


def remove_nodes(
    canvas: dict[str, Any],
    ids: Iterable[str],
    *,
    cascade_group_members: bool = False,
) -> dict[str, Any]:
    """Remove `ids` and incident edges; clear `groupId` on surviving members (or cascade if requested)."""
    drop = set(ids)
    if cascade_group_members:
        for gid in list(drop):
            for desc in group_descendants(canvas.get("nodes") or [], gid):
                drop.add(desc["id"])
    canvas["nodes"] = [n for n in (canvas.get("nodes") or []) if n["id"] not in drop]
    for n in canvas["nodes"]:
        if n.get("groupId") in drop:
            n["groupId"] = None
    canvas["edges"] = [
        e for e in (canvas.get("edges") or []) if e.get("from") not in drop and e.get("to") not in drop
    ]
    return canvas


def struct_item_key(item: dict[str, Any]) -> str:
    """Stable key of a struct layout item (`canvas_model.js:structItemKey`)."""
    line = item.get("line") or 0
    label = item.get("name") or item.get("tag") or item.get("directive") or ""
    return f"{line}:{label}"


def format_lock_badge(lock: dict[str, Any] | None) -> str:
    if not _is_obj(lock) or not lock.get("kind"):
        return ""
    lock_name = str(lock.get("lockName") or "").strip()
    return f"🔒 {lock['kind']}: {lock_name}" if lock_name else f"🔒 {lock['kind']}"
