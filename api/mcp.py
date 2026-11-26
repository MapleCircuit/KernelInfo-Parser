"""api/mcp.py - Zero-dependency Model Context Protocol (MCP) server over the curated `/api/v1/*` handlers.

One transport-agnostic JSON-RPC 2.0 dispatcher (`handle_payload()` / `handle_raw()`) serves both the
Streamable HTTP endpoint `POST /api/mcp` (`router`, mounted by `api/app.py` behind the full API middleware
stack) and the local stdio entrypoint `api/mcp_stdio.py`. The tool registry (`TOOLS`) is derived from the
`api/routers/v1.py` routes: names are the handler function names, `inputSchema` and argument validation come
from the same `Query`/`Path` constraints FastAPI enforces, feature dependencies (`require_feature`) run before
each call, and handlers execute in-process (no loopback HTTP). See `api/API.md` §5.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, get_args, get_origin

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.config import reset_active_repository, set_active_repository
from api.routers import v1 as v1_module

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
    "Read-only introspection of indexed kernel repositories: source files, ASTs, symbols, call graphs, "
    "git history, Kconfig, maintainers, docs, man pages, and CVEs. Call get_capabilities first for the "
    "repositories, enabled features, and limits. Every tool takes an optional `repo` (see list_repos); "
    "`version` defaults to the service default revision. Paginated tools return `total` and `truncated`; "
    "page with `limit`/`offset`."
)

_PARAM_DOCS: dict[str, str] = {
    "repo": "Repository name (list_repos); defaults to the request or session repository.",
    "version": "Git revision (tag, branch, or commit); defaults to the service default version.",
    "path": "Repository-relative path.",
    "limit": "Maximum number of items to return.",
    "offset": "Number of items to skip (pagination).",
    "arch": "Target architecture.",
    "q": "Search query.",
    "from": "Starting (caller) function name.",
    "to": "Target (callee) function name.",
}


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

    def describe(self) -> dict[str, Any]:
        """`tools/list` entry for this tool."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"title": self.title, **TOOL_ANNOTATIONS},
        }


# ------------------------------------------------------------------ Tool registry


def _is_list_annotation(annotation: Any) -> bool:
    """True for `list[X]` and `list[X] | None`."""
    if get_origin(annotation) is list:
        return True
    return any(get_origin(arg) is list for arg in get_args(annotation))


def _clean_property(prop: dict[str, Any]) -> dict[str, Any]:
    """Drop pydantic titles and collapse `anyOf: [X, null]` into `X` (an omitted argument means null)."""
    out = {k: v for k, v in prop.items() if k != "title"}
    variants = out.get("anyOf")
    if isinstance(variants, list):
        non_null = [v for v in variants if v != {"type": "null"}]
        if len(non_null) == 1 and len(non_null) < len(variants):
            out.pop("anyOf")
            out = {**non_null[0], **out}
    if out.get("default", "") is None:
        out.pop("default")
    if isinstance(out.get("items"), dict):
        out["items"] = _clean_property(out["items"])
    return out


def _build_tool(route: APIRoute, catalog: dict[str, dict[str, Any]]) -> McpTool:
    endpoint = route.endpoint
    name = endpoint.__name__
    fields: dict[str, Any] = {}
    list_params: set[str] = set()
    for param in [*route.dependant.path_params, *route.dependant.query_params]:
        info = param.field_info
        annotation = Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
        alias = param.alias if param.alias != param.name else None
        default = ... if param.required else param.default
        fields[param.name] = (annotation, Field(default, alias=alias))
        if _is_list_annotation(info.annotation):
            list_params.add(param.alias)
    model = create_model(
        f"McpArgs_{name}",
        __config__=ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=True),
        **fields,
    )
    raw = model.model_json_schema(by_alias=True)
    properties = {key: _clean_property(value) for key, value in (raw.get("properties") or {}).items()}
    for key, prop in properties.items():
        doc = _PARAM_DOCS.get(key, "")
        if key in list_params:
            doc = f"{doc} List of values; a single string counts as a one-item list.".strip()
        if doc:
            prop.setdefault("description", doc)
    properties["repo"] = {"type": "string", "maxLength": MAX_REPO_NAME_LENGTH, "description": _PARAM_DOCS["repo"]}
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if raw.get("required"):
        schema["required"] = list(raw["required"])

    deps: list[Callable[[], Any]] = []
    for dep in route.dependant.dependencies:
        if dep.path_params or dep.query_params or dep.header_params or dep.dependencies:
            raise RuntimeError(f"MCP tool {name}: unsupported parameterized dependency {dep.call!r}")
        deps.append(dep.call)

    entry = catalog.get(route.path, {})
    feature = entry.get("feature")
    summary = entry.get("summary") or name.replace("_", " ")
    doc = " ".join((endpoint.__doc__ or summary).split())
    description = f"{doc} (REST twin: GET {route.path}"
    description += f"; requires the `{feature}` repository feature)" if feature else ")"
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
    )


def build_tool_registry(routes: list[Any] | None = None) -> dict[str, McpTool]:
    """Build the tool registry from the `/api/v1/*` GET routes (one tool per handler, in route order)."""
    catalog = {e["path"]: e for e in v1_module._ENDPOINT_CATALOG}
    tools: dict[str, McpTool] = {}
    for route in routes if routes is not None else v1_module.router.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        tool = _build_tool(route, catalog)
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
    try:
        validated = tool.model.model_validate(args)
    except ValidationError as exc:
        return _tool_error(422, _validation_detail(exc))
    kwargs = {key: getattr(validated, key) for key in type(validated).model_fields}

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
    except StarletteHTTPException as exc:
        return _tool_error(exc.status_code, exc.detail)
    except Exception:  # noqa: BLE001 - a failing handler is a tool error, never a transport crash
        logger.exception("MCP tool %s failed", name)
        return _tool_error(500, "Internal server error")
    finally:
        reset_active_repository(token)
    if isinstance(result, Response):
        return _response_result(result)
    return _tool_success(result)


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


def _tools_list(params: dict[str, Any]) -> dict[str, Any]:
    if params.get("cursor") not in (None, ""):
        raise McpError(INVALID_PARAMS, "Invalid cursor: this server returns every tool in one page.")
    return {"tools": [tool.describe() for tool in TOOLS.values()]}


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
    response = await run_in_threadpool(handle_payload, payload)
    if response is None:
        return Response(status_code=202)
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
