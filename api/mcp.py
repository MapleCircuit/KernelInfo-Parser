"""api/mcp.py - Zero-dependency Model Context Protocol (MCP) server over the curated `/api/v1/*` handlers.

One transport-agnostic JSON-RPC 2.0 dispatcher (`handle_payload()` / `handle_raw()`) serves both the
Streamable HTTP endpoint `POST /api/mcp` (`router`, mounted by `api/app.py` behind the full API middleware
stack) and the local stdio entrypoint `api/mcp_stdio.py`. The tool registry (`TOOLS`) is derived from the
`api/routers/v1.py` routes: names are the handler function names, `inputSchema` and argument validation come
from the same `Query`/`Path` constraints FastAPI enforces, feature dependencies (`require_feature`) run before
each call, and handlers execute in-process (no loopback HTTP). See `api/API.md` §5.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, get_args, get_origin

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.config import reset_active_repository, set_active_repository
from api.routers import v1 as v1_module
from api.security import has_write_scope

logger = logging.getLogger(__name__)

SERVER_NAME = "kernelinfo-parser"
SERVER_TITLE = "KernelInfo-Parser AI API"
SERVER_VERSION = "1.0.0"
MCP_PATH = "/api/mcp"
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
MAX_BODY_BYTES = 1 << 20
MAX_BATCH_SIZE = 16
MAX_REPO_NAME_LENGTH = 128

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
_ENVELOPE_ERRORS = (PARSE_ERROR, INVALID_REQUEST)

TOOL_ANNOTATIONS: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

SERVER_INSTRUCTIONS = (
    "Indexed kernel repository analysis: source, ASTs, symbols, call graphs, git history, Kconfig, "
    "Kernel API, LKML, maintainers, docs, man pages, CVEs, and NodeMap canvases. Call `get_capabilities` "
    "first for repos, features, and limits. Optional `repo` overrides the active repository; `version` "
    "defaults to the latest indexed mainline release."
)

_PARAM_DOCS: dict[str, str] = {
    "repo": "Repository name (see list_repos).",
    "version": "Git revision (default: active latest).",
    "path": "Repository-relative path.",
    "limit": "Max items to return.",
    "offset": "Pagination offset.",
    "arch": "Target architecture.",
    "q": "Search query.",
}

# Duplicate REST convenience aliases omitted from MCP inputSchema (preserved on REST /api/v1/*).
_MCP_OMITTED_PARAMS: dict[str, frozenset[str]] = {
    "resolve_file_target": frozenset({"from_file"}),
    "compare_struct_layout": frozenset({"struct", "a", "b"}),
    "get_struct_history": frozenset({"struct"}),
    "get_kconfig_diff": frozenset({"a", "b"}),
    "get_kapi_diff": frozenset({"a", "b"}),
    "get_man_diff": frozenset({"a", "b"}),
    "search_mail": frozenset({"page"}),
    "match_patch_maintainers": frozenset({"patch", "touched_files"}),
    "get_maintainer_person": frozenset({"email"}),
    "preview_nodemap_canvas": frozenset({"expand", "layout"}),
    "create_nodemap_canvas": frozenset({"expand", "layout"}),
    "replace_nodemap_canvas": frozenset({"layout"}),
    "mutate_nodemap_canvas": frozenset({"upsert_tour", "layout"}),
}

# Canonical parameters that are optional on REST only because aliases exist, required on MCP.
_MCP_REQUIRED_OVERRIDES: dict[str, frozenset[str]] = {
    "compare_struct_layout": frozenset({"name", "version_a", "version_b"}),
    "get_struct_history": frozenset({"name"}),
    "get_kconfig_diff": frozenset({"version_a", "version_b"}),
    "get_kapi_diff": frozenset({"version_a", "version_b"}),
    "get_man_diff": frozenset({"version_a", "version_b"}),
}

_MCP_PROJECTION_KEYS: frozenset[str] = frozenset({
    "verbose",
    "include_canvas",
    "include_endpoints",
    "include_raw",
    "include_headers",
})

_ESSENTIAL_MAIL_HEADERS: frozenset[str] = frozenset({
    "from",
    "to",
    "cc",
    "date",
    "subject",
    "message-id",
    "in-reply-to",
    "references",
})

_NODEMAP_STRICT_TOOLS: frozenset[str] = frozenset({
    "preview_nodemap_canvas",
    "create_nodemap_canvas",
    "replace_nodemap_canvas",
    "mutate_nodemap_canvas",
})

_EMPTY_NODE_UI_KEYS: frozenset[str] = frozenset({
    "color",
    "bgColor",
    "customTitle",
    "locks",
    "layerId",
    "hideHeader",
    "autoFit",
    "groupId",
})
_EMPTY_DATA_UI_KEYS: frozenset[str] = frozenset({
    "collapsed",
    "hiddenItems",
    "fieldLocks",
    "openMids",
    "fullMids",
    "pinnedBodies",
    "typeOverrides",
    "collapsedGuards",
})
_EMPTY_EDGE_UI_KEYS: frozenset[str] = frozenset({
    "color",
    "waypoints",
    "layerId",
    "lockKind",
    "fromField",
    "toField",
    "callSite",
})


class McpError(Exception):
    """JSON-RPC protocol error carrying a standard error code."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class McpTool:
    """One MCP tool bound to an `api/routers/v1.py` handler."""

    name: str
    title: str
    description: str
    rest_path: str
    feature: str | None
    endpoint: Callable[..., Any]
    model: type[BaseModel]
    input_schema: dict[str, Any]
    dependencies: tuple[Callable[[], Any], ...] = ()
    list_params: frozenset[str] = field(default_factory=frozenset)
    http_method: str = "GET"
    required_scope: str | None = None
    annotations: dict[str, bool] = field(default_factory=lambda: dict(TOOL_ANNOTATIONS))
    body_param_name: str | None = None
    body_model: type[BaseModel] | None = None
    direct_param_names: frozenset[str] = field(default_factory=frozenset)

    def describe(self) -> dict[str, Any]:
        """`tools/list` entry for this tool."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"title": self.title, **self.annotations},
        }


# ------------------------------------------------------------------ Tool registry


def _is_string_list_annotation(annotation: Any) -> bool:
    """True for `list[str]` and `list[str] | None` (never `list[dict]` or `list[BaseModel]`)."""
    if get_origin(annotation) is list:
        args = get_args(annotation)
        return not args or args[0] is str
    for arg in get_args(annotation):
        if get_origin(arg) is list:
            sub = get_args(arg)
            if not sub or sub[0] is str:
                return True
    return False


def _mcp_extra_fields(tool_name: str) -> dict[str, Any]:
    """MCP-only projection/compactness parameters added to specific tools."""
    extras: dict[str, Any] = {}
    if tool_name == "get_capabilities":
        extras["include_endpoints"] = (
            bool,
            Field(default=False, description="Include full REST endpoint catalog."),
        )
        extras["verbose"] = (
            bool,
            Field(default=False, description="Include full REST endpoint catalog."),
        )
    elif tool_name == "preview_nodemap_canvas":
        extras["include_canvas"] = (
            bool | None,
            Field(
                default=None,
                description="Include canvas JSON (default: false when export_format is set, true otherwise).",
            ),
        )
        extras["verbose"] = (
            bool,
            Field(default=False, description="Include empty/default UI-state keys on nodes and edges."),
        )
    elif tool_name == "get_mail_message":
        extras["include_raw"] = (
            bool,
            Field(default=False, description="Include raw RFC822 message text."),
        )
        extras["include_headers"] = (
            Literal["essential", "all"],
            Field(default="essential", description="Header set: 'essential' (default) or 'all'."),
        )
        extras["verbose"] = (
            bool,
            Field(default=False, description="Include all fields without compact projection."),
        )
    elif tool_name in ("get_kconfig_symbol", "get_nodemap_struct", "get_struct_layout", "get_tree"):
        extras["verbose"] = (
            bool,
            Field(default=False, description="Include null/false/0/empty defaults and redundant fields."),
        )
    return extras


def _deref_schema(node: Any, defs: dict[str, Any], seen: set[str] | None = None) -> Any:
    """Recursively inline/dereference Pydantic `$defs` / `$ref` pointers."""
    if not isinstance(node, dict):
        if isinstance(node, list):
            return [_deref_schema(item, defs, seen) for item in node]
        return node
    active_seen = set(seen or ())
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        ref_name = ref[len("#/$defs/") :]
        if ref_name in defs and ref_name not in active_seen:
            active_seen.add(ref_name)
            target = _deref_schema(dict(defs[ref_name]), defs, active_seen)
            overlay = {k: v for k, v in node.items() if k != "$ref"}
            return {**target, **_deref_schema(overlay, defs, active_seen)}
        return {"type": "object"}
    return {k: _deref_schema(v, defs, active_seen) for k, v in node.items() if k != "$defs"}


def _clean_property(prop: dict[str, Any], defs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drop pydantic titles, inline `$ref`, and collapse `anyOf: [X, null]` into `X`."""
    derefed = _deref_schema(prop, defs or {}) if defs else prop
    out = {k: v for k, v in derefed.items() if k not in ("title", "$defs", "$ref")}
    variants = out.get("anyOf")
    if isinstance(variants, list):
        non_null = [v for v in variants if v != {"type": "null"}]
        if non_null:
            out.pop("anyOf")
            primary = _clean_property(non_null[0], defs) if isinstance(non_null[0], dict) else {}
            out = {**primary, **out}
    if out.get("default", "") is None:
        out.pop("default")
    if "type" not in out:
        out["type"] = "object"
    if isinstance(out.get("items"), dict):
        cleaned_items = _clean_property(out["items"], defs) if out["items"] else {"type": "string"}
        out["items"] = cleaned_items
    elif out.get("type") == "array" and "items" not in out:
        out["items"] = {"type": "string"}
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: _clean_property(v, defs) for k, v in out["properties"].items()}
    if isinstance(out.get("additionalProperties"), dict):
        out["additionalProperties"] = _clean_property(out["additionalProperties"], defs)
    return out


def _annotations_for_route(http_method: str, required_scope: str | None, tool_name: str) -> dict[str, bool]:
    if required_scope is None:
        return dict(TOOL_ANNOTATIONS)
    is_delete = http_method == "DELETE"
    is_idempotent = http_method in ("PUT", "DELETE") or tool_name == "layout_nodemap_canvas"
    return {
        "readOnlyHint": False,
        "destructiveHint": is_delete,
        "idempotentHint": is_idempotent,
        "openWorldHint": False,
    }


def _build_tool(route: APIRoute, catalog: dict[tuple[str, str], dict[str, Any]], http_method: str) -> McpTool:
    endpoint = route.endpoint
    name = endpoint.__name__
    omitted = _MCP_OMITTED_PARAMS.get(name, frozenset())
    required_overrides = _MCP_REQUIRED_OVERRIDES.get(name, frozenset())
    extra_fields = _mcp_extra_fields(name)
    fields: dict[str, Any] = {}
    list_params: set[str] = set()
    direct_param_names: set[str] = set()

    for param in [*route.dependant.path_params, *route.dependant.query_params]:
        if param.name in omitted or param.alias in omitted:
            continue
        if param.name in extra_fields:
            continue
        info = param.field_info
        annotation = Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
        alias = param.alias if param.alias != param.name else None
        is_req = param.required or param.name in required_overrides
        if param.name in required_overrides and get_origin(info.annotation) is not None:
            non_none = [a for a in get_args(info.annotation) if a is not type(None)]
            if len(non_none) == 1:
                base_ann = non_none[0]
                meta = [m for m in info.metadata]
                if base_ann is str and not any(getattr(m, "min_length", None) for m in meta):
                    meta.append(Field(min_length=1))
                annotation = Annotated[(base_ann, *meta)] if meta else base_ann
        default = ... if is_req else param.default
        fields[param.name] = (annotation, Field(default, alias=alias, description=info.description))
        direct_param_names.add(param.name)
        if _is_string_list_annotation(info.annotation):
            list_params.add(param.alias)
            list_params.add(param.name)

    body_param_name: str | None = None
    body_model: type[BaseModel] | None = None
    for bparam in route.dependant.body_params:
        btype = bparam.field_info.annotation
        if isinstance(btype, type) and issubclass(btype, BaseModel):
            body_param_name = bparam.name
            body_model = btype
            for bfname, bfinfo in btype.model_fields.items():
                if bfname == "repo" or bfinfo.alias == "repo" or bfname in fields:
                    continue
                if bfname in omitted or (bfinfo.alias and bfinfo.alias in omitted):
                    continue
                b_annotation = Annotated[(bfinfo.annotation, *bfinfo.metadata)] if bfinfo.metadata else bfinfo.annotation
                b_alias = bfinfo.alias if bfinfo.alias and bfinfo.alias != bfname else None
                if bfinfo.is_required():
                    b_field = Field(..., alias=b_alias, description=bfinfo.description)
                elif bfinfo.default_factory is not None:
                    b_field = Field(default_factory=bfinfo.default_factory, alias=b_alias, description=bfinfo.description)
                else:
                    b_field = Field(default=bfinfo.default, alias=b_alias, description=bfinfo.description)
                fields[bfname] = (b_annotation, b_field)
                if _is_string_list_annotation(bfinfo.annotation):
                    list_params.add(bfname)
                    if b_alias:
                        list_params.add(b_alias)

    for efname, efspec in extra_fields.items():
        fields[efname] = efspec

    model = create_model(
        f"McpArgs_{name}",
        __config__=ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=True),
        **fields,
    )
    raw = model.model_json_schema(by_alias=True)
    defs = raw.get("$defs") if isinstance(raw.get("$defs"), dict) else {}
    properties = {key: _clean_property(value, defs) for key, value in (raw.get("properties") or {}).items()}
    for key, prop in properties.items():
        existing = prop.get("description")
        if not existing:
            doc = _PARAM_DOCS.get(key, "")
            if key in list_params:
                doc = f"{doc} Accepts a string or list of strings.".strip()
            if doc:
                prop["description"] = doc
        elif key in list_params and "string" not in existing.lower():
            prop["description"] = f"{existing} Accepts a string or list of strings."
    properties["repo"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_REPO_NAME_LENGTH,
        "description": _PARAM_DOCS["repo"],
    }
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if raw.get("required"):
        schema["required"] = [r for r in raw["required"] if r != "repo"]

    deps: list[Callable[[], Any]] = []
    required_scope: str | None = None
    for dep in route.dependant.dependencies:
        if dep.path_params or dep.query_params or dep.header_params or dep.dependencies:
            raise RuntimeError(f"MCP tool {name}: unsupported parameterized dependency {dep.call!r}")
        deps.append(dep.call)
        dep_scope = getattr(dep.call, "__kip_required_scope__", None)
        if isinstance(dep_scope, str) and dep_scope:
            required_scope = dep_scope

    entry = catalog.get((http_method, route.path), {})
    if not required_scope and isinstance(entry.get("scope"), str):
        required_scope = entry["scope"]
    feature = entry.get("feature")
    summary = entry.get("summary") or name.replace("_", " ")
    doc = " ".join((endpoint.__doc__ or summary).split())
    description = f"{doc} Requires `{feature}` repository feature." if feature else doc
    annotations = _annotations_for_route(http_method, required_scope, name)

    return McpTool(
        name=name,
        title=summary,
        description=description,
        rest_path=route.path,
        feature=feature,
        endpoint=endpoint,
        model=model,
        input_schema=schema,
        dependencies=tuple(deps),
        list_params=frozenset(list_params),
        http_method=http_method,
        required_scope=required_scope,
        annotations=annotations,
        body_param_name=body_param_name,
        body_model=body_model,
        direct_param_names=frozenset(direct_param_names),
    )


def build_tool_registry(routes: list[Any] | None = None) -> dict[str, McpTool]:
    """Build the tool registry from `/api/v1/*` GET routes (unless `mcp=False`) and explicitly marked POST/PUT/DELETE routes."""
    catalog = {(e["method"], e["path"]): e for e in v1_module._ENDPOINT_CATALOG}
    tools: dict[str, McpTool] = {}
    for route in routes if routes is not None else v1_module.router.routes:
        if not isinstance(route, APIRoute):
            continue
        method: str | None = None
        if "GET" in route.methods:
            entry = catalog.get(("GET", route.path), {})
            if entry.get("mcp", True) is not False:
                method = "GET"
        else:
            for candidate in ("POST", "PUT", "DELETE"):
                if candidate in route.methods:
                    entry = catalog.get((candidate, route.path), {})
                    if entry.get("mcp") is False:
                        continue
                    has_scope_dep = any(getattr(d.call, "__kip_required_scope__", None) for d in route.dependant.dependencies)
                    if entry.get("mcp") or has_scope_dep:
                        method = candidate
                        break
        if method is None:
            continue
        tool = _build_tool(route, catalog, method)
        if tool.name in tools:
            raise RuntimeError(f"Duplicate MCP tool name: {tool.name}")
        tools[tool.name] = tool
    return tools


TOOLS: dict[str, McpTool] = build_tool_registry()


# ------------------------------------------------------------------ Tool execution


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _tool_error(status: int, detail: Any) -> dict[str, Any]:
    text = detail if isinstance(detail, str) else _dump(detail)
    return {
        "content": [{"type": "text", "text": f"HTTP {status}: {text}"}],
        "structuredContent": {"error": {"status": status, "detail": jsonable_encoder(detail)}},
        "isError": True,
    }


def _tool_success(value: Any) -> dict[str, Any]:
    payload = jsonable_encoder(value)
    structured = payload if isinstance(payload, dict) else {"result": payload}
    return {"content": [{"type": "text", "text": _dump(payload)}], "structuredContent": structured, "isError": False}


def _response_result(resp: Response) -> dict[str, Any]:
    """Translate a handler `Response` (e.g. `job_answer()`'s `202` building state) into a tool result."""
    raw = bytes(resp.body or b"")
    try:
        body: Any = json.loads(raw) if raw else {}
    except ValueError:
        body = raw.decode("utf-8", errors="replace")
    if resp.status_code >= 400:
        detail = body.get("detail", body) if isinstance(body, dict) else body
        return _tool_error(resp.status_code, detail)
    return _tool_success(body)


def _validation_detail(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def _compact_nodemap_canvas(canvas: dict[str, Any]) -> dict[str, Any]:
    """Strip empty/default UI-state keys from NodeMap nodes/edges (and nested subCanvases)."""
    out = copy.deepcopy(canvas)
    nodes = out.get("nodes")
    if isinstance(nodes, list):
        cleaned_nodes: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                cleaned_nodes.append(node)
                continue
            cnode = {
                k: v
                for k, v in node.items()
                if not (k in _EMPTY_NODE_UI_KEYS and (v in ("", [], {}, False, None)))
            }
            data = cnode.get("data")
            if isinstance(data, dict):
                cdata = {
                    k: v
                    for k, v in data.items()
                    if not (k in _EMPTY_DATA_UI_KEYS and (v in ("", [], {})))
                }
                if isinstance(cdata.get("subCanvas"), dict):
                    cdata["subCanvas"] = _compact_nodemap_canvas(cdata["subCanvas"])
                cnode["data"] = cdata
            cleaned_nodes.append(cnode)
        out["nodes"] = cleaned_nodes
    edges = out.get("edges")
    if isinstance(edges, list):
        out["edges"] = [
            {
                k: v
                for k, v in edge.items()
                if not (k in _EMPTY_EDGE_UI_KEYS and (v in ("", [], {}, None)))
            }
            if isinstance(edge, dict)
            else edge
            for edge in edges
        ]
    return out


def _compact_nodemap_struct_items(items: list[Any]) -> list[Any]:
    """Strip null, empty-list, and 0-default per-item fields from `get_nodemap_struct` items."""
    out: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        c = dict(item)
        if c.get("bits") is None:
            c.pop("bits", None)
        if c.get("array") is None:
            c.pop("array", None)
        if c.get("conds") == []:
            c.pop("conds", None)
        if c.get("expr") in (None, ""):
            c.pop("expr", None)
        if c.get("depth") == 0:
            c.pop("depth", None)
        if c.get("kind") == "aggregate":
            if c.get("name") == "":
                c.pop("name", None)
            if c.get("type_name") == "":
                c.pop("type_name", None)
            if isinstance(c.get("children"), list):
                c["children"] = _compact_nodemap_struct_items(c["children"])
        out.append(c)
    return out


def _project_mcp_result(
    name: str,
    result: Any,
    proj_opts: dict[str, Any],
    input_args: dict[str, Any],
) -> Any:
    """Apply MCP-only compact payload projections while keeping REST responses untouched."""
    if not isinstance(result, dict):
        return result
    verbose = bool(proj_opts.get("verbose"))

    if name == "preview_nodemap_canvas":
        out = dict(result)
        out.pop("export_content", None)
        include_canvas = proj_opts.get("include_canvas")
        if include_canvas is None:
            include_canvas = not bool(input_args.get("export_format"))
        if not include_canvas:
            out.pop("canvas", None)
        elif not verbose and isinstance(out.get("canvas"), dict):
            out["canvas"] = _compact_nodemap_canvas(out["canvas"])
        return out

    if verbose:
        return result

    if name == "get_mail_message":
        out = dict(result)
        if not proj_opts.get("include_raw"):
            out.pop("raw", None)
        if proj_opts.get("include_headers", "essential") != "all" and isinstance(out.get("headers"), dict):
            out["headers"] = {
                k: v for k, v in out["headers"].items() if str(k).lower() in _ESSENTIAL_MAIL_HEADERS
            }
        if "commit_message" in out and (out["commit_message"] is None or out["commit_message"] == out.get("body")):
            out.pop("commit_message", None)
        if "review_trailers" in out and "trailers" in out and out["review_trailers"] == out["trailers"]:
            out.pop("review_trailers", None)
        return out

    if name == "get_kconfig_symbol":
        out = dict(result)
        top_help = out.get("help")
        top_prompt = out.get("prompt")
        if isinstance(out.get("definition"), dict):
            defn = dict(out["definition"])
            if defn.get("help") == top_help:
                defn.pop("help", None)
            if defn.get("prompt") == top_prompt:
                defn.pop("prompt", None)
            out["definition"] = defn
        if isinstance(out.get("definitions"), list):
            new_defs: list[Any] = []
            multi_defs = len(out["definitions"]) > 1
            for d in out["definitions"]:
                if isinstance(d, dict):
                    cd = dict(d)
                    if cd.get("help") == top_help:
                        cd.pop("help", None)
                    if not multi_defs and cd.get("prompt") == top_prompt:
                        cd.pop("prompt", None)
                    new_defs.append(cd)
                else:
                    new_defs.append(d)
            out["definitions"] = new_defs
        prompts = out.get("prompts")
        if isinstance(prompts, list) and (not prompts or (top_prompt is not None and prompts == [top_prompt])):
            out.pop("prompts", None)
        declarations = out.get("declarations")
        if (
            isinstance(declarations, list)
            and isinstance(out.get("definitions"), list)
            and len(declarations) == len(out["definitions"])
            and all(
                isinstance(d, dict)
                and isinstance(x, dict)
                and (d.get("file_path"), d.get("line_s"), d.get("line_e"))
                == (x.get("file_path"), x.get("line_s"), x.get("line_e"))
                for d, x in zip(out["definitions"], declarations)
            )
        ):
            out.pop("declarations", None)
        if (
            isinstance(out.get("kbuild"), list)
            and isinstance(out.get("compiled_files"), list)
        ):
            kb_paths = {r.get("path") for r in out["kbuild"] if isinstance(r, dict)}
            if all(isinstance(cf, dict) and cf.get("file_path") in kb_paths for cf in out["compiled_files"]):
                out.pop("compiled_files", None)
        return out

    if name == "get_nodemap_struct":
        out = dict(result)
        if out.get("outer_conds") == []:
            out.pop("outer_conds", None)
        if isinstance(out.get("items"), list):
            out["items"] = _compact_nodemap_struct_items(out["items"])
        return out

    if name == "get_struct_layout":
        out = dict(result)
        if isinstance(out.get("members"), list):
            compact_members: list[Any] = []
            for m in out["members"]:
                if not isinstance(m, dict):
                    compact_members.append(m)
                    continue
                cm = dict(m)
                if cm.get("estimated") is False:
                    cm.pop("estimated", None)
                if cm.get("padding_before") == 0:
                    cm.pop("padding_before", None)
                if cm.get("crosses_cacheline") is False:
                    cm.pop("crosses_cacheline", None)
                if cm.get("bitfield_width") is None:
                    cm.pop("bitfield_width", None)
                if cm.get("bit_offset") is None:
                    cm.pop("bit_offset", None)
                compact_members.append(cm)
            out["members"] = compact_members
        return out

    if name == "get_tree":
        out = dict(result)
        if isinstance(out.get("entries"), list):
            compact_entries: list[Any] = []
            for e in out["entries"]:
                if not isinstance(e, dict):
                    compact_entries.append(e)
                    continue
                ce = dict(e)
                ce.pop("fid", None)
                if ce.get("ftype") in (None, "", 1, "1", 7, "7"):
                    ce.pop("ftype", None)
                if ce.get("s_stat") in (None, "", "0"):
                    ce.pop("s_stat", None)
                    ce.pop("s_stat_label", None)
                if ce.get("e_stat") in (None, "", "0"):
                    ce.pop("e_stat", None)
                compact_entries.append(ce)
            out["entries"] = compact_entries
        return out

    return result


def call_tool(name: Any, arguments: Any) -> dict[str, Any]:
    """Validate `arguments`, select the repository, and run one tool; handler errors become `isError` results.

    Unknown tools and non-object arguments raise `McpError(INVALID_PARAMS)` (protocol errors).
    """
    if not isinstance(name, str) or name not in TOOLS:
        raise McpError(INVALID_PARAMS, f"Unknown tool: {str(name)[:128]!r}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise McpError(INVALID_PARAMS, "Tool arguments must be a JSON object.")
    tool = TOOLS[name]
    args = dict(arguments)
    repo = args.pop("repo", None)
    if repo is not None and (not isinstance(repo, str) or not repo.strip() or len(repo) > MAX_REPO_NAME_LENGTH):
        return _tool_error(422, [{"loc": ["repo"], "msg": "repo must be a non-empty repository name", "type": "string_type"}])
    for key in tool.list_params:
        if isinstance(args.get(key), str):
            args[key] = [args[key]]

    if name in _NODEMAP_STRICT_TOOLS:
        from api.routers.nodemap import validate_nodemap_payload_strict

        strict_errors = validate_nodemap_payload_strict(args)
        if strict_errors:
            return _tool_error(422, strict_errors)

    try:
        validated = tool.model.model_validate(args)
    except ValidationError as exc:
        return _tool_error(422, _validation_detail(exc))

    proj_opts = {
        key: getattr(validated, key)
        for key in _MCP_PROJECTION_KEYS
        if hasattr(validated, key)
    }

    if tool.body_param_name and tool.body_model is not None:
        kwargs = {key: getattr(validated, key) for key in tool.direct_param_names if hasattr(validated, key)}
        set_fields = getattr(validated, "model_fields_set", set())
        body_data = {
            key: getattr(validated, key)
            for key in tool.body_model.model_fields
            if hasattr(validated, key) and (key in set_fields or getattr(validated, key) is not None)
        }
        try:
            kwargs[tool.body_param_name] = tool.body_model.model_validate(body_data)
        except ValidationError as exc:
            return _tool_error(422, _validation_detail(exc))
    else:
        kwargs = {
            key: getattr(validated, key)
            for key in type(validated).model_fields
            if key not in _MCP_PROJECTION_KEYS
        }
        if name == "get_capabilities":
            kwargs["include_endpoints"] = bool(proj_opts.get("include_endpoints") or proj_opts.get("verbose"))

    token = None
    repo_name = (repo or "").strip()
    if repo is not None:
        token = set_active_repository(repo_name)
        if token is None:
            return _tool_error(404, f"Unknown repository: {repo_name[:64]!r} (see list_repos)")
    try:
        for dep in tool.dependencies:
            dep()
        result = tool.endpoint(**kwargs)
        # If a real background job is actively building in `jobs`, wait up to 15s for it to finish
        # so AI agents calling MCP over HTTP do not spin in a tight polling loop burning tool-call turns.
        if isinstance(result, Response) and result.status_code == 202:
            try:
                from webapp.backend.services.jobs import jobs as _job_mgr

                deadline = time.monotonic() + 15.0
                while (
                    isinstance(result, Response)
                    and result.status_code == 202
                    and time.monotonic() < deadline
                ):
                    with _job_mgr._lock:
                        has_active_jobs = bool(_job_mgr._jobs)
                    if not has_active_jobs:
                        break
                    time.sleep(0.5)
                    result = tool.endpoint(**kwargs)
            except Exception:  # noqa: BLE001
                pass
    except StarletteHTTPException as exc:
        return _tool_error(exc.status_code, exc.detail)
    except Exception:  # noqa: BLE001 - a failing handler is a tool error, never a transport crash
        logger.exception("MCP tool %s failed", name)
        return _tool_error(500, "Internal server error")
    finally:
        reset_active_repository(token)
    if isinstance(result, Response):
        return _response_result(result)
    result = _project_mcp_result(name, result, proj_opts, args)
    out = _tool_success(result)
    if (
        name in ("search_mail", "get_mail_message", "get_mail_thread")
        and isinstance(result, dict)
        and (result.get("available") is False or bool(result.get("error")))
    ):
        out["isError"] = True
    return out


# ------------------------------------------------------------------ JSON-RPC dispatcher


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def negotiate_protocol_version(requested: Any) -> str:
    """Echo a supported client version, otherwise answer with the latest supported version."""
    return requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    if requested is not None and not isinstance(requested, str):
        raise McpError(INVALID_PARAMS, "protocolVersion must be a string.")
    return {
        "protocolVersion": negotiate_protocol_version(requested),
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION},
        "instructions": SERVER_INSTRUCTIONS,
    }


def visible_tools(scopes: frozenset[str] | None = None) -> list[McpTool]:
    """Return tools visible to the current caller (write-scoped tools hidden when disallowed)."""
    return [
        tool
        for tool in TOOLS.values()
        if tool.required_scope is None or has_write_scope(tool.required_scope, scopes)
    ]


def _tools_list(params: dict[str, Any]) -> dict[str, Any]:
    if params.get("cursor") not in (None, ""):
        raise McpError(INVALID_PARAMS, "Invalid cursor: this server returns every tool in one page.")
    return {"tools": [tool.describe() for tool in visible_tools()]}


def _tools_call(params: dict[str, Any]) -> dict[str, Any]:
    if "name" not in params:
        raise McpError(INVALID_PARAMS, "tools/call requires `name`.")
    return call_tool(params.get("name"), params.get("arguments"))


_METHODS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "initialize": _initialize,
    "ping": lambda _params: {},
    "tools/list": _tools_list,
    "tools/call": _tools_call,
}


def handle_message(msg: Any) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message; returns the response, or None for notifications and client responses."""
    if not isinstance(msg, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: expected a JSON-RPC 2.0 object.")
    has_id = "id" in msg
    msg_id = msg.get("id") if has_id and _valid_id(msg.get("id")) else None
    if msg.get("jsonrpc") != "2.0":
        return _error(msg_id, INVALID_REQUEST, "Invalid Request: jsonrpc must be \"2.0\".")
    if "method" not in msg:
        if has_id and ("result" in msg or "error" in msg):
            return None  # a client response; this server sends no requests
        return _error(msg_id, INVALID_REQUEST, "Invalid Request: missing method.")
    method = msg["method"]
    if not isinstance(method, str) or not method:
        return _error(msg_id, INVALID_REQUEST, "Invalid Request: method must be a non-empty string.")
    if has_id and not _valid_id(msg["id"]):
        return _error(None, INVALID_REQUEST, "Invalid Request: id must be a string or integer.")
    params = msg.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        return _error(msg_id, INVALID_REQUEST, "Invalid Request: params must be an object.")
    if not has_id:
        return None  # notifications (initialized, cancelled, progress, ...) need no reply in a stateless server
    if isinstance(params, list):
        return _error(msg_id, INVALID_PARAMS, "Invalid params: params must be an object.")
    handler = _METHODS.get(method)
    if handler is None:
        return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method[:128]}")
    try:
        return _result(msg_id, handler(params or {}))
    except McpError as exc:
        return _error(msg_id, exc.code, exc.message, exc.data)
    except Exception:  # noqa: BLE001 - keep the transport alive
        logger.exception("MCP method %s failed", method)
        return _error(msg_id, INTERNAL_ERROR, "Internal error")


def handle_payload(payload: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Dispatch a decoded message or JSON-RPC batch.

    A batch holds at most `MAX_BATCH_SIZE` messages and at most one `tools/call`, so one authenticated
    HTTP request (one rate-limit hit) never runs more than one tool.
    """
    if isinstance(payload, list):
        if not payload:
            return _error(None, INVALID_REQUEST, "Invalid Request: empty batch.")
        if len(payload) > MAX_BATCH_SIZE:
            return _error(None, INVALID_REQUEST, f"Invalid Request: batch exceeds {MAX_BATCH_SIZE} messages.")
        if sum(1 for m in payload if isinstance(m, dict) and m.get("method") == "tools/call") > 1:
            return _error(None, INVALID_REQUEST, "Invalid Request: at most one tools/call per batch.")
        responses = [r for r in (handle_message(m) for m in payload) if r is not None]
        return responses or None
    return handle_message(payload)


_UNPARSABLE = object()


def _decode_json(raw: str | bytes) -> Any:
    """Decoded JSON, or `_UNPARSABLE` for malformed, non-UTF, or too deeply nested input."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return _UNPARSABLE


def handle_raw(raw: str | bytes) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Decode one JSON text and dispatch it (`-32700` on malformed JSON)."""
    payload = _decode_json(raw)
    if payload is _UNPARSABLE:
        return _error(None, PARSE_ERROR, "Parse error: invalid JSON.")
    return handle_payload(payload)


def is_envelope_error(response: Any) -> bool:
    """True for a parse / invalid-request error answering an unusable message (HTTP 400)."""
    return (
        isinstance(response, dict)
        and response.get("id") is None
        and isinstance(response.get("error"), dict)
        and response["error"].get("code") in _ENVELOPE_ERRORS
    )


# ------------------------------------------------------------------ Streamable HTTP transport

router = APIRouter()
_NOT_ALLOWED_METHODS = ["GET", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"]


async def _read_body(request: Request) -> bytes | None:
    """Request body, or None once it exceeds `MAX_BODY_BYTES`."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _summarize_mcp_call(payload: Any) -> str:
    if isinstance(payload, dict):
        method = str(payload.get("method") or "?")
        if method == "tools/call" and isinstance(payload.get("params"), dict):
            tname = payload["params"].get("name", "?")
            targs = payload["params"].get("arguments")
            arg_str = json.dumps(targs, ensure_ascii=False)[:160] if isinstance(targs, dict) else ""
            return f"tools/call {tname}({arg_str})"
        return method
    if isinstance(payload, list):
        return f"batch[{len(payload)}]"
    return "invalid"


@router.post(MCP_PATH, include_in_schema=False)
async def mcp_post(request: Request) -> Response:
    """Stateless Streamable HTTP: one JSON-RPC message (or batch) in, one `application/json` reply out."""
    header_version = request.headers.get("mcp-protocol-version")
    if header_version and header_version not in SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(
            _error(None, INVALID_REQUEST, f"Unsupported MCP-Protocol-Version: {header_version[:32]}"),
            status_code=400,
        )
    body = await _read_body(request)
    if body is None:
        return JSONResponse(_error(None, INVALID_REQUEST, "Request body too large."), status_code=413)
    payload = _decode_json(body)
    if payload is _UNPARSABLE:
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error: invalid JSON."), status_code=400)
    summary = _summarize_mcp_call(payload)
    started = time.monotonic()
    response = await run_in_threadpool(handle_payload, payload)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if response is None:
        logger.info("MCP %s -> 202 (%.1f ms)", summary, elapsed_ms)
        return Response(status_code=202)
    is_err = (
        isinstance(response, dict)
        and (
            "error" in response
            or (isinstance(response.get("result"), dict) and response["result"].get("isError"))
        )
    )
    logger.info("MCP %s -> %s (%.1f ms)", summary, "error" if is_err else "ok", elapsed_ms)
    return JSONResponse(response, status_code=400 if is_envelope_error(response) else 200)


@router.api_route(MCP_PATH, methods=_NOT_ALLOWED_METHODS, include_in_schema=False)
async def mcp_method_not_allowed() -> Response:
    """No SSE stream and no sessions: only POST is served."""
    return JSONResponse(
        _error(None, -32000, "Method not allowed: stateless MCP server, use POST."),
        status_code=405,
        headers={"Allow": "POST"},
    )


__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "MCP_PATH",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOOLS",
    "McpError",
    "McpTool",
    "build_tool_registry",
    "call_tool",
    "handle_message",
    "handle_payload",
    "handle_raw",
    "is_envelope_error",
    "negotiate_protocol_version",
    "router",
]
