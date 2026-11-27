"""tests/test_ai_mcp.py - MCP JSON-RPC 2.0 server (`api/mcp.py`, `POST /api/mcp`, `api/mcp_stdio.py`) tests."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import mcp, mcp_stdio
from api.app import create_app
from api.config import get_active_repo_name
from api.routers import v1 as v1_router
from core import config as core_config
from tests import test_ai_api as _rest_tests

REPO_ROOT = Path(__file__).resolve().parent.parent
_ALLOWED_TYPES = {"string", "integer", "boolean", "array", "number", "object"}


def _v1_get_routes() -> list[APIRoute]:
    return [r for r in v1_router.router.routes if isinstance(r, APIRoute) and "GET" in r.methods]


def _v1_readonly_tool_routes() -> list[APIRoute]:
    readonly_pairs = {
        (e["method"], e["path"])
        for e in v1_router._ENDPOINT_CATALOG
        if e.get("mcp", e["method"] == "GET") and not e.get("scope")
    }
    return [
        r
        for r in v1_router.router.routes
        if isinstance(r, APIRoute) and any((m, r.path) in readonly_pairs for m in r.methods)
    ]


class _McpFixture(_rest_tests.TestAiApiEndpoints):
    """Reuses the `/api/v1/*` git + MockDB fixture; the inherited REST tests are not collected here."""

    def rpc(self, method: str, params: dict | None = None, *, msg_id=1, headers: dict | None = None):
        body: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post(mcp.MCP_PATH, json=body, headers={**self.auth, **(headers or {})})

    def call(self, name: str, arguments: dict | None = None, *, headers: dict | None = None) -> dict:
        r = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertNotIn("error", body, body)
        return body["result"]

    def assertToolOk(self, result: dict) -> dict:
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    def assertToolError(self, result: dict, status: int) -> dict:
        self.assertTrue(result["isError"], result)
        err = result["structuredContent"]["error"]
        self.assertEqual(err["status"], status, result)
        self.assertTrue(result["content"][0]["text"].startswith(f"HTTP {status}:"))
        return err


# Keep the inherited REST tests out of this module's test collection.
for _name in [n for n in dir(_rest_tests.TestAiApiEndpoints) if n.startswith("test_")]:
    setattr(_McpFixture, _name, None)


class TestMcpProtocol(_McpFixture):
    def test_initialize_negotiation_notifications_and_ping(self) -> None:
        for requested, expected in (
            ("2025-03-26", "2025-03-26"),
            ("2024-11-05", "2024-11-05"),
            ("2025-06-18", "2025-06-18"),
            ("1999-01-01", mcp.LATEST_PROTOCOL_VERSION),
            (None, mcp.LATEST_PROTOCOL_VERSION),
        ):
            params = {"capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}
            if requested:
                params["protocolVersion"] = requested
            r = self.rpc("initialize", params)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "application/json")
            res = r.json()["result"]
            self.assertEqual(res["protocolVersion"], expected)
            self.assertEqual(res["capabilities"], {"tools": {"listChanged": False}})
            self.assertEqual(res["serverInfo"]["name"], mcp.SERVER_NAME)
            self.assertIn("get_capabilities", res["instructions"])
            self.assertNotIn("mcp-session-id", {k.lower() for k in r.headers})

        bad = self.rpc("initialize", {"protocolVersion": 20250326})
        self.assertEqual(bad.json()["error"]["code"], mcp.INVALID_PARAMS)

        for note in ("notifications/initialized", "notifications/cancelled", "notifications/progress"):
            r = self.client.post(
                mcp.MCP_PATH, json={"jsonrpc": "2.0", "method": note, "params": {"requestId": 1}}, headers=self.auth
            )
            self.assertEqual(r.status_code, 202, note)
            self.assertEqual(r.content, b"")

        # A notification never executes or answers, even for a request-only method
        with patch.object(mcp, "call_tool") as call_tool:
            r = self.client.post(
                mcp.MCP_PATH, json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "list_repos"}}, headers=self.auth
            )
            self.assertEqual(r.status_code, 202)
            call_tool.assert_not_called()

        r_ping = self.rpc("ping", msg_id="abc")
        self.assertEqual(r_ping.json(), {"jsonrpc": "2.0", "id": "abc", "result": {}})

        # Client responses (to server requests this server never sends) are accepted silently
        r_resp = self.client.post(mcp.MCP_PATH, json={"jsonrpc": "2.0", "id": 9, "result": {}}, headers=self.auth)
        self.assertEqual(r_resp.status_code, 202)

    def test_protocol_version_header(self) -> None:
        ok = self.rpc("ping", headers={"MCP-Protocol-Version": "2025-03-26"})
        self.assertEqual(ok.status_code, 200)
        bad = self.rpc("ping", headers={"MCP-Protocol-Version": "1.0"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], mcp.INVALID_REQUEST)

    def test_jsonrpc_envelope_errors(self) -> None:
        post = lambda content: self.client.post(  # noqa: E731
            mcp.MCP_PATH, content=content, headers={**self.auth, "Content-Type": "application/json"}
        )
        r = post(b"{not json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error: invalid JSON."}})
        self.assertEqual(post(b"\xff\xfe\x00garbage").json()["error"]["code"], mcp.PARSE_ERROR)
        nested = post(b"[" * 200_000)  # RecursionError in the decoder is a parse error, not a 500
        self.assertEqual(nested.status_code, 400)
        self.assertEqual(nested.json()["error"]["code"], mcp.PARSE_ERROR)

        for payload, code, status in (
            ({"id": 1, "method": "ping"}, mcp.INVALID_REQUEST, 200),  # id is usable -> plain JSON-RPC error
            ({"jsonrpc": "1.0", "method": "ping"}, mcp.INVALID_REQUEST, 400),
            ({"jsonrpc": "2.0", "id": None, "method": "ping"}, mcp.INVALID_REQUEST, 400),
            ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, mcp.INVALID_REQUEST, 400),
            ({"jsonrpc": "2.0", "id": True, "method": "ping"}, mcp.INVALID_REQUEST, 400),
            ({"jsonrpc": "2.0", "id": 1}, mcp.INVALID_REQUEST, 200),
            ({"jsonrpc": "2.0", "id": 1, "method": 5}, mcp.INVALID_REQUEST, 200),
            ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": "x"}, mcp.INVALID_REQUEST, 200),
            ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}, mcp.INVALID_PARAMS, 200),
            ({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, mcp.METHOD_NOT_FOUND, 200),
            ({"jsonrpc": "2.0", "id": 1, "method": "prompts/list"}, mcp.METHOD_NOT_FOUND, 200),
            ("just a string", mcp.INVALID_REQUEST, 400),
            ([], mcp.INVALID_REQUEST, 400),
        ):
            r = post(json.dumps(payload).encode())
            self.assertEqual(r.status_code, status, payload)
            self.assertEqual(r.json()["error"]["code"], code, payload)
        self.assertEqual(post(json.dumps({"id": 7, "method": "ping"}).encode()).json()["id"], 7)

        # Internal dispatcher failures surface as -32603 without leaking details
        with patch.dict(mcp._METHODS, {"ping": lambda _p: 1 / 0}):
            r = self.rpc("ping")
            self.assertEqual(r.json()["error"], {"code": mcp.INTERNAL_ERROR, "message": "Internal error"})

    def test_batches_and_body_limit(self) -> None:
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "nope"},
        ]
        r = self.client.post(mcp.MCP_PATH, json=batch, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([m["id"] for m in r.json()], [1, 2])
        self.assertEqual(r.json()[1]["error"]["code"], mcp.METHOD_NOT_FOUND)

        only_notes = [{"jsonrpc": "2.0", "method": "notifications/initialized"}] * 2
        self.assertEqual(self.client.post(mcp.MCP_PATH, json=only_notes, headers=self.auth).status_code, 202)

        # One batch (one rate-limit hit) runs at most one tool; context never leaks between items
        call = lambda i, repo: {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "list_repos", "arguments": {"repo": repo}}}  # noqa: E731
        two_calls = self.client.post(mcp.MCP_PATH, json=[call(1, "man-pages"), call(2, "linux")], headers=self.auth)
        self.assertEqual(two_calls.status_code, 400)
        self.assertIn("one tools/call", two_calls.json()["error"]["message"])
        mixed = self.client.post(
            mcp.MCP_PATH,
            json=[call(1, "man-pages"), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
            headers=self.auth,
        ).json()
        self.assertEqual(mixed[0]["result"]["structuredContent"]["active"], "man-pages")
        self.assertEqual(len(mixed[1]["result"]["tools"]), len(mcp.visible_tools()))
        self.assertEqual(get_active_repo_name(), "linux")

        too_big = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(mcp.MAX_BATCH_SIZE + 1)]
        r_big = self.client.post(mcp.MCP_PATH, json=too_big, headers=self.auth)
        self.assertEqual(r_big.status_code, 400)
        self.assertEqual(r_big.json()["error"]["code"], mcp.INVALID_REQUEST)

        huge = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + b"x" * mcp.MAX_BODY_BYTES + b'"}}'
        r_huge = self.client.post(mcp.MCP_PATH, content=huge, headers={**self.auth, "Content-Type": "application/json"})
        self.assertEqual(r_huge.status_code, 413)

    def test_get_delete_and_other_methods_are_405(self) -> None:
        for method in ("GET", "DELETE", "PUT", "PATCH"):
            r = self.client.request(method, mcp.MCP_PATH, headers={**self.auth, "Accept": "text/event-stream"})
            self.assertEqual(r.status_code, 405, method)
            self.assertEqual(r.headers["allow"], "POST")
        # Auth still runs first
        self.assertEqual(self.client.get(mcp.MCP_PATH).status_code, 401)
        self.assertEqual(self.client.delete(mcp.MCP_PATH).status_code, 401)


class TestMcpTools(_McpFixture):
    def test_tools_list_schema_validity_and_v1_parity(self) -> None:
        r = self.rpc("tools/list", {})
        self.assertEqual(r.status_code, 200)
        tools = r.json()["result"]["tools"]
        self.assertNotIn("nextCursor", r.json()["result"])
        names = [t["name"] for t in tools]
        routes = _v1_get_routes()
        readonly_routes = _v1_readonly_tool_routes()
        self.assertEqual(names, [route.endpoint.__name__ for route in readonly_routes])
        self.assertEqual(len(names), len(set(names)))

        # Catalog parity: every REST GET endpoint is a tool and is advertised in /api/v1/capabilities
        catalog_get = {e["path"] for e in v1_router._ENDPOINT_CATALOG if e["method"] == "GET"}
        self.assertEqual(catalog_get, {route.path for route in routes})

        for tool_spec in mcp.TOOLS.values():
            tool = tool_spec.describe()
            self.assertRegex(tool["name"], r"^[a-z][a-z0-9_]*$")
            self.assertTrue(tool["description"])
            if tool_spec.required_scope is None:
                self.assertEqual(
                    tool["annotations"],
                    {"title": tool["title"], "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                )
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("repo", schema["properties"])
            self.assertEqual(schema["properties"]["repo"].get("minLength"), 1)
            self.assertTrue(set(schema.get("required", [])) <= set(schema["properties"]), tool["name"])
            self.assertNotIn("repo", schema.get("required", []))
            schema_json = json.dumps(schema)  # JSON-serializable
            self.assertNotIn('"$ref"', schema_json, tool["name"])
            self.assertNotIn('"$defs"', schema_json, tool["name"])
            for key, prop in schema["properties"].items():
                self.assertIn(prop.get("type"), _ALLOWED_TYPES, f"{tool['name']}.{key}: {prop}")
                self.assertNotIn("title", prop)
                self.assertNotIn("anyOf", prop)
                if prop["type"] == "array":
                    self.assertIn(prop["items"].get("type"), ("string", "object"), f"{tool['name']}.{key}")

        by_name = {t["name"]: t["inputSchema"] for t in tools}
        fs = by_name["get_file_slice"]
        self.assertEqual(fs["required"], ["path"])
        self.assertEqual(fs["properties"]["path"], {"type": "string", "minLength": 1, "maxLength": 512, "description": "Repository-relative path."})
        self.assertEqual((fs["properties"]["limit"]["minimum"], fs["properties"]["limit"]["maximum"]), (1, 500))
        self.assertEqual(fs["properties"]["limit"]["default"], 200)
        self.assertNotIn("default", fs["properties"]["version"])
        cp = by_name["get_callpath"]
        self.assertEqual(sorted(cp["required"]), ["from", "to"])
        self.assertNotIn("src", cp["properties"])
        self.assertEqual(by_name["grep_code"]["properties"]["mode"]["pattern"], "^(fixed|regex)$")
        self.assertEqual(by_name["grep_code"]["properties"]["path"]["type"], "array")
        self.assertEqual(by_name["get_kconfig_build_conditions"]["properties"]["path"]["minItems"], 1)
        self.assertEqual(by_name["get_symbol_ast"]["required"], ["ast_id"])
        self.assertEqual(by_name["get_symbol_ast"]["properties"]["ast_id"]["type"], "integer")
        self.assertEqual(by_name["lookup_cves"]["properties"]["kind"]["pattern"], "^(|cna|commit|text)$")
        self.assertIn("`kconfig` repository feature", next(t for t in tools if t["name"] == "eval_kconfig")["description"])

        bad_cursor = self.rpc("tools/list", {"cursor": "page-2"})
        self.assertEqual(bad_cursor.json()["error"]["code"], mcp.INVALID_PARAMS)

    def test_capabilities_advertise_mcp(self) -> None:
        rest = self.client.get("/api/v1/capabilities", headers=self.auth).json()
        self.assertEqual(rest["mcp"]["endpoint"], "/api/mcp")
        self.assertEqual(rest["mcp"]["protocol_versions"], list(mcp.SUPPORTED_PROTOCOL_VERSIONS))
        self.assertEqual(rest["mcp"]["tools"], len(mcp.TOOLS))
        self.assertIn({"method": "POST", "path": "/api/mcp"}, [{k: e[k] for k in ("method", "path")} for e in rest["endpoints"]])
        via_mcp = self.assertToolOk(self.call("get_capabilities"))
        self.assertEqual(via_mcp["mcp"]["endpoint"], "/api/mcp")
        self.assertEqual(via_mcp["active_repo"], "linux")

    def test_tool_calls_return_structured_content(self) -> None:
        sl = self.assertToolOk(self.call("get_file_slice", {"path": "kernel/foo.c", "start_line": 1, "limit": 3}))
        self.assertTrue(sl["truncated"])
        self.assertEqual((sl["start_line"], sl["end_line"], sl["total_lines"]), (1, 3, 8))
        self.assertIn("foo_helper", sl["content"])

        # Lax scalar coercion mirrors query-string semantics; a bare string fills a list parameter
        grep = self.assertToolOk(self.call("grep_code", {"q": "foo_helper", "path": "kernel/", "limit": "10"}))
        self.assertEqual(grep["matches"][0]["path"], "kernel/foo.c")
        grep_list = self.assertToolOk(self.call("grep_code", {"q": "foo_helper", "path": ["kernel/"]}))
        self.assertEqual(grep_list["matches"][0]["path"], "kernel/foo.c")

        tree = self.assertToolOk(self.call("get_tree", {"limit": 2}))
        self.assertTrue(tree["truncated"])

        with patch.object(v1_router._pahole_service, "get_struct_layout", return_value={"struct": "foo", "size": 4}):
            self.assertEqual(self.assertToolOk(self.call("get_struct_layout", {"name": "foo"}))["size"], 4)

        # Non-object payloads are wrapped under `result` (structuredContent is always an object)
        self.assertEqual(mcp._tool_success([1, 2])["structuredContent"], {"result": [1, 2]})

    def test_repo_override_header_default_and_no_context_leak(self) -> None:
        self.assertEqual(self.assertToolOk(self.call("list_repos"))["active"], "linux")
        over = self.assertToolOk(self.call("list_repos", {"repo": "man-pages"}))
        self.assertEqual(over["active"], "man-pages")
        self.assertEqual(self.assertToolOk(self.call("list_repos"))["active"], "linux")

        # X-KI-Repo / ?repo= set the request default; the `repo` argument overrides it per call
        hdr = {"X-KI-Repo": "man-pages"}
        self.assertEqual(self.assertToolOk(self.call("get_capabilities", headers=hdr))["active_repo"], "man-pages")
        self.assertEqual(self.assertToolOk(self.call("list_repos", {"repo": "linux"}, headers=hdr))["active"], "linux")
        r_qs = self.client.post(
            f"{mcp.MCP_PATH}?repo=man-pages",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_repos"}},
            headers=self.auth,
        )
        self.assertEqual(r_qs.json()["result"]["structuredContent"]["active"], "man-pages")

        # Direct in-process calls restore the caller's context, also when the handler raises
        self.assertEqual(get_active_repo_name(), "linux")
        mcp.call_tool("list_repos", {"repo": "man-pages"})
        self.assertEqual(get_active_repo_name(), "linux")
        with patch.object(v1_router._pahole_service, "get_struct_layout", side_effect=RuntimeError("boom")):
            mcp.call_tool("get_struct_layout", {"name": "foo"})
        self.assertEqual(get_active_repo_name(), "linux")

        unknown = self.call("list_repos", {"repo": "no-such-repo"})
        self.assertIn("Unknown repository", self.assertToolError(unknown, 404)["detail"])
        self.assertToolError(self.call("list_repos", {"repo": 5}), 422)
        for blank in ("", "   "):
            self.assertToolError(self.call("list_repos", {"repo": blank}), 422)

    def test_concurrent_repo_overrides_are_isolated(self) -> None:
        real_catalog = v1_router.projects_catalog
        barrier = threading.Barrier(4, timeout=10)

        def slow_catalog(*args, **kwargs):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return real_catalog(*args, **kwargs)

        repos = ["linux", "man-pages", "man-pages", "linux"]

        async def run() -> list[dict]:
            transport = httpx.ASGITransport(app=self.client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                reqs = [
                    client.post(
                        mcp.MCP_PATH,
                        json={"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "list_repos", "arguments": {"repo": repo}}},
                        headers=self.auth,
                    )
                    for i, repo in enumerate(repos)
                ]
                return [r.json() for r in await asyncio.gather(*reqs)]

        with patch.object(v1_router, "projects_catalog", side_effect=slow_catalog):
            results = asyncio.run(run())
        self.assertEqual([r["result"]["structuredContent"]["active"] for r in results], repos)
        self.assertEqual(get_active_repo_name(), "linux")

    def test_callpath_aliases_and_job_states(self) -> None:
        with patch("api.routers.v1.callpath_state", return_value={"state": "done", "result": {"paths": [["main_entry", "foo_helper"]]}}) as st:
            res = self.assertToolOk(self.call("get_callpath", {"from": "main_entry", "to": "foo_helper", "max_depth": 3}))
            self.assertEqual(res["paths"], [["main_entry", "foo_helper"]])
            self.assertEqual(st.call_args.args[1:3], ("main_entry", "foo_helper"))
            self.assertEqual(st.call_args.kwargs["max_depth"], 3)
            # Python parameter names are accepted too
            self.assertToolOk(self.call("get_callpath", {"src": "main_entry", "dst": "foo_helper"}))

        missing = self.assertToolError(self.call("get_callpath", {"from": "main_entry"}), 422)
        self.assertEqual(missing["detail"][0]["loc"], ["to"])

        with patch("api.routers.v1.callpath_state", return_value={"state": "building", "progress": {"done": 1}}):
            building = self.assertToolOk(self.call("get_callpath", {"from": "a", "to": "b"}))
            self.assertEqual(building["state"], "building")
            self.assertIn("retry_after_ms", building)
        with patch("api.routers.v1.callpath_state", return_value={"state": "failed", "error": "oops"}):
            self.assertIn("oops", self.assertToolError(self.call("get_callpath", {"from": "a", "to": "b"}), 500)["detail"])

    def test_argument_validation_and_handler_errors(self) -> None:
        cases = [
            ("get_file_slice", {}, "path"),
            ("get_file_slice", {"path": "kernel/foo.c", "limit": 0}, "limit"),
            ("get_file_slice", {"path": "kernel/foo.c", "limit": 501}, "limit"),
            ("get_file_slice", {"path": "kernel/foo.c", "start_line": "abc"}, "start_line"),
            ("get_file_slice", {"path": ""}, "path"),
            ("get_file_slice", {"path": "x" * 513}, "path"),
            ("get_file_slice", {"path": "kernel/foo.c", "bogus": 1}, "bogus"),
            ("grep_code", {"q": "foo", "mode": "glob"}, "mode"),
            ("grep_code", {"q": "foo", "path": [1]}, "path"),
            ("get_symbol_ast", {"ast_id": "x"}, "ast_id"),
            ("get_kconfig_build_conditions", {"path": []}, "path"),
            ("search_symbols", {"q": 123}, "q"),
        ]
        for name, args, field in cases:
            err = self.assertToolError(self.call(name, args), 422)
            self.assertEqual(err["detail"][0]["loc"][0], field, (name, args, err))

        # Handler HTTPExceptions (400) become tool errors
        self.assertToolError(self.call("get_symbol_definition", {"name": "bad;ident"}), 400)
        self.assertToolError(self.call("grep_code", {"q": "foo", "path": "../secret"}), 400)

        # Unexpected handler failures are tool errors without internals
        with patch.object(v1_router._pahole_service, "get_struct_layout", side_effect=RuntimeError("secret internals")):
            with self.assertLogs("api.mcp", level=logging.ERROR):
                err = self.call("get_struct_layout", {"name": "foo"})
        self.assertEqual(self.assertToolError(err, 500)["detail"], "Internal server error")
        self.assertNotIn("secret internals", json.dumps(err))

        # Unknown tools, missing names, and non-object arguments are protocol errors
        for params in ({"name": "drop_tables"}, {"arguments": {}}, {"name": "list_repos", "arguments": [1]}, {"name": 7}):
            r = self.rpc("tools/call", params)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["error"]["code"], mcp.INVALID_PARAMS, params)
        # Omitted arguments mean {}
        r = self.rpc("tools/call", {"name": "list_repos"})
        self.assertFalse(r.json()["result"]["isError"])

    def test_disabled_features_return_tool_errors(self) -> None:
        for name, args in (
            ("search_kconfig", {"q": "NET"}),
            ("get_kconfig_symbol", {"symbol_name": "NET"}),
            ("eval_kconfig", {"symbol": ["NET"]}),
            ("lookup_maintainers", {"q": "net"}),
            ("lookup_cves", {}),
            ("get_struct_layout", {"name": "foo"}),
            ("get_callgraph", {"function": "foo"}),
            ("get_callpath", {"from": "a", "to": "b"}),
        ):
            err = self.assertToolError(self.call(name, {**args, "repo": "man-pages"}), 403)
            self.assertIn("disabled", err["detail"], name)
        # The same tools pass the feature gate on the kernel repository
        with patch("api.routers.v1.callpath_state", return_value={"state": "done", "result": {"paths": []}}):
            self.assertToolOk(self.call("get_callpath", {"from": "a", "to": "b"}))

    def test_write_scoped_mcp_tools_visibility_and_execution(self) -> None:
        # 1. Default (allow_writes=False): write tools hidden from tools/list and return 403 on tools/call
        default_names = {t["name"] for t in self.rpc("tools/list", {}).json()["result"]["tools"]}
        self.assertIn("preview_nodemap_canvas", default_names)
        self.assertNotIn("create_nodemap_canvas", default_names)
        self.assertToolError(self.call("create_nodemap_canvas", {"id": "mcp-map", "name": "MCP Map"}), 403)

        # 2. Stateless preview works via MCP in read-only mode
        prev = self.assertToolOk(
            self.call(
                "preview_nodemap_canvas",
                {
                    "name": "MCP Preview",
                    "version": "v3.0",
                    "ops": [
                        {
                            "op": "add_nodes",
                            "hydrate": True,
                            "nodes": [{"id": "n1", "type": "struct", "data": {"name": "foo", "hint": "include/linux/foo.h"}}],
                        }
                    ],
                    "export_format": "dot",
                },
            )
        )
        self.assertEqual(prev["node_count"], 1)
        self.assertIn("digraph NodeMap", prev["export"])

        # 3. Enable allow_writes with a scoped key -> write tools appear in tools/list and execute via tools/call
        cfg = core_config.get_config()
        cfg["api"]["allow_writes"] = True
        cfg["api"]["keys"] = {
            "reader": {"key": "mcp-read-token", "scopes": ["read"]},
            "writer": {"key": "mcp-write-token", "scopes": ["read", "nodemap:write"]},
        }
        client = TestClient(create_app())

        def rpc_with(token: str, method: str, params: dict | None = None):
            return client.post(
                mcp.MCP_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                headers={"Authorization": f"Bearer {token}"},
            ).json()

        read_tools = {t["name"] for t in rpc_with("mcp-read-token", "tools/list")["result"]["tools"]}
        write_tools = {t["name"]: t for t in rpc_with("mcp-write-token", "tools/list")["result"]["tools"]}
        self.assertNotIn("create_nodemap_canvas", read_tools)
        self.assertIn("create_nodemap_canvas", write_tools)
        self.assertFalse(write_tools["create_nodemap_canvas"]["annotations"]["readOnlyHint"])
        self.assertTrue(write_tools["delete_nodemap_canvas"]["annotations"]["destructiveHint"])
        self.assertTrue(write_tools["replace_nodemap_canvas"]["annotations"]["idempotentHint"])

        # Reader token gets 403 on write tool call
        r_denied = rpc_with(
            "mcp-read-token",
            "tools/call",
            {"name": "create_nodemap_canvas", "arguments": {"id": "mcp-map", "name": "MCP Map"}},
        )["result"]
        self.assertTrue(r_denied["isError"])
        self.assertEqual(r_denied["structuredContent"]["error"]["status"], 403)

        # Writer token creates, mutates, lays out, and deletes canvas via MCP
        r_created = rpc_with(
            "mcp-write-token",
            "tools/call",
            {"name": "create_nodemap_canvas", "arguments": {"id": "mcp-map", "name": "MCP Map", "version": "v3.0"}},
        )["result"]
        self.assertFalse(r_created["isError"], r_created)
        self.assertEqual(r_created["structuredContent"]["id"], "mcp-map")

        r_mut = rpc_with(
            "mcp-write-token",
            "tools/call",
            {
                "name": "mutate_nodemap_canvas",
                "arguments": {
                    "canvas_id": "mcp-map",
                    "expected_revision": 1,
                    "ops": [
                        {
                            "op": "add_nodes",
                            "nodes": [{"id": "n1", "type": "note", "title": "Hello", "data": {"text": "World"}}],
                        }
                    ],
                },
            },
        )["result"]
        self.assertFalse(r_mut["isError"], r_mut)
        self.assertEqual(r_mut["structuredContent"]["node_count"], 1)

        r_del = rpc_with(
            "mcp-write-token",
            "tools/call",
            {"name": "delete_nodemap_canvas", "arguments": {"canvas_id": "mcp-map"}},
        )["result"]
        self.assertFalse(r_del["isError"], r_del)
        self.assertTrue(r_del["structuredContent"]["deleted"])

    def test_p1_p2_mcp_tools_readonly_posts_and_p3_exclusion(self) -> None:
        # 1. Read-only POST MCP tools remain visible in tools/list and callable when allow_writes=False
        tools = {t["name"]: t for t in self.rpc("tools/list", {}).json()["result"]["tools"]}
        for ro_post_tool in ("preview_nodemap_canvas", "match_patch_maintainers", "check_watched_changes"):
            self.assertIn(ro_post_tool, tools)
            self.assertTrue(tools[ro_post_tool]["annotations"]["readOnlyHint"])
            self.assertFalse(tools[ro_post_tool]["annotations"]["destructiveHint"])

        # 2. P3 REST-only endpoints (mcp=False) are strictly excluded from TOOLS, tools/list, and tools/call
        for p3_tool in ("get_compile_commands", "get_codebase_treemap", "run_checkpatch_tool"):
            self.assertNotIn(p3_tool, mcp.TOOLS)
            self.assertNotIn(p3_tool, tools)
            r_p3 = self.rpc("tools/call", {"name": p3_tool, "arguments": {}})
            self.assertEqual(r_p3.status_code, 200)
            self.assertEqual(r_p3.json()["error"]["code"], mcp.INVALID_PARAMS)

        # 3. Call P1 and P2 MCP tools (including read-only POST tools)
        with patch("webapp.backend.services.kapi_service.summary", return_value={
            "version": "v3.0", "arch": "x86_64", "counts": {"syscalls": 300},
        }):
            ksum = self.assertToolOk(self.call("get_kapi_summary", {"arch": "x86_64"}))
            self.assertEqual(ksum["repo"], "linux")
            self.assertEqual(ksum["counts"]["syscalls"], 300)

        with patch("webapp.backend.services.mailing_list_service.mailing_list_service.search", return_value={
            "total": 1, "results": [{"message_id": "m1@example.com", "subject": "[PATCH] test"}],
        }):
            ms = self.assertToolOk(self.call("search_mail", {"q": "test", "filter": "patch"}))
            self.assertEqual(ms["repo"], "linux")
            self.assertEqual(ms["results"][0]["message_id"], "m1@example.com")

        with patch.object(v1_router._kconfig_service, "get_graph", return_value={
            "symbol": "FOO_BAR", "nodes": [{"id": "FOO_BAR"}], "edges": [],
        }):
            kg = self.assertToolOk(self.call("get_kconfig_graph", {"symbol": "FOO_BAR"}))
            self.assertEqual(kg["repo"], "linux")
            self.assertEqual(kg["symbol"], "FOO_BAR")

        with patch("webapp.backend.services.pahole_compare.compare", return_value={
            "struct": "foo", "a": {"size": 4}, "b": {"size": 8},
        }):
            scmp = self.assertToolOk(self.call("compare_struct_layout", {"name": "foo", "version_a": "v3.0", "version_b": "master"}))
            self.assertEqual(scmp["repo"], "linux")
            self.assertEqual(scmp["b"]["size"], 8)

        with patch.object(v1_router._maintainer_service, "match_maintainers", return_value={
            "version": "v3.0", "sections": [{"name": "FOO SUBSYSTEM"}], "recipients": [{"email": "alice@example.com"}],
        }):
            # Multi-path lookup_maintainers (bare string or list)
            mt_list = self.assertToolOk(self.call("lookup_maintainers", {"path": ["kernel/foo.c", "include/linux/foo.h"]}))
            self.assertEqual(mt_list["paths"], ["kernel/foo.c", "include/linux/foo.h"])
            # Read-only POST tool match_patch_maintainers
            mp_post = self.assertToolOk(
                self.call(
                    "match_patch_maintainers",
                    {"patch_text": "--- a/kernel/foo.c\n+++ b/kernel/foo.c\n", "paths": ["kernel/foo.c"]},
                )
            )
            self.assertEqual(mp_post["repo"], "linux")
            self.assertEqual(mp_post["recipients"][0]["email"], "alice@example.com")

        # Read-only POST tool check_watched_changes (accepts `from` and `to` aliases)
        w_res = self.assertToolOk(
            self.call(
                "check_watched_changes",
                {
                    "from": "v3.0",
                    "to": "master",
                    "items": [{"key": "w1", "type": "file", "path": "kernel/foo.c"}],
                },
            )
        )
        self.assertEqual(w_res["repo"], "linux")
        self.assertEqual(w_res["items"][0]["status"], "same")

        btags = self.assertToolOk(self.call("get_branch_tags", {"branch": "master"}))
        self.assertEqual(btags["repo"], "linux")
        self.assertIn("v3.0", btags["tags"])

    def test_mcp_http_with_oauth_access_token_and_scope_visibility(self) -> None:
        from api.oauth import oauth_manager

        with tempfile.TemporaryDirectory() as tmp:
            oauth_dir = Path(tmp) / "oauth-data"
            with patch.dict(
                os.environ,
                {
                    "API_OAUTH_ENABLED": "1",
                    "API_OAUTH_ISSUER": "https://kernelinfo.org",
                    "API_OAUTH_DATA_DIR": str(oauth_dir),
                    "API_ALLOW_WRITES": "1",
                    "API_KEYS": json.dumps(
                        {
                            "ro_user": {"key": "ro-secret-key", "scopes": ["read"]},
                            "rw_user": {"key": "rw-secret-key", "scopes": ["read", "nodemap:write"]},
                        }
                    ),
                },
            ):
                core_config.reset_config()
                core_config.init_config(self.cfg_path)

                ro_token, _ = oauth_manager.issue_access_token(
                    issuer="https://kernelinfo.org",
                    sub="ro_user",
                    aud="https://kernelinfo.org/api/mcp",
                    client_id="claude-web",
                    scopes=["read"],
                    ttl=3600,
                )
                rw_token, _ = oauth_manager.issue_access_token(
                    issuer="https://kernelinfo.org",
                    sub="rw_user",
                    aud="https://kernelinfo.org/api/mcp",
                    client_id="claude-web",
                    scopes=["read", "nodemap:write"],
                    ttl=3600,
                )

                # Read-only OAuth token hides write tools in tools/list and rejects write tools/call with 403
                r_ro_list = self.client.post(
                    mcp.MCP_PATH,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": f"Bearer {ro_token}"},
                )
                self.assertEqual(r_ro_list.status_code, 200)
                ro_names = {t["name"] for t in r_ro_list.json()["result"]["tools"]}
                self.assertNotIn("create_nodemap_canvas", ro_names)
                self.assertIn("preview_nodemap_canvas", ro_names)

                r_ro_call = self.client.post(
                    mcp.MCP_PATH,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "create_nodemap_canvas", "arguments": {"name": "Blocked"}},
                    },
                    headers={"Authorization": f"Bearer {ro_token}"},
                )
                self.assertEqual(r_ro_call.status_code, 200)
                self.assertToolError(r_ro_call.json()["result"], 403)

                # Write-scoped OAuth token sees and executes create_nodemap_canvas
                r_rw_list = self.client.post(
                    mcp.MCP_PATH,
                    json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                    headers={"Authorization": f"Bearer {rw_token}"},
                )
                self.assertEqual(r_rw_list.status_code, 200)
                rw_names = {t["name"] for t in r_rw_list.json()["result"]["tools"]}
                self.assertIn("create_nodemap_canvas", rw_names)

                r_rw_call = self.client.post(
                    mcp.MCP_PATH,
                    json={
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "create_nodemap_canvas", "arguments": {"name": "OAuth Canvas"}},
                    },
                    headers={"Authorization": f"Bearer {rw_token}"},
                )
                self.assertEqual(r_rw_call.status_code, 200)
                created = self.assertToolOk(r_rw_call.json()["result"])
                self.assertEqual(created["name"], "OAuth Canvas")
            core_config.reset_config()
            core_config.init_config(self.cfg_path)


class TestMcpSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_cfg = copy.deepcopy(core_config._ACTIVE_CONFIG)
        self._orig_cfg_path = core_config._LOADED_CONFIG_PATH
        core_config.reset_config()
        self._orig_env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self._repo_dir = Path(self._tmp.name) / "linux"
        self._repo_dir.mkdir()
        self._cfg_path = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        core_config.reset_config()
        os.environ.clear()
        os.environ.update(self._orig_env)
        core_config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core_config._LOADED_CONFIG_PATH = self._orig_cfg_path
        self._tmp.cleanup()

    def _client(self, api_section: dict) -> TestClient:
        self._cfg_path.write_text(
            json.dumps(
                {
                    "workspace": {"auto_detect_repos": False},
                    "repositories": [{"name": "linux", "path": str(self._repo_dir), "default": True}],
                    "api": api_section,
                }
            ),
            encoding="utf-8",
        )
        core_config.init_config(self._cfg_path)
        return TestClient(create_app())

    @staticmethod
    def _ping(client: TestClient, headers: dict | None = None):
        return client.post(mcp.MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers or {})

    def test_auth_required_with_bearer_and_x_api_key(self) -> None:
        client = self._client({"keys": {"agent": "mcp-secret"}})
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-API-Key": "wrong"}, {"Authorization": "mcp-secret"}):
            r = self._ping(client, headers)
            self.assertEqual(r.status_code, 401, headers)
            self.assertEqual(r.json(), {"detail": "Invalid or missing API key"})
            self.assertEqual(r.headers["www-authenticate"], "Bearer")
            self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(self._ping(client, {"Authorization": "Bearer mcp-secret"}).status_code, 200)
        self.assertEqual(self._ping(client, {"X-API-Key": "mcp-secret"}).status_code, 200)

        # Fails closed without keys
        closed = self._client({"keys": []})
        self.assertEqual(self._ping(closed, {"Authorization": "Bearer anything"}).status_code, 401)

    def test_no_cors_and_not_in_openapi(self) -> None:
        client = self._client({"keys": ["k"]})
        hdrs = {"Authorization": "Bearer k", "Origin": "https://evil.example"}
        r = self._ping(client, hdrs)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in r.headers})
        pre = client.options(mcp.MCP_PATH, headers={**hdrs, "Access-Control-Request-Method": "POST"})
        self.assertEqual(pre.status_code, 405)
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in pre.headers})
        self.assertNotIn(mcp.MCP_PATH, client.get("/api/openapi.json", headers=hdrs).json()["paths"])

    def test_progressive_lockout_covers_mcp(self) -> None:
        client = self._client(
            {
                "keys": {"agent": "good"},
                "auth_lockout": {"max_failures": 3, "failure_window_seconds": 60, "base_lockout_seconds": 30, "max_lockout_seconds": 60},
            }
        )
        for _ in range(3):
            self.assertEqual(self._ping(client, {"X-API-Key": "bad"}).status_code, 401)
        locked = self._ping(client, {"X-API-Key": "good"})
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(int(locked.headers["retry-after"]), 30)
        # The lockout is per IP across the whole service
        self.assertEqual(client.get("/api/v1/capabilities", headers={"X-API-Key": "good"}).status_code, 429)

    def test_rate_limit_is_shared_by_mcp_and_rest(self) -> None:
        client = self._client({"keys": {"agent": "good"}, "rate_limit": {"max_requests": 2, "window_seconds": 60}})
        hdrs = {"Authorization": "Bearer good"}
        self.assertEqual(self._ping(client, hdrs).status_code, 200)
        self.assertEqual(client.get("/api/v1/capabilities", headers=hdrs).status_code, 200)
        limited = self._ping(client, hdrs)
        self.assertEqual(limited.status_code, 429)
        self.assertGreaterEqual(int(limited.headers["retry-after"]), 1)

    def test_request_logs_redact_secrets(self) -> None:
        secret = "mcp-super-secret-token-42"
        client = self._client({"keys": {"bot": secret}})
        with self.assertLogs("api.request", level=logging.INFO) as cm:
            ok = self._ping(client, {"Authorization": f"Bearer {secret}"})
            bad = self._ping(client, {"X-API-Key": "mcp-wrong-secret"})
            leaked = client.post(f"{mcp.MCP_PATH}?token={secret}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"X-API-Key": secret})
        self.assertEqual((ok.status_code, bad.status_code, leaked.status_code), (200, 401, 200))
        joined = "\n".join(cm.output)
        self.assertIn(mcp.MCP_PATH, joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("mcp-wrong-secret", joined)
        self.assertIn("key=bot", joined)


class TestMcpStdio(unittest.TestCase):
    def test_serve_newline_framing(self) -> None:
        lines = [
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}\n',
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            b"\n",
            b"   \r\n",
            b"{broken\n",
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
            b'[{"jsonrpc":"2.0","id":3,"method":"ping"},{"jsonrpc":"2.0","method":"notifications/cancelled"}]\n',
            b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_file_slice","arguments":{}}}\n',
            b"[" * 200_000 + b"\n",
        ]
        out = io.BytesIO()
        mcp_stdio.serve(lines, out)
        raw = out.getvalue()
        self.assertTrue(raw.endswith(b"\n"))
        frames = [json.loads(line) for line in raw.decode("utf-8").split("\n") if line]
        self.assertEqual(len(frames), 6)
        self.assertEqual(frames[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(frames[1]["error"]["code"], mcp.PARSE_ERROR)
        self.assertEqual(len(frames[2]["result"]["tools"]), len(mcp.visible_tools(frozenset({"read"}))))
        self.assertEqual(frames[3], [{"jsonrpc": "2.0", "id": 3, "result": {}}])
        self.assertTrue(frames[4]["result"]["isError"])
        self.assertEqual(frames[5]["error"]["code"], mcp.PARSE_ERROR)

    def test_oversized_lines_are_rejected_and_skipped(self) -> None:
        limit = 64
        stream = io.BytesIO(
            b"x" * 200
            + b"\n"
            + b"   "
            + b"y" * 200
            + b"\n"
            + b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
            + b"tail-without-newline"
        )
        lines = list(mcp_stdio.read_lines(stream, limit))
        self.assertEqual(len(lines), 4)
        self.assertEqual(len(lines[0]), limit + 1)
        self.assertEqual(len(lines[1]), limit + 1)
        self.assertEqual(json.loads(lines[2]), {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        with patch.object(mcp, "MAX_BODY_BYTES", limit):
            out = io.BytesIO()
            mcp_stdio.serve(lines, out)
        frames = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(frames[0]["error"]["code"], mcp.INVALID_REQUEST)
        # Leading whitespace on an oversized line must still trigger INVALID_REQUEST (-32600), not PARSE_ERROR (-32700)
        self.assertEqual(frames[1]["error"]["code"], mcp.INVALID_REQUEST)
        self.assertEqual(frames[2]["result"], {})
        self.assertEqual(frames[3]["error"]["code"], mcp.PARSE_ERROR)

    def test_encode_frame_is_single_line(self) -> None:
        frame = mcp_stdio.encode_frame({"jsonrpc": "2.0", "id": 1, "result": {"text": "a\nb c"}})
        self.assertEqual(frame.count(b"\n"), 1)
        self.assertTrue(frame.endswith(b"\n"))
        self.assertEqual(json.loads(frame)["result"]["text"], "a\nb c")

    def test_subprocess_stdout_carries_only_jsonrpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "linux"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
            cfg = Path(tmp) / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "database": {"engine": "mock", "ssh_tunnel": {"enabled": False}},
                        "workspace": {"auto_detect_repos": False},
                        "repositories": [
                            {"name": "linux", "path": str(repo), "default": True},
                            {"name": "other", "path": str(repo)},
                        ],
                        "api": {"keys": ["unused-for-stdio"]},
                    }
                ),
                encoding="utf-8",
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith(("DB_", "MYSQL_", "KIP_", "API_"))}
            env.update({"DB_ENGINE": "mock", "PYTHONWARNINGS": "always", "PYTHONUNBUFFERED": "1"})
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_repos", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_repos", "arguments": {"repo": "linux"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "ping"},
            ]
            stdin = "".join(json.dumps(m) + "\n" for m in messages) + "not-json\n"
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "api" / "mcp_stdio.py"), "-c", str(cfg), "--repo", "other"],
                input=stdin.encode(),
                capture_output=True,
                env=env,
                cwd=tmp,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace")[-2000:])
            lines = proc.stdout.decode("utf-8").splitlines()
            frames = [json.loads(line) for line in lines]  # every stdout line is JSON
            self.assertTrue(all(f.get("jsonrpc") == "2.0" for f in frames))
            self.assertEqual([f["id"] for f in frames], [1, 2, 3, 4, 5, None])
            self.assertEqual(frames[0]["result"]["protocolVersion"], "2024-11-05")
            self.assertEqual(len(frames[1]["result"]["tools"]), len(mcp.visible_tools(frozenset({"read"}))))
            self.assertEqual(frames[2]["result"]["structuredContent"]["active"], "other")  # --repo default
            self.assertEqual(frames[3]["result"]["structuredContent"]["active"], "linux")  # per-call override
            self.assertEqual(frames[5]["error"]["code"], mcp.PARSE_ERROR)
            self.assertIn(b"Loaded configuration", proc.stderr)  # logging went to stderr

            bad = subprocess.run(
                [sys.executable, str(REPO_ROOT / "api" / "mcp_stdio.py"), "-c", str(cfg), "--repo", "nope"],
                input=b"", capture_output=True, env=env, cwd=tmp, timeout=120,
            )
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(bad.stdout, b"")
            self.assertIn(b"Unknown repository", bad.stderr)

    def test_allow_writes_flag_overrides_env_disable_and_restores_state(self) -> None:
        from api.config import get_api_server_config

        with patch.dict(os.environ, {"API_ALLOW_WRITES": "0", "KIP_API_ALLOW_WRITES": "0"}):
            self.assertFalse(get_api_server_config()["allow_writes"])
            out = io.BytesIO()
            mcp_stdio.serve([b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'], out, allow_writes=True)
            frame = json.loads(out.getvalue().decode("utf-8").strip())
            names = {t["name"] for t in frame["result"]["tools"]}
            self.assertIn("create_nodemap_canvas", names)
            # Cleanly restored on exit
            self.assertFalse(get_api_server_config()["allow_writes"])


if __name__ == "__main__":
    unittest.main()
