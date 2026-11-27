"""api/routers/nodemap.py - NodeMap struct/source inspection and programmatic canvas authoring endpoints."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from api.config import (
    get_active_repo_name,
    require_feature,
    resolve_default_version,
    resolve_version,
)
from api.routers.common import (
    DEFAULT_ARCH,
    _with_repo,
    nodemap_canvas_service,
    nodemap_service,
)
from api.security import get_active_api_key_id, require_write_scope
from webapp.backend.services.nodemap_canvas_schema import (
    ARROWS,
    CURVES,
    DIFF_MODES,
    DOC_MODES,
    HISTORY_MODES,
    ID_RE,
    KAPI_CATALOGS,
    LINE_STYLES,
    LOCK_KINDS,
    NODE_TYPES,
    SIDES,
    SOURCE_MODES,
    STRUCT_STYLES,
)

router = APIRouter()

_NODE_DATA_KEYS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "struct": (
        "name", "kind", "path", "hint", "line", "showTypes", "showGuards", "typeOverrides",
        "collapsed", "style", "collapsedGuards", "hiddenItems", "fieldLocks", "disableKconfigEngine",
    ),
    "symbol": ("name", "kind", "astId", "expanded", "constituents"),
    "source": ("path", "mode", "start", "end", "symbol", "symbolKind", "context"),
    "man": ("repo", "version", "path", "name", "section", "mode"),
    "tab": ("tabType", "hash", "embedSessionId", "frozen", "viewState"),
    "diff": ("mode", "versionA", "versionB", "path", "oldPath", "commitHash"),
    "mail": ("tid", "mid", "subject", "openMids", "fullMids", "pinnedBodies", "single", "fullBody"),
    "kconfig": ("symbol", "arch"),
    "maintainer": ("secId", "title", "query", "path"),
    "cve": ("cveId", "release", "filePath"),
    "kapi": ("catalog", "query", "symbol", "arch"),
    "doc": ("path", "mode", "functions"),
    "history": ("mode", "path", "line", "name", "astId"),
    "commit": ("commitHash", "subject"),
    "note": ("text", "markdown"),
    "group": ("subCanvas",),
}
_ALL_NODE_DATA_KEYS: frozenset[str] = frozenset(
    key for keys in _NODE_DATA_KEYS_BY_TYPE.values() for key in keys
)
_NODE_TYPE_SUGGESTIONS: dict[str, str] = {
    "file": "source",
    "code": "source",
    "c": "source",
    "config": "kconfig",
    "email": "mail",
    "patch": "diff",
    "git_commit": "commit",
    "func": "symbol",
    "function": "symbol",
    "maintainers": "maintainer",
    "docs": "doc",
}
_DATA_KEY_SUGGESTIONS: dict[str, str] = {
    "commit_hash": "commitHash",
    "file": "path",
    "file_path": "filePath (for cve) or path (for source/struct/doc/diff/history/man)",
    "cve_id": "cveId",
    "version_a": "versionA",
    "version_b": "versionB",
    "old_path": "oldPath",
    "ast_id": "astId",
    "sec_id": "secId",
    "symbol_kind": "symbolKind",
    "message_id": "mid",
    "thread_id": "tid",
}
_TOP_LEVEL_NODE_FIELDS: frozenset[str] = frozenset({
    "id", "type", "title", "customTitle", "x", "y", "w", "h", "color", "bgColor",
    "hideHeader", "groupId", "layerId", "version", "repo", "locks", "autoFit", "baseW", "baseH", "scale", "data",
})
_TOP_LEVEL_EDGE_FIELDS: frozenset[str] = frozenset({
    "id", "from", "to", "label", "style", "arrow", "curve", "fromSide", "toSide",
    "fromField", "toField", "color", "layerId", "lockKind", "preset", "waypoints", "callSite",
})
_EDGE_SNAKE_ALIASES: frozenset[str] = frozenset({
    "from_field", "to_field", "from_side", "to_side", "lock_kind",
})
_TOP_LEVEL_GROUP_FIELDS: frozenset[str] = frozenset({
    "id", "title", "customTitle", "members", "fit_to_members", "parentGroupId", "layerId",
    "color", "bgColor", "version", "repo", "x", "y", "w", "h", "subCanvas",
})
_GROUP_COMPAT_FIELDS: frozenset[str] = frozenset({"group_id", "groupId", "member_ids", "node_ids", "auto_fit"})
_TOP_LEVEL_TOUR_FIELDS: frozenset[str] = frozenset({"id", "title", "steps"})
_TOUR_STEP_FIELDS: frozenset[str] = frozenset({"id", "title", "caption", "nodeIds", "edgeIds", "viewport"})
_TOUR_STEP_OPS_ALIASES: frozenset[str] = frozenset({"nodeId", "node_ids", "edgeId", "edge_ids", "auto_viewport"})
_SUPPORTED_OPS: frozenset[str] = frozenset({
    "upsert_nodes", "add_nodes", "add_node", "upsert_node", "update_nodes", "update_node",
    "remove_nodes", "remove_node", "delete_nodes",
    "upsert_edges", "add_edges", "add_edge", "connect",
    "remove_edges", "remove_edge", "disconnect",
    "upsert_groups", "add_group", "upsert_group", "assign_group",
    "annotate", "add_note",
    "upsert_tour", "upsert_tours", "add_tour",
    "remove_tour", "remove_tours", "delete_tour",
    "expand",
})


class NodemapNodeDataInput(BaseModel):
    """Type-specific payload for a NodeMap node (`nodes[].data`)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=256, description="Struct, symbol, man-page, or history target name.")
    kind: str | None = Field(default=None, max_length=32, description="Kind ('struct'|'union' for struct; 'symbol'|'func'|'macro' for symbol).")
    path: str | None = Field(default=None, max_length=1024, description="Repository-relative file path (source/struct/man/diff/doc/history/maintainer).")
    hint: str | None = Field(default=None, max_length=1024, description="Header path hint for struct disambiguation.")
    line: int | None = Field(default=None, ge=1, description="1-based source line number.")
    showTypes: bool | None = None
    showGuards: bool | None = None
    typeOverrides: dict[str, bool] | None = None
    collapsed: dict[str, bool] | None = None
    style: Literal["list", "table", "layout"] | None = None
    collapsedGuards: dict[str, bool] | None = None
    hiddenItems: dict[str, bool] | None = None
    fieldLocks: dict[str, Any] | None = None
    disableKconfigEngine: bool | None = None
    astId: int | None = None
    expanded: bool | None = None
    constituents: list[Any] | None = None
    mode: str | None = Field(default=None, max_length=32, description="Mode (source: file|range|symbol; diff: file|commit; doc: auto|rst|kernel-doc; history: symbol|blame).")
    start: int | None = Field(default=None, ge=1, description="Start line for source range.")
    end: int | None = Field(default=None, ge=1, description="End line for source range.")
    symbol: str | None = Field(default=None, max_length=256, description="Symbol or Kconfig symbol name (without CONFIG_).")
    symbolKind: Literal["", "struct", "union", "function", "macro", "enum"] | None = None
    context: int | None = Field(default=None, ge=0, le=200)
    repo: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=256)
    section: str | None = Field(default=None, max_length=16, description="Man-page section.")
    tabType: str | None = Field(default=None, max_length=32)
    hash: str | None = Field(default=None, max_length=4096)
    embedSessionId: str | None = Field(default=None, max_length=64)
    frozen: bool | None = None
    viewState: dict[str, Any] | None = None
    versionA: str | None = Field(default=None, max_length=256)
    versionB: str | None = Field(default=None, max_length=256)
    oldPath: str | None = Field(default=None, max_length=1024)
    commitHash: str | None = Field(default=None, max_length=64, description="Git commit SHA (diff/commit).")
    tid: int | None = Field(default=None, ge=1, description="LKML thread ID (mail).")
    mid: str | None = Field(default=None, max_length=512, description="LKML Message-ID (mail).")
    subject: str | None = Field(default=None, max_length=512, description="Email or commit subject.")
    openMids: dict[str, bool] | None = None
    fullMids: dict[str, bool] | None = None
    pinnedBodies: dict[str, str] | None = None
    single: bool | None = None
    fullBody: str | None = None
    arch: str | None = Field(default=None, max_length=64, description="Target arch (kconfig/kapi).")
    secId: int | None = Field(default=None, ge=1, description="MAINTAINERS section ID.")
    title: str | None = Field(default=None, max_length=512, description="MAINTAINERS section title.")
    query: str | None = Field(default=None, max_length=256, description="Search query (maintainer/kapi).")
    cveId: str | None = Field(default=None, max_length=64, description="CVE identifier.")
    release: str | None = Field(default=None, max_length=128)
    filePath: str | None = Field(default=None, max_length=1024, description="File path for CVE card.")
    catalog: Literal["summary", "syscalls", "exports", "params", "tracepoints", "modules", "devices"] | None = None
    functions: str | None = Field(default=None, max_length=256)
    text: str | None = Field(default=None, max_length=20000, description="Note body text.")
    markdown: bool | None = None
    subCanvas: dict[str, Any] | None = None


class NodemapNodeInput(BaseModel):
    """NodeMap canvas node specification."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    type: Literal[
        "struct", "symbol", "source", "man", "tab", "note", "group", "diff",
        "mail", "kconfig", "maintainer", "cve", "kapi", "doc", "history", "commit",
    ]
    title: str | None = Field(default=None, max_length=512)
    customTitle: str | None = Field(default=None, max_length=512)
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    color: str | None = Field(default=None, max_length=32)
    bgColor: str | None = Field(default=None, max_length=32)
    hideHeader: bool | None = None
    groupId: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    layerId: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=256)
    repo: str | None = Field(default=None, max_length=128)
    locks: list[dict[str, Any]] | None = None
    autoFit: bool | None = None
    baseW: float | None = None
    baseH: float | None = None
    scale: float | None = None
    data: NodemapNodeDataInput | None = None


class NodemapEdgeInput(BaseModel):
    """NodeMap canvas edge specification."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    from_node: str = Field(..., alias="from", min_length=1, max_length=64, description="Source node ID.")
    to: str = Field(..., min_length=1, max_length=64, description="Target node ID.")
    label: str | None = Field(default=None, max_length=512)
    style: Literal["solid", "dashed", "dotted"] | None = None
    arrow: Literal["none", "forward", "backward", "both"] | None = None
    curve: Literal["bezier", "straight", "orthogonal"] | None = None
    fromSide: Literal["auto", "top", "right", "bottom", "left"] | None = None
    toSide: Literal["auto", "top", "right", "bottom", "left"] | None = None
    fromField: str | None = Field(default=None, max_length=600)
    toField: str | None = Field(default=None, max_length=256)
    color: str | None = Field(default=None, max_length=32)
    layerId: str | None = Field(default=None, max_length=64)
    lockKind: Literal["", "spinlock", "mutex", "rwsem", "rcu", "atomic", "seqlock", "lock-order"] | None = None
    preset: str | None = Field(default=None, max_length=64)
    waypoints: list[dict[str, Any]] | None = None
    callSite: dict[str, Any] | None = None


class NodemapGroupInput(BaseModel):
    """NodeMap canvas group specification."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    title: str | None = Field(default=None, max_length=512)
    customTitle: str | None = Field(default=None, max_length=512)
    members: list[str] | None = None
    fit_to_members: bool | None = None
    parentGroupId: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    layerId: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=32)
    bgColor: str | None = Field(default=None, max_length=32)
    version: str | None = Field(default=None, max_length=256)
    repo: str | None = Field(default=None, max_length=128)
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    subCanvas: dict[str, Any] | None = None


class NodemapTourStepInput(BaseModel):
    """Walkthrough step targeting nodes/edges on a NodeMap canvas."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    title: str = Field(..., min_length=1, max_length=256)
    caption: str | None = Field(default=None, max_length=2000)
    nodeIds: list[str] = Field(default_factory=list)
    edgeIds: list[str] = Field(default_factory=list)
    viewport: dict[str, Any] | None = None


class NodemapTourInput(BaseModel):
    """Guided walkthrough tour over a NodeMap canvas."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=r"^[\w-]{1,64}$")
    title: str = Field(default="Walkthrough", min_length=1, max_length=128)
    steps: list[NodemapTourStepInput] = Field(default_factory=list)


def _fmt_loc(loc: list[str | int]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif not out:
            out = part
        else:
            out += f".{part}"
    return out


def _err(loc: list[str | int], msg: str, err_type: str = "value_error") -> dict[str, Any]:
    return {"loc": loc, "msg": f"{_fmt_loc(loc)}: {msg}", "type": err_type}


def _validate_node_dict(
    node: Any,
    loc: list[str | int],
    errors: list[dict[str, Any]],
    *,
    is_update: bool = False,
) -> None:
    if not isinstance(node, dict):
        errors.append(_err(loc, f"Expected node object, got {type(node).__name__}.", "dict_type"))
        return

    node_type = node.get("type")
    if (not is_update or "type" in node) and (not isinstance(node_type, str) or node_type not in NODE_TYPES):
        hint = f" Did you mean '{_NODE_TYPE_SUGGESTIONS[node_type]}'?" if isinstance(node_type, str) and node_type in _NODE_TYPE_SUGGESTIONS else ""
        errors.append(
            _err(
                [*loc, "type"],
                f"Unknown node type {node_type!r}.{hint} Expected one of: {', '.join(NODE_TYPES)}.",
                "literal_error",
            )
        )
        return

    for key in node:
        if key not in _TOP_LEVEL_NODE_FIELDS:
            if key in _ALL_NODE_DATA_KEYS or key in _DATA_KEY_SUGGESTIONS:
                target_key = _DATA_KEY_SUGGESTIONS.get(key, key)
                errors.append(
                    _err(
                        [*loc, key],
                        f"Misplaced field '{key}' at top level of node; place '{target_key}' inside '{_fmt_loc(loc)}.data'. Allowed top-level node fields: {', '.join(sorted(_TOP_LEVEL_NODE_FIELDS))}.",
                        "extra_forbidden",
                    )
                )
            else:
                errors.append(
                    _err(
                        [*loc, key],
                        f"Unexpected top-level node field '{key}'. Allowed top-level node fields: {', '.join(sorted(_TOP_LEVEL_NODE_FIELDS))}.",
                        "extra_forbidden",
                    )
                )

    data = node.get("data")
    if data is not None:
        if not isinstance(data, dict):
            errors.append(_err([*loc, "data"], f"Expected object for node.data, got {type(data).__name__}.", "dict_type"))
            return
        if isinstance(node_type, str) and node_type in _NODE_DATA_KEYS_BY_TYPE:
            allowed_data = _NODE_DATA_KEYS_BY_TYPE[node_type]
            for dkey, dval in data.items():
                if dkey not in allowed_data:
                    sug = _DATA_KEY_SUGGESTIONS.get(dkey)
                    hint = f" Did you mean '{sug}'?" if sug else ""
                    errors.append(
                        _err(
                            [*loc, "data", dkey],
                            f"Unknown data field '{dkey}' for node type '{node_type}'.{hint} Allowed data fields for '{node_type}': {', '.join(allowed_data)}.",
                            "extra_forbidden",
                        )
                    )
                elif dkey == "kind" and node_type == "struct" and dval not in (None, "struct", "union"):
                    errors.append(_err([*loc, "data", "kind"], f"Invalid struct kind {dval!r}; expected 'struct' or 'union'."))
                elif dkey == "style" and node_type == "struct" and dval not in (None, *STRUCT_STYLES):
                    errors.append(_err([*loc, "data", "style"], f"Invalid struct style {dval!r}; expected one of: {', '.join(STRUCT_STYLES)}."))
                elif dkey == "mode":
                    valid_modes = {
                        "source": SOURCE_MODES,
                        "diff": DIFF_MODES,
                        "doc": DOC_MODES,
                        "history": HISTORY_MODES,
                        "man": ("web", "term", ""),
                    }.get(node_type)
                    if valid_modes and dval is not None and dval not in valid_modes:
                        errors.append(_err([*loc, "data", "mode"], f"Invalid mode {dval!r} for '{node_type}'; expected one of: {', '.join(valid_modes)}."))
                elif dkey == "catalog" and node_type == "kapi" and dval not in (None, *KAPI_CATALOGS):
                    errors.append(_err([*loc, "data", "catalog"], f"Invalid kapi catalog {dval!r}; expected one of: {', '.join(KAPI_CATALOGS)}."))


def _validate_edge_dict(
    edge: Any,
    loc: list[str | int],
    errors: list[dict[str, Any]],
    *,
    allow_snake_aliases: bool = False,
) -> None:
    if not isinstance(edge, dict):
        errors.append(_err(loc, f"Expected edge object, got {type(edge).__name__}.", "dict_type"))
        return
    allowed = _TOP_LEVEL_EDGE_FIELDS | (_EDGE_SNAKE_ALIASES if allow_snake_aliases else frozenset())
    for key in edge:
        if key not in allowed:
            errors.append(_err([*loc, key], f"Unknown edge field '{key}'. Allowed edge fields: {', '.join(sorted(_TOP_LEVEL_EDGE_FIELDS))}.", "extra_forbidden"))
    src = edge.get("from")
    dst = edge.get("to")
    if not isinstance(src, str) or not src.strip():
        errors.append(_err([*loc, "from"], "Edge requires non-empty string 'from' node ID.", "missing"))
    if not isinstance(dst, str) or not dst.strip():
        errors.append(_err([*loc, "to"], "Edge requires non-empty string 'to' node ID.", "missing"))
    if isinstance(src, str) and isinstance(dst, str) and src and src == dst:
        errors.append(_err([*loc, "to"], f"Self-loop edge ('from' == 'to' == {src!r}) is not allowed."))
    for field_name, allowed_vals in (
        ("style", LINE_STYLES),
        ("arrow", ARROWS),
        ("curve", CURVES),
        ("fromSide", SIDES),
        ("toSide", SIDES),
        ("lockKind", ["", *LOCK_KINDS]),
    ):
        val = edge.get(field_name)
        if val is not None and val not in allowed_vals:
            errors.append(_err([*loc, field_name], f"Invalid {field_name} {val!r}; expected one of: {', '.join(allowed_vals)}.", "literal_error"))


def _validate_group_dict(
    group: Any,
    loc: list[str | int],
    errors: list[dict[str, Any]],
    *,
    allow_compat: bool = False,
) -> None:
    if not isinstance(group, dict):
        errors.append(_err(loc, f"Expected group object, got {type(group).__name__}.", "dict_type"))
        return
    allowed = _TOP_LEVEL_GROUP_FIELDS | (_GROUP_COMPAT_FIELDS if allow_compat else frozenset())
    for key in group:
        if key not in allowed:
            errors.append(_err([*loc, key], f"Unknown group field '{key}'. Allowed group fields: {', '.join(sorted(_TOP_LEVEL_GROUP_FIELDS))}.", "extra_forbidden"))
    members = group.get("members")
    if members is not None and (not isinstance(members, list) or not all(isinstance(m, str) and m for m in members)):
        errors.append(_err([*loc, "members"], "Group 'members' must be a list of non-empty node ID strings."))


def _validate_tour_dict(
    tour: Any,
    loc: list[str | int],
    errors: list[dict[str, Any]],
    *,
    require_id: bool = True,
    allow_ops_aliases: bool = False,
) -> None:
    if not isinstance(tour, dict):
        errors.append(_err(loc, f"Expected tour object, got {type(tour).__name__}.", "dict_type"))
        return
    for key in tour:
        if key not in _TOP_LEVEL_TOUR_FIELDS:
            errors.append(_err([*loc, key], f"Unknown tour field '{key}'. Allowed tour fields: id, title, steps.", "extra_forbidden"))
    tid = tour.get("id")
    if require_id and (not isinstance(tid, str) or not ID_RE.match(tid)):
        errors.append(_err([*loc, "id"], "Tour 'id' is required and must match /^[\\w-]{1,64}$/."))
    elif tid is not None and (not isinstance(tid, str) or not ID_RE.match(tid)):
        errors.append(_err([*loc, "id"], "Tour 'id' must match /^[\\w-]{1,64}$/."))
    steps = tour.get("steps")
    if steps is None:
        return
    if not isinstance(steps, list):
        errors.append(_err([*loc, "steps"], "Tour 'steps' must be a list of step objects."))
        return
    allowed_step = _TOUR_STEP_FIELDS | (_TOUR_STEP_OPS_ALIASES if allow_ops_aliases else frozenset())
    for idx, step in enumerate(steps):
        sloc = [*loc, "steps", idx]
        if not isinstance(step, dict):
            errors.append(_err(sloc, f"Expected step object, got {type(step).__name__}.", "dict_type"))
            continue
        for skey in step:
            if skey not in allowed_step:
                errors.append(_err([*sloc, skey], f"Unknown tour step field '{skey}'. Allowed step fields: id, title, caption, nodeIds, edgeIds, viewport.", "extra_forbidden"))
        if not isinstance(step.get("title"), str) or not step["title"].strip():
            errors.append(_err([*sloc, "title"], "Tour step requires non-empty string 'title'."))
        for id_list_key in ("nodeIds", "edgeIds"):
            vals = step.get(id_list_key)
            if vals is not None and (not isinstance(vals, list) or not all(isinstance(v, str) and ID_RE.match(v) for v in vals)):
                errors.append(_err([*sloc, id_list_key], f"Tour step '{id_list_key}' must be a list of valid IDs matching /^[\\w-]{{1,64}}$/."))


def validate_nodemap_payload_strict(raw_args: dict[str, Any]) -> list[dict[str, Any]]:
    """Strict actionable validation for NodeMap MCP inputs (`nodes`, `edges`, `groups`, `tours`, `ops`, `canvas`)."""
    errors: list[dict[str, Any]] = []
    for list_key, fn in (
        ("nodes", lambda item, loc: _validate_node_dict(item, loc, errors)),
        ("upsert_nodes", lambda item, loc: _validate_node_dict(item, loc, errors)),
        ("edges", lambda item, loc: _validate_edge_dict(item, loc, errors)),
        ("upsert_edges", lambda item, loc: _validate_edge_dict(item, loc, errors)),
        ("groups", lambda item, loc: _validate_group_dict(item, loc, errors)),
        ("upsert_groups", lambda item, loc: _validate_group_dict(item, loc, errors)),
        ("tours", lambda item, loc: _validate_tour_dict(item, loc, errors, require_id=True)),
        ("upsert_tours", lambda item, loc: _validate_tour_dict(item, loc, errors, require_id=False, allow_ops_aliases=True)),
    ):
        items = raw_args.get(list_key)
        if isinstance(items, list):
            for idx, item in enumerate(items):
                fn(item, [list_key, idx])

    canvas = raw_args.get("canvas")
    if isinstance(canvas, dict):
        for ckey, cfn in (
            ("nodes", lambda item, loc: _validate_node_dict(item, loc, errors)),
            ("edges", lambda item, loc: _validate_edge_dict(item, loc, errors)),
            ("tours", lambda item, loc: _validate_tour_dict(item, loc, errors, require_id=True)),
        ):
            citems = canvas.get(ckey)
            if isinstance(citems, list):
                for idx, item in enumerate(citems):
                    cfn(item, ["canvas", ckey, idx])

    ops = raw_args.get("ops")
    if isinstance(ops, list):
        for idx, raw_op in enumerate(ops):
            oloc: list[str | int] = ["ops", idx]
            if not isinstance(raw_op, dict):
                errors.append(_err(oloc, f"Expected op object, got {type(raw_op).__name__}.", "dict_type"))
                continue
            op_name = str(raw_op.get("op") or raw_op.get("action") or raw_op.get("type") or "").strip().lower()
            if op_name and op_name not in _SUPPORTED_OPS:
                errors.append(_err([*oloc, "op"], f"Unknown operation {op_name!r}; expected one of: {', '.join(sorted(_SUPPORTED_OPS))}."))
                continue
            if op_name in ("upsert_nodes", "add_nodes", "add_node", "upsert_node", "update_nodes", "update_node") or ("nodes" in raw_op and not op_name):
                is_upd = op_name in ("update_nodes", "update_node")
                if isinstance(raw_op.get("nodes"), list):
                    for nidx, nitem in enumerate(raw_op["nodes"]):
                        _validate_node_dict(nitem, [*oloc, "nodes", nidx], errors, is_update=is_upd)
                elif isinstance(raw_op.get("node"), dict):
                    _validate_node_dict(raw_op["node"], [*oloc, "node"], errors, is_update=is_upd)
            elif op_name in ("upsert_edges", "add_edges", "add_edge", "connect") or ("edges" in raw_op and not op_name):
                if isinstance(raw_op.get("edges"), list):
                    for eidx, eitem in enumerate(raw_op["edges"]):
                        _validate_edge_dict(eitem, [*oloc, "edges", eidx], errors, allow_snake_aliases=True)
                elif isinstance(raw_op.get("edge"), dict):
                    _validate_edge_dict(raw_op["edge"], [*oloc, "edge"], errors, allow_snake_aliases=True)
                elif "from" in raw_op or "to" in raw_op:
                    edge_view = {k: v for k, v in raw_op.items() if k not in ("op", "action", "type")}
                    _validate_edge_dict(edge_view, oloc, errors, allow_snake_aliases=True)
            elif op_name in ("upsert_groups", "add_group", "upsert_group", "assign_group"):
                if isinstance(raw_op.get("groups"), list):
                    for gidx, gitem in enumerate(raw_op["groups"]):
                        _validate_group_dict(gitem, [*oloc, "groups", gidx], errors, allow_compat=True)
                else:
                    grp_view = {k: v for k, v in raw_op.items() if k not in ("op", "action", "type")}
                    _validate_group_dict(grp_view, oloc, errors, allow_compat=True)
            elif op_name in ("upsert_tour", "upsert_tours", "add_tour"):
                if isinstance(raw_op.get("tours"), list):
                    for tidx, titem in enumerate(raw_op["tours"]):
                        _validate_tour_dict(titem, [*oloc, "tours", tidx], errors, require_id=False, allow_ops_aliases=True)
                elif isinstance(raw_op.get("tour"), dict):
                    _validate_tour_dict(raw_op["tour"], [*oloc, "tour"], errors, require_id=False, allow_ops_aliases=True)
                else:
                    tour_view = {k: v for k, v in raw_op.items() if k not in ("op", "action", "type")}
                    _validate_tour_dict(tour_view, oloc, errors, require_id=False, allow_ops_aliases=True)
    return errors


class NodemapPreviewRequest(BaseModel):
    """Stateless read-only NodeMap canvas construction, hydration, expansion, layout, and export request."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Preview Canvas", max_length=256)
    version: str | None = Field(default=None, max_length=256)
    nodes: list[NodemapNodeInput] = Field(default_factory=list)
    edges: list[NodemapEdgeInput] = Field(default_factory=list)
    groups: list[NodemapGroupInput] = Field(default_factory=list)
    ops: list[dict[str, Any]] = Field(default_factory=list)
    expansions: list[dict[str, Any]] = Field(default_factory=list)
    expand: dict[str, Any] | list[dict[str, Any]] | None = None
    layout: dict[str, Any] | str | None = None
    settings: dict[str, Any] | None = None
    tours: list[NodemapTourInput] = Field(default_factory=list)
    canvas: dict[str, Any] | None = None
    hydrate: bool = True
    auto_layout: str | None = Field(default="dag")
    export_format: str | None = Field(default=None, pattern="^(json|mermaid|dot)$")
    direction: str = Field(default="LR", pattern="^(LR|RL|TB|TD|BT)$")


class NodemapCreateRequest(BaseModel):
    """Create and persist a new NodeMap canvas document."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(default="Untitled Canvas", min_length=1, max_length=256)
    description: str = Field(default="", max_length=2000)
    version: str | None = Field(default=None, max_length=256)
    nodes: list[NodemapNodeInput] = Field(default_factory=list)
    edges: list[NodemapEdgeInput] = Field(default_factory=list)
    groups: list[NodemapGroupInput] = Field(default_factory=list)
    ops: list[dict[str, Any]] = Field(default_factory=list)
    expansions: list[dict[str, Any]] = Field(default_factory=list)
    expand: dict[str, Any] | list[dict[str, Any]] | None = None
    layout: dict[str, Any] | str | None = None
    settings: dict[str, Any] | None = None
    tours: list[NodemapTourInput] = Field(default_factory=list)
    canvas: dict[str, Any] | None = None
    hydrate: bool = True
    auto_layout: str | None = Field(default="dag")


class NodemapReplaceRequest(BaseModel):
    """Full replacement of a saved NodeMap canvas document with optional optimistic concurrency check."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    version: str | None = Field(default=None, max_length=256)
    expected_revision: int | None = Field(default=None, ge=1)
    nodes: list[NodemapNodeInput] = Field(default_factory=list)
    edges: list[NodemapEdgeInput] = Field(default_factory=list)
    groups: list[NodemapGroupInput] = Field(default_factory=list)
    ops: list[dict[str, Any]] = Field(default_factory=list)
    layout: dict[str, Any] | str | None = None
    settings: dict[str, Any] | None = None
    tours: list[NodemapTourInput] = Field(default_factory=list)
    canvas: dict[str, Any] | None = None
    hydrate: bool = True
    auto_layout: str | None = None


class NodemapMutateRequest(BaseModel):
    """Atomic batch of declarative operations over a saved NodeMap canvas."""

    model_config = ConfigDict(extra="forbid")

    ops: list[dict[str, Any]] = Field(default_factory=list)
    upsert_nodes: list[NodemapNodeInput] | None = None
    remove_nodes: list[str] | None = None
    upsert_edges: list[NodemapEdgeInput] | None = None
    remove_edges: list[str] | None = None
    upsert_groups: list[NodemapGroupInput] | None = None
    annotate: dict[str, Any] | None = None
    upsert_tour: dict[str, Any] | None = None
    upsert_tours: list[NodemapTourInput] | None = None
    layout: dict[str, Any] | str | None = None
    name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    version: str | None = Field(default=None, max_length=256)
    expected_revision: int | None = Field(default=None, ge=1)
    hydrate: bool = True
    auto_layout: str | None = None


class NodemapExpandRequest(BaseModel):
    """Server-side graph expansion over a saved NodeMap canvas."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: str = Field(pattern="^(struct_types|callgraph|callpath|kconfig|commit|impact_map)$")
    node_id: str | None = Field(default=None, max_length=64)
    struct: str | None = Field(default=None, max_length=256)
    function: str | None = Field(default=None, max_length=256)
    direction: str = Field(default="both", pattern="^(both|callers|callees|in|out)$")
    from_func: str | None = Field(default=None, alias="from", max_length=256, description="Starting function name (callpath mode).")
    to_func: str | None = Field(default=None, alias="to", max_length=256, description="Target function name (callpath mode).")
    max_depth: int = Field(default=6, ge=1, le=10)
    k: int = Field(default=5, ge=1, le=20)
    symbol: str | None = Field(default=None, max_length=256)
    arch: str = Field(default=DEFAULT_ARCH, max_length=64)
    commit_hash: str | None = Field(default=None, max_length=64)
    seed_kind: str | None = Field(default=None, max_length=64)
    target: str | None = Field(default=None, max_length=512)
    path: str | None = Field(default=None, max_length=512)
    files: list[str] = Field(default_factory=list)
    diff_text: str = Field(default="", max_length=200000)
    title: str = Field(default="", max_length=256)
    limit: int = Field(default=16, ge=1, le=100)
    version: str | None = Field(default=None, max_length=256)
    expected_revision: int | None = Field(default=None, ge=1)
    auto_layout: str | bool | None = Field(default="dag")


class NodemapLayoutRequest(BaseModel):
    """Deterministic Sugiyama DAG or force-directed layout over a saved NodeMap canvas."""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(default="dag", pattern="^(dag|force)$")
    group_id: str | None = Field(default=None, max_length=64)
    node_ids: list[str] | None = None
    fit_groups: bool = True
    options: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


@router.get("/nodemap/struct", dependencies=[Depends(require_feature("nodemap"))])
def get_nodemap_struct(
    name: Annotated[str, Query(min_length=1, max_length=256)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    path: Annotated[str | None, Query(max_length=512)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    line: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """Members of a struct/union with preprocessor guards read from its source span."""
    ver = resolve_version(version)
    res = nodemap_service.get_struct(ver, name, path=path, kind=kind, line=line)
    return {"repo": get_active_repo_name(), **res}


@router.get("/nodemap/source", dependencies=[Depends(require_feature("nodemap"))])
def get_nodemap_source(
    path: Annotated[str | None, Query(max_length=512)] = None,
    symbol: Annotated[str | None, Query(max_length=256)] = None,
    version: Annotated[str | None, Query(max_length=256)] = None,
    start: Annotated[int | None, Query(ge=1)] = None,
    end: Annotated[int | None, Query(ge=1)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    context: Annotated[int, Query(ge=0, le=200)] = 0,
) -> dict[str, Any]:
    """Lines of a file, line range, or symbol definition span plus `context` lines for NodeMap cards."""
    ver = resolve_version(version)
    res = nodemap_service.get_source(ver, path=path, start=start, end=end, symbol=symbol, kind=kind, context=context)
    return {"repo": get_active_repo_name(), **res}


@router.get("/nodemap/canvases", dependencies=[Depends(require_feature("nodemap"))])
def list_nodemap_canvases(
    q: Annotated[str, Query(max_length=256)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Paginated list of saved NodeMap canvases for the active repository."""
    return _with_repo(nodemap_canvas_service.list_canvases(q=q, limit=limit, offset=offset))


@router.get("/nodemap/canvases/{canvas_id}", dependencies=[Depends(require_feature("nodemap"))])
def get_nodemap_canvas(canvas_id: str) -> dict[str, Any]:
    """Retrieve a saved NodeMap canvas document, metadata, and webapp URL."""
    return _with_repo(nodemap_canvas_service.get_canvas(canvas_id))


@router.get("/nodemap/canvases/{canvas_id}/export", dependencies=[Depends(require_feature("nodemap"))])
def export_nodemap_canvas(
    canvas_id: str,
    format: Annotated[str, Query(pattern="^(json|mermaid|dot)$")] = "json",
    direction: Annotated[str, Query(pattern="^(LR|RL|TB|TD|BT)$")] = "LR",
) -> dict[str, Any]:
    """Export a saved NodeMap canvas in `"json"`, `"mermaid"`, or `"dot"` format."""
    return _with_repo(nodemap_canvas_service.export_saved_canvas(canvas_id, fmt=format, direction=direction))


@router.post("/nodemap/preview", dependencies=[Depends(require_feature("nodemap"))])
def preview_nodemap_canvas(payload: NodemapPreviewRequest) -> dict[str, Any]:
    """Stateless read-only NodeMap canvas builder: normalize, hydrate, expand, lay out, and optionally export in memory."""
    ver = resolve_version(payload.version)
    data = payload.model_dump(by_alias=True, exclude_none=True)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(
        nodemap_canvas_service.preview_canvas(data, default_version=ver, default_repo=get_active_repo_name())
    )


@router.post(
    "/nodemap/canvases",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def create_nodemap_canvas(payload: NodemapCreateRequest) -> dict[str, Any]:
    """Create and persist a new NodeMap canvas document on disk."""
    ver = resolve_version(payload.version)
    data = payload.model_dump(by_alias=True, exclude_none=True)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(
        nodemap_canvas_service.create_canvas(
            data,
            default_version=ver,
            created_by=get_active_api_key_id() or "api",
        )
    )


@router.put(
    "/nodemap/canvases/{canvas_id}",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def replace_nodemap_canvas(canvas_id: str, payload: NodemapReplaceRequest) -> dict[str, Any]:
    """Replace a saved NodeMap canvas with optional `expected_revision` optimistic concurrency check."""
    ver = resolve_version(payload.version) if payload.version else resolve_default_version()
    data = payload.model_dump(by_alias=True, exclude_unset=True)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(
        nodemap_canvas_service.replace_canvas(
            canvas_id,
            data,
            default_version=ver,
            updated_by=get_active_api_key_id() or "api",
        )
    )


@router.delete(
    "/nodemap/canvases/{canvas_id}",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def delete_nodemap_canvas(canvas_id: str) -> dict[str, Any]:
    """Delete a saved NodeMap canvas JSON file."""
    return _with_repo(nodemap_canvas_service.delete_canvas(canvas_id))


@router.post(
    "/nodemap/canvases/{canvas_id}/ops",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def mutate_nodemap_canvas(canvas_id: str, payload: NodemapMutateRequest) -> dict[str, Any]:
    """Apply an atomic batch of declarative operations to a saved NodeMap canvas."""
    data = payload.model_dump(by_alias=True, exclude_none=True)
    if payload.version:
        data["version"] = resolve_version(payload.version)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(nodemap_canvas_service.mutate_canvas(canvas_id, data))


@router.post(
    "/nodemap/canvases/{canvas_id}/expand",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def expand_nodemap_graph(canvas_id: str, payload: NodemapExpandRequest) -> dict[str, Any]:
    """Expand a saved NodeMap canvas with server-side graph discovery (`struct_types`, `callgraph`, `callpath`, `kconfig`, `commit`, `impact_map`)."""
    data = payload.model_dump(by_alias=True, exclude_none=True)
    if payload.version:
        data["version"] = resolve_version(payload.version)
    if str(data.get("auto_layout") or "").strip().lower() in ("none", "false", "0", ""):
        data["auto_layout"] = None
    return _with_repo(nodemap_canvas_service.expand_canvas(canvas_id, data))


@router.post(
    "/nodemap/canvases/{canvas_id}/layout",
    dependencies=[Depends(require_feature("nodemap")), Depends(require_write_scope("nodemap:write"))],
)
def layout_nodemap_canvas(canvas_id: str, payload: NodemapLayoutRequest) -> dict[str, Any]:
    """Run deterministic Sugiyama `"dag"` or `"force"` layout over a saved NodeMap canvas."""
    return _with_repo(nodemap_canvas_service.layout_canvas(canvas_id, payload.model_dump(by_alias=True, exclude_none=True)))
