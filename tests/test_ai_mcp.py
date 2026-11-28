"""tests/test_ai_mcp.py - MCP JSON-RPC 2.0 server (`api/mcp.py`, `POST /api/mcp`, `api/mcp_stdio.py`) tests."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import os
import shutil
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

        # Slimming: no (REST twin: ...) boilerplate, no duplicate alias params, fixed git-revision docs
        for t in tools:
            self.assertNotIn("(REST twin:", t["description"], t["name"])
        for tool_name, omitted_keys in (
            ("resolve_file_target", ("from_file",)),
            ("compare_struct_layout", ("struct", "a", "b")),
            ("get_struct_history", ("struct",)),
            ("get_kconfig_diff", ("a", "b")),
            ("get_kapi_diff", ("a", "b")),
            ("get_man_diff", ("a", "b")),
            ("search_mail", ("page",)),
            ("match_patch_maintainers", ("patch", "touched_files")),
            ("get_maintainer_person", ("email",)),
            ("preview_nodemap_canvas", ("expand", "layout")),
        ):
            props = by_name[tool_name]["properties"]
            for alias_key in omitted_keys:
                self.assertNotIn(alias_key, props, f"{tool_name}.{alias_key}")

        lrc = by_name["get_line_range_commits"]["properties"]
        self.assertIn("Git revision", lrc["from"]["description"])
        self.assertIn("Git revision", lrc["to"]["description"])
        self.assertNotIn("caller", lrc["from"]["description"].lower())
        self.assertNotIn("callee", lrc["to"]["description"].lower())
        self.assertIn("caller", by_name["get_callpath"]["properties"]["from"]["description"].lower())
        self.assertIn("callee", by_name["get_callpath"]["properties"]["to"]["description"].lower())
        self.assertEqual(by_name["list_versions"]["properties"]["order"]["default"], "desc")

        # Explicit nested schema for preview_nodemap_canvas (no untyped objects, no list-of-strings hint)
        pnc = by_name["preview_nodemap_canvas"]["properties"]
        for obj_list_key in ("nodes", "edges", "groups", "tours"):
            self.assertNotIn("single string", pnc[obj_list_key].get("description", ""), obj_list_key)
        node_item = pnc["nodes"]["items"]
        self.assertFalse(node_item["additionalProperties"])
        self.assertIn("source", node_item["properties"]["type"]["enum"])
        self.assertIn("struct", node_item["properties"]["type"]["enum"])
        self.assertIn("kconfig", node_item["properties"]["type"]["enum"])
        self.assertIn("commit", node_item["properties"]["type"]["enum"])
        self.assertIn("mail", node_item["properties"]["type"]["enum"])
        self.assertIn("group", node_item["properties"]["type"]["enum"])
        self.assertFalse(node_item["properties"]["data"]["additionalProperties"])
        self.assertIn("commitHash", node_item["properties"]["data"]["properties"])
        edge_item = pnc["edges"]["items"]
        self.assertFalse(edge_item["additionalProperties"])
        self.assertEqual(sorted(edge_item["required"]), ["from", "to"])
        self.assertEqual(edge_item["properties"]["style"]["enum"], ["solid", "dashed", "dotted"])
        tour_item = pnc["tours"]["items"]
        self.assertFalse(tour_item["additionalProperties"])
        self.assertIn("id", tour_item["properties"])
        self.assertIn("title", tour_item["properties"])
        step_item = tour_item["properties"]["steps"]["items"]
        self.assertIn("title", step_item["properties"])
        self.assertIn("caption", step_item["properties"])
        self.assertIn("nodeIds", step_item["properties"])

        bad_cursor = self.rpc("tools/list", {"cursor": "page-2"})
        self.assertEqual(bad_cursor.json()["error"]["code"], mcp.INVALID_PARAMS)

    def test_capabilities_advertise_mcp(self) -> None:
        rest = self.client.get("/api/v1/capabilities", headers=self.auth).json()
        self.assertEqual(rest["mcp"]["endpoint"], "/api/mcp")
        self.assertEqual(rest["mcp"]["protocol_versions"], list(mcp.SUPPORTED_PROTOCOL_VERSIONS))
        self.assertEqual(rest["mcp"]["tools"], len(mcp.visible_tools()))
        self.assertTrue(rest["read_only"])
        self.assertEqual(rest["scopes"], ["read"])
        self.assertIn({"method": "POST", "path": "/api/mcp"}, [{k: e[k] for k in ("method", "path")} for e in rest["endpoints"]])
        via_mcp = self.assertToolOk(self.call("get_capabilities"))
        self.assertEqual(via_mcp["mcp"]["endpoint"], "/api/mcp")
        self.assertEqual(via_mcp["mcp"]["tools"], len(mcp.visible_tools()))
        self.assertTrue(via_mcp["read_only"])
        self.assertEqual(via_mcp["active_repo"], "linux")
        self.assertNotIn("endpoints", via_mcp)
        via_mcp_full = self.assertToolOk(self.call("get_capabilities", {"include_endpoints": True}))
        self.assertGreaterEqual(len(via_mcp_full["endpoints"]), 20)

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

    def test_no_write_tools_exist_even_with_writes_enabled(self) -> None:
        removed = ('create_nodemap_canvas', 'replace_nodemap_canvas', 'delete_nodemap_canvas', 'mutate_nodemap_canvas', 'expand_nodemap_graph', 'layout_nodemap_canvas', 'attach_nested_nodemap', 'group_nodemap_nodes')
        # 1. Default: removed canvas-write tools are absent; calling one is an unknown-tool protocol error
        default_names = {t["name"] for t in self.rpc("tools/list", {}).json()["result"]["tools"]}
        self.assertIn("preview_nodemap_canvas", default_names)
        for name in removed:
            self.assertNotIn(name, default_names)
            self.assertNotIn(name, mcp.TOOLS)
            r = self.rpc("tools/call", {"name": name, "arguments": {}})
            self.assertEqual(r.json()["error"]["code"], mcp.INVALID_PARAMS)

        # 2. Stateless preview works via MCP in read-only mode (compact export omits canvas + export_content)
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
        self.assertNotIn("export_content", prev)
        self.assertNotIn("canvas", prev)

        # Opt-in include_canvas=True returns canvas with empty UI defaults stripped unless verbose=True
        prev_c = self.assertToolOk(
            self.call(
                "preview_nodemap_canvas",
                {
                    "name": "MCP Preview",
                    "version": "v3.0",
                    "nodes": [{"id": "n1", "type": "struct", "data": {"name": "foo", "hint": "include/linux/foo.h"}}],
                },
            )
        )
        n0 = prev_c["canvas"]["nodes"][0]
        for ui_key in ("color", "bgColor", "customTitle", "locks", "layerId"):
            self.assertNotIn(ui_key, n0)
        for data_ui_key in ("collapsed", "hiddenItems", "fieldLocks", "typeOverrides", "collapsedGuards"):
            self.assertNotIn(data_ui_key, n0["data"])

        prev_v = self.assertToolOk(
            self.call(
                "preview_nodemap_canvas",
                {
                    "name": "MCP Preview",
                    "version": "v3.0",
                    "nodes": [{"id": "n1", "type": "struct", "data": {"name": "foo", "hint": "include/linux/foo.h"}}],
                    "export_format": "dot",
                    "include_canvas": True,
                    "verbose": True,
                },
            )
        )
        self.assertNotIn("export_content", prev_v)
        self.assertIn("canvas", prev_v)
        self.assertIn("locks", prev_v["canvas"]["nodes"][0])
        self.assertIn("collapsed", prev_v["canvas"]["nodes"][0]["data"])

        # 3. Writes enabled + nodemap:write scope: still no extra tools, nothing requires a write scope
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

        read_tools = {t["name"]: t for t in rpc_with("mcp-read-token", "tools/list")["result"]["tools"]}
        write_tools = {t["name"]: t for t in rpc_with("mcp-write-token", "tools/list")["result"]["tools"]}
        self.assertEqual(set(read_tools), set(write_tools))
        self.assertEqual(set(read_tools), set(mcp.TOOLS))
        self.assertFalse([n for n, t in mcp.TOOLS.items() if t.required_scope])
        for name, tool in write_tools.items():
            self.assertTrue(tool["annotations"]["readOnlyHint"], name)
            self.assertFalse(tool["annotations"]["destructiveHint"], name)
        for name in removed:
            self.assertNotIn(name, write_tools)
            r = rpc_with("mcp-write-token", "tools/call", {"name": name, "arguments": {}})
            self.assertEqual(r["error"]["code"], mcp.INVALID_PARAMS)
        for name in ("preview_nodemap_canvas", "nest_nodemap_canvas", "group_nodemap_canvas", "inspect_nodemap_hierarchy",
                     "get_nodemap_canvas", "get_nodemap_hierarchy", "export_nodemap_canvas", "list_nodemap_canvases"):
            self.assertIn(name, read_tools)
        for name in ("nest_nodemap_canvas", "group_nodemap_canvas", "preview_nodemap_canvas"):
            self.assertIn("nothing is saved", mcp.TOOLS[name].description.lower())
        self.assertNotIn("insertable_into", read_tools["list_nodemap_canvases"]["inputSchema"]["properties"])

        for token in ("mcp-read-token", "mcp-write-token"):
            caps = rpc_with(token, "tools/call", {"name": "get_capabilities", "arguments": {}})["result"]["structuredContent"]
            self.assertTrue(caps["read_only"])
            self.assertTrue(caps["mcp"]["read_only"])
            self.assertEqual(caps["mcp"]["tools"], len(mcp.TOOLS))

    def test_nested_nodemap_stateless_tools_readonly_server_canvases_and_projection(self) -> None:
        canvas_dir = Path(self._tmp.name) / "canvases"
        (canvas_dir / "linux").mkdir(parents=True)
        cfg = core_config.get_config()
        cfg["api"]["allow_writes"] = True
        cfg["api"]["nodemap_canvas_dir"] = str(canvas_dir)
        cfg["api"]["keys"] = {"writer": {"key": "mcp-write-token", "scopes": ["read", "nodemap:write"]}}
        client = TestClient(create_app())

        def call(name: str, arguments: dict) -> dict:
            r = client.post(
                mcp.MCP_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
                headers={"Authorization": "Bearer mcp-write-token"},
            )
            self.assertEqual(r.status_code, 200, r.text)
            return r.json()["result"]

        def note(nid: str) -> dict:
            return {"id": nid, "type": "note", "x": 0, "y": 0, "w": 200, "h": 100, "data": {"text": nid}}

        def doc(nodes: list, edges: list | None = None) -> dict:
            return {"schema": "kip-nodemap-canvas", "version": 1, "nodes": nodes, "edges": edges or []}

        def files() -> list:
            return sorted(p.name for p in canvas_dir.rglob("*") if p.is_file())

        tools = mcp.TOOLS
        self.assertEqual(tools["group_nodemap_canvas"].input_schema["properties"]["node_ids"]["minItems"], 1)
        self.assertEqual(tools["nest_nodemap_canvas"].input_schema["required"], ["canvas"])
        for name in ("nest_nodemap_canvas", "group_nodemap_canvas"):
            self.assertIn("Import JSON", tools[name].description)
            self.assertIn("include_preview", tools[name].input_schema["properties"])

        # nest: result carries the complete importable canvas (not compacted); nothing saved
        nested = self.assertToolOk(call("nest_nodemap_canvas", {"canvas": doc([note("a")]), "name": "Kid", "node": {"id": "sub"}}))
        self.assertEqual(nested["node_id"], "sub")
        sub = {n["id"]: n for n in nested["canvas"]["nodes"]}["sub"]["data"]
        self.assertEqual(sub["summary"]["nodes"], 0)
        self.assertIn("canvas", sub)
        self.assertNotIn("preview", sub)
        second = self.assertToolOk(call("nest_nodemap_canvas", {
            "canvas": nested["canvas"], "path": ["sub"], "child": doc([note("leaf")]), "node": {"id": "sub2"},
        }))
        self.assertEqual(second["nested_depth"], 2)
        with_prev = self.assertToolOk(call("nest_nodemap_canvas", {"canvas": doc([note("a")]), "node": {"id": "s"}, "include_preview": True}))
        self.assertIn("preview", {n["id"]: n for n in with_prev["canvas"]["nodes"]}["s"]["data"])

        # group
        grouped = self.assertToolOk(call("group_nodemap_canvas", {
            "canvas": doc([note("a"), note("b"), note("c")], [{"id": "ab", "from": "a", "to": "b"}, {"id": "bc", "from": "b", "to": "c"}]),
            "node_ids": ["a", "b"], "node": {"id": "grp"},
        }))
        self.assertEqual(sorted(grouped["moved_node_ids"]), ["a", "b"])
        self.assertEqual(grouped["rewired_edge_ids"], ["bc"])
        gnodes = {n["id"]: n for n in grouped["canvas"]["nodes"]}
        self.assertEqual(sorted(n["id"] for n in gnodes["grp"]["data"]["canvas"]["nodes"]), ["a", "b"])
        self.assertToolError(call("group_nodemap_canvas", {"canvas": doc([note("a")]), "node_ids": []}), 422)
        self.assertToolError(call("group_nodemap_canvas", {"canvas": doc([note("a")]), "node_ids": ["zz"]}), 422)

        # strict validation: ports on nested nodes and unknown node types inside nested canvases are rejected
        ported = doc([note("b"), {"id": "n", "type": "nodemap", "data": {}}], [{"from": "b", "to": "n", "toField": "x"}])
        self.assertToolError(call("preview_nodemap_canvas", {"canvas": ported}), 422)
        bad_inner = doc([{"id": "n", "type": "nodemap", "data": {"canvas": doc([{"id": "q", "type": "bogus"}])}}])
        self.assertToolError(call("preview_nodemap_canvas", {"canvas": bad_inner}), 422)
        self.assertToolError(call("nest_nodemap_canvas", {"canvas": bad_inner}), 422)

        # preview: nested summaries kept, preview geometry only with include_preview (also in compact output)
        chain = doc([{"id": "n1", "type": "nodemap", "x": 0, "y": 0, "w": 300, "h": 200, "data": {"canvas": doc([note("x")])}}])
        prev = self.assertToolOk(call("preview_nodemap_canvas", {"canvas": chain, "hydrate": False}))
        pdata = prev["canvas"]["nodes"][0]["data"]
        self.assertEqual(pdata["canvas"]["nodes"][0]["id"], "x")
        self.assertEqual(pdata["summary"]["nodes"], 1)
        self.assertNotIn("preview", pdata)
        prev2 = self.assertToolOk(call("preview_nodemap_canvas", {"canvas": chain, "hydrate": False, "include_preview": True}))
        self.assertIn("preview", prev2["canvas"]["nodes"][0]["data"])

        # inline hierarchy
        hier = self.assertToolOk(call("inspect_nodemap_hierarchy", {"canvas": chain}))
        self.assertEqual(hier["nested_depth"], 1)
        self.assertToolError(call("inspect_nodemap_hierarchy", {"depth": 3}), 422)
        self.assertToolError(call("inspect_nodemap_hierarchy", {"canvas": chain, "depth": 99}), 422)
        self.assertEqual(files(), [])

        # read-only access to pre-existing on-disk canvases (reference-style chain p -> q, plus a missing ref)
        (canvas_dir / "linux" / "q.json").write_text(json.dumps({"id": "q", "name": "Q", "canvas": doc([note("qn")])}), encoding="utf-8")
        p_nodes = [
            note("pn"),
            {"id": "rq", "type": "nodemap", "x": 0, "y": 300, "w": 300, "h": 200, "data": {"canvasId": "q"}},
            {"id": "rg", "type": "nodemap", "x": 0, "y": 600, "w": 300, "h": 200, "data": {"canvasId": "gone"}},
        ]
        (canvas_dir / "linux" / "p.json").write_text(json.dumps({"id": "p", "name": "P", "canvas": doc(p_nodes)}), encoding="utf-8")
        snapshot = {f.name: f.read_text() for f in (canvas_dir / "linux").iterdir()}
        listed = self.assertToolOk(call("list_nodemap_canvases", {}))
        self.assertEqual(sorted(i["id"] for i in listed["items"]), ["p", "q"])
        got = self.assertToolOk(call("get_nodemap_canvas", {"canvas_id": "p"}))
        gdata = {n["id"]: n for n in got["canvas"]["nodes"]}
        self.assertEqual(gdata["rq"]["data"]["canvas"]["nodes"][0]["id"], "qn")
        self.assertNotIn("preview", gdata["rq"]["data"])
        self.assertNotIn("canvasId", gdata["rq"]["data"])
        self.assertTrue(any(w["code"] == "missing_reference" for w in got["warnings"]))
        self.assertNotIn("nested_maps", got)
        got_p = self.assertToolOk(call("get_nodemap_canvas", {"canvas_id": "p", "include_preview": True}))
        self.assertIn("preview", {n["id"]: n for n in got_p["canvas"]["nodes"]}["rq"]["data"])
        hier_saved = self.assertToolOk(call("get_nodemap_hierarchy", {"canvas_id": "p", "depth": 3}))
        self.assertNotIn("invalid_targets", hier_saved)
        self.assertToolError(call("get_nodemap_hierarchy", {"canvas_id": "p", "depth": 99}), 422)
        exported = self.assertToolOk(call("export_nodemap_canvas", {"canvas_id": "p"}))
        self.assertIn("content", exported)
        self.assertEqual({f.name: f.read_text() for f in (canvas_dir / "linux").iterdir()}, snapshot)

    def test_p1_p2_mcp_tools_readonly_posts_and_p3_exclusion(self) -> None:
        # 1. Read-only POST MCP tools remain visible in tools/list and callable when allow_writes=False
        tools = {t["name"]: t for t in self.rpc("tools/list", {}).json()["result"]["tools"]}
        for ro_post_tool in ("preview_nodemap_canvas", "nest_nodemap_canvas", "group_nodemap_canvas", "inspect_nodemap_hierarchy", "match_patch_maintainers", "check_watched_changes"):
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

        with patch("webapp.backend.services.mailing_list_service.mailing_list_service.search", return_value={
            "available": False, "degraded": True, "total": 0, "results": [], "error": "Foreign SQLite query failed",
        }):
            ms_err = self.call("search_mail", {"q": "ext3"})
            self.assertTrue(ms_err.get("isError"))
            self.assertIn("Foreign SQLite query failed", ms_err["structuredContent"]["error"])

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

    def test_preview_nodemap_canvas_strict_validation_errors(self) -> None:
        # Unknown node type ("file" instead of "source") -> actionable isError: true
        err_type = self.assertToolError(
            self.call("preview_nodemap_canvas", {"nodes": [{"id": "n1", "type": "file", "data": {"path": "kernel/foo.c"}}]}),
            422,
        )
        self.assertEqual(err_type["detail"][0]["loc"], ["nodes", 0, "type"])
        self.assertIn("source", err_type["detail"][0]["msg"])

        # Misplaced top-level node fields outside `data` -> actionable isError: true
        for misplaced_key, misplaced_val in (
            ("symbol", "FOO_BAR"),
            ("commit_hash", "deadbeef"),
            ("mid", "m1@example.com"),
            ("name", "foo"),
        ):
            err_mis = self.assertToolError(
                self.call("preview_nodemap_canvas", {"nodes": [{"id": "n1", "type": "struct", misplaced_key: misplaced_val}]}),
                422,
            )
            self.assertEqual(err_mis["detail"][0]["loc"], ["nodes", 0, misplaced_key])
            self.assertIn("data", err_mis["detail"][0]["msg"])

        # Invalid data field for node type (e.g., commit_hash inside commit.data instead of commitHash)
        err_data = self.assertToolError(
            self.call("preview_nodemap_canvas", {"nodes": [{"id": "n1", "type": "commit", "data": {"commit_hash": "deadbeef"}}]}),
            422,
        )
        self.assertEqual(err_data["detail"][0]["loc"], ["nodes", 0, "data", "commit_hash"])
        self.assertIn("commitHash", err_data["detail"][0]["msg"])

        # Invalid groups[] / tours[] fields -> actionable isError: true
        err_grp = self.assertToolError(
            self.call("preview_nodemap_canvas", {"groups": [{"id": "g1", "title": "Core", "bogus_group": 1}]}),
            422,
        )
        self.assertEqual(err_grp["detail"][0]["loc"], ["groups", 0, "bogus_group"])

        err_tour_id = self.assertToolError(
            self.call("preview_nodemap_canvas", {"tours": [{"title": "Missing ID", "steps": []}]}),
            422,
        )
        self.assertEqual(err_tour_id["detail"][0]["loc"], ["tours", 0, "id"])

        err_tour_step = self.assertToolError(
            self.call("preview_nodemap_canvas", {"tours": [{"id": "t1", "title": "Walk", "steps": [{"title": "S1", "nodes": ["n1"]}]}]}),
            422,
        )
        self.assertEqual(err_tour_step["detail"][0]["loc"], ["tours", 0, "steps", 0, "nodes"])

        # Strict validation also covers ops[] payloads
        err_ops = self.assertToolError(
            self.call(
                "preview_nodemap_canvas",
                {"ops": [{"op": "add_nodes", "nodes": [{"id": "n1", "type": "file", "name": "foo"}]}]},
            ),
            422,
        )
        self.assertEqual(err_ops["detail"][0]["loc"], ["ops", 0, "nodes", 0, "type"])

    def test_mcp_compact_projections_preserve_rest_defaults(self) -> None:
        # 1. get_mail_message: compact omits raw, heavy headers, duplicate commit_message/review_trailers
        sample_mail = {
            "available": True,
            "message_id": "m1@example.com",
            "subject": "[PATCH] foo",
            "headers": {
                "from": "Alice <alice@example.com>",
                "to": "lkml@vger.kernel.org",
                "cc": "Bob <bob@example.com>",
                "date": "Mon, 1 Jan 2024 00:00:00 +0000",
                "subject": "[PATCH] foo",
                "message-id": "<m1@example.com>",
                "in-reply-to": "<m0@example.com>",
                "references": "<m0@example.com>",
                "arc-seal": "i=1; ...",
                "dkim-signature": "v=1; ...",
                "received": "from mail.example.com ...",
                "authentication-results": "dkim=pass",
                "x-gm-message-state": "xyz",
                "x-google-dkim-signature": "abc",
                "x-received": "by 2002:...",
            },
            "raw": "From: Alice ...\n\nBody text",
            "body": "Body text",
            "commit_message": "Body text",
            "diffstat": "1 file changed",
            "diff": "--- a/foo.c\n+++ b/foo.c\n",
            "trailers": [{"tag": "Signed-off-by", "value": "Alice"}],
            "review_trailers": [{"tag": "Signed-off-by", "value": "Alice"}],
        }
        with patch("webapp.backend.services.mailing_list_service.mailing_list_service.get_message", return_value=sample_mail):
            rest_mail = self.client.get("/api/v1/mail/message?mid=m1@example.com", headers=self.auth).json()
            self.assertIn("raw", rest_mail)
            self.assertIn("dkim-signature", rest_mail["headers"])
            self.assertEqual(rest_mail["commit_message"], "Body text")

            mcp_mail = self.assertToolOk(self.call("get_mail_message", {"mid": "m1@example.com"}))
            self.assertNotIn("raw", mcp_mail)
            self.assertNotIn("commit_message", mcp_mail)
            self.assertNotIn("review_trailers", mcp_mail)
            self.assertEqual(
                set(mcp_mail["headers"].keys()),
                {"from", "to", "cc", "date", "subject", "message-id", "in-reply-to", "references"},
            )
            self.assertEqual(mcp_mail["body"], "Body text")
            self.assertEqual(mcp_mail["trailers"], [{"tag": "Signed-off-by", "value": "Alice"}])

            mcp_mail_raw = self.assertToolOk(
                self.call("get_mail_message", {"mid": "m1@example.com", "include_raw": True, "include_headers": "all"})
            )
            self.assertIn("raw", mcp_mail_raw)
            self.assertIn("dkim-signature", mcp_mail_raw["headers"])

        # 2. get_kconfig_symbol: deduplicates help, prompt/prompts, declarations, and compiled_files
        sample_kc = {
            "version": "v3.0",
            "arch": "x86_64",
            "name": "FOO_BAR",
            "type": 1,
            "type_name": "bool",
            "prompt": "Foo Bar support",
            "prompts": ["Foo Bar support"],
            "help": "Enable foo bar.",
            "file_path": "Kconfig",
            "line_s": 1,
            "line_e": 5,
            "definition": {
                "file_path": "Kconfig",
                "line_s": 1,
                "line_e": 5,
                "prompt": "Foo Bar support",
                "help": "Enable foo bar.",
            },
            "declarations": [{"file_path": "Kconfig", "line_s": 1, "line_e": 5}],
            "definitions": [
                {"file_path": "Kconfig", "line_s": 1, "line_e": 5, "prompt": "Foo Bar support", "help": "Enable foo bar."}
            ],
            "kbuild": [{"path": "kernel/foo.c", "target_obj": "foo.o", "exists": True}],
            "compiled_files": [{"file_path": "kernel/foo.c", "target_obj": "foo.o"}],
        }
        with patch.object(v1_router._kconfig_service, "get_symbol_detail", return_value=sample_kc):
            rest_kc = self.client.get("/api/v1/kconfig/symbol/FOO_BAR", headers=self.auth).json()
            self.assertIn("prompts", rest_kc)
            self.assertIn("compiled_files", rest_kc)
            self.assertEqual(rest_kc["definition"]["help"], "Enable foo bar.")

            mcp_kc = self.assertToolOk(self.call("get_kconfig_symbol", {"symbol_name": "FOO_BAR"}))
            self.assertEqual(mcp_kc["help"], "Enable foo bar.")
            self.assertEqual(mcp_kc["prompt"], "Foo Bar support")
            self.assertNotIn("prompts", mcp_kc)
            self.assertNotIn("declarations", mcp_kc)
            self.assertNotIn("compiled_files", mcp_kc)
            self.assertNotIn("help", mcp_kc["definition"])
            self.assertNotIn("help", mcp_kc["definitions"][0])

            mcp_kc_v = self.assertToolOk(self.call("get_kconfig_symbol", {"symbol_name": "FOO_BAR", "verbose": True}))
            self.assertIn("prompts", mcp_kc_v)
            self.assertIn("compiled_files", mcp_kc_v)
            self.assertEqual(mcp_kc_v["definition"]["help"], "Enable foo bar.")

        # 3. get_nodemap_struct, get_struct_layout, get_tree: omit null/false/0/empty/internal defaults unless verbose
        rest_nm = self.client.get("/api/v1/nodemap/struct?name=foo&path=include/linux/foo.h", headers=self.auth).json()
        self.assertIn("bits", rest_nm["items"][0])
        mcp_nm = self.assertToolOk(self.call("get_nodemap_struct", {"name": "foo", "path": "include/linux/foo.h"}))
        self.assertNotIn("outer_conds", mcp_nm)
        self.assertNotIn("bits", mcp_nm["items"][0])
        self.assertNotIn("array", mcp_nm["items"][0])
        self.assertNotIn("conds", mcp_nm["items"][0])
        mcp_nm_v = self.assertToolOk(
            self.call("get_nodemap_struct", {"name": "foo", "path": "include/linux/foo.h", "verbose": True})
        )
        self.assertIn("bits", mcp_nm_v["items"][0])

        sample_layout = {
            "version": "v3.0",
            "struct_name": "struct foo",
            "total_size": 4,
            "members": [
                {
                    "name": "a",
                    "type": "int",
                    "size": 4,
                    "alignment": 4,
                    "estimated": False,
                    "offset": 0,
                    "padding_before": 0,
                    "cacheline_idx": 0,
                    "cacheline_offset": 0,
                    "crosses_cacheline": False,
                    "bitfield_width": None,
                    "bit_offset": None,
                }
            ],
        }
        with patch.object(v1_router._pahole_service, "get_struct_layout", return_value=sample_layout):
            rest_sl = self.client.get("/api/v1/struct?name=foo", headers=self.auth).json()
            self.assertIn("bitfield_width", rest_sl["members"][0])
            mcp_sl = self.assertToolOk(self.call("get_struct_layout", {"name": "foo"}))
            m0 = mcp_sl["members"][0]
            for dropped in ("estimated", "padding_before", "crosses_cacheline", "bitfield_width", "bit_offset"):
                self.assertNotIn(dropped, m0)
            self.assertEqual(m0["offset"], 0)
            mcp_sl_v = self.assertToolOk(self.call("get_struct_layout", {"name": "foo", "verbose": True}))
            self.assertIn("bitfield_width", mcp_sl_v["members"][0])

        rest_tree = self.client.get("/api/v1/tree?path=kernel", headers=self.auth).json()
        self.assertIn("s_stat_label", rest_tree["entries"][0])
        mcp_tree = self.assertToolOk(self.call("get_tree", {"path": "kernel"}))
        f0 = mcp_tree["entries"][0]
        for dropped in ("fid", "ftype", "s_stat", "e_stat", "s_stat_label"):
            self.assertNotIn(dropped, f0)
        mcp_tree_v = self.assertToolOk(self.call("get_tree", {"path": "kernel", "verbose": True}))
        self.assertIn("s_stat_label", mcp_tree_v["entries"][0])

        # 4. Duplicate REST aliases remain accepted on REST while rejected on MCP
        with patch("webapp.backend.services.pahole_compare.compare", return_value={"struct": "foo"}):
            self.assertEqual(
                self.client.get("/api/v1/struct/compare?struct=foo&a=v3.0&b=master", headers=self.auth).status_code,
                200,
            )
        self.assertToolError(self.call("compare_struct_layout", {"struct": "foo", "a": "v3.0", "b": "master"}), 422)

    def test_server_instructions_and_catalog_size_budget(self) -> None:
        res = self.rpc("initialize", {"protocolVersion": mcp.SUPPORTED_PROTOCOL_VERSIONS[0], "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
        instructions = res["instructions"]
        for needle in ("get_capabilities", "preview_nodemap_canvas", "hydrate", "verbose=false", "include_canvas",
                       "export_format"):
            self.assertIn(needle, instructions)

        def size(obj) -> int:
            return len(json.dumps(obj, separators=(",", ":")).encode())

        tools = self.rpc("tools/list", {}).json()["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        # Token budget of the advertised catalog (83,769 B / 9,381 B before the slimming pass)
        self.assertLess(size({"tools": tools}), 74_000)
        self.assertLess(size(by_name["preview_nodemap_canvas"]), 7_000)
        # `repo` is documented once in the server instructions, not on every tool
        self.assertEqual(by_name["get_file_slice"]["inputSchema"]["properties"]["repo"],
                         {"type": "string", "minLength": 1, "maxLength": 128})
        data = by_name["preview_nodemap_canvas"]["inputSchema"]["properties"]["nodes"]["items"]["properties"]["data"]
        self.assertIn("symbol name [path", data["description"])
        self.assertIn("path", data["properties"])
        for prop in data["properties"].values():
            self.assertNotIn("maxLength", prop)
            self.assertNotIn("description", prop)
        self.assertIn("NodeMap", by_name["preview_nodemap_canvas"]["description"])

    def test_preview_nodemap_canvas_hydrates_symbol_path_and_mail_subject(self) -> None:
        from webapp.backend.services import nodemap_canvas_service as ncs

        detail = {
            "definition": {"ast_id": 11, "file": "fs/ext2/super.c", "type_name": "function"},
            "definitions": [
                {"ast_id": 11, "file": "fs/ext2/super.c", "type_name": "function"},
                {"ast_id": 22, "file": "fs/ext3/super.c", "type_name": "function"},
            ],
        }
        message = {"available": True, "subject": "[PATCH] ext2: mark as deprecated", "tid": 42}
        with patch.object(ncs._symbol_service, "get_symbol_detail", return_value=detail) as sym, patch(
            "webapp.backend.services.mailing_list_service.mailing_list_service.get_message", return_value=message
        ), patch("webapp.backend.services.mailing_list_service.mailing_list_service.get_thread",
                 return_value={"available": True, "tid": 7, "messages": [{"subject": "Remove ext3 filesystem driver"}]}):
            out = self.assertToolOk(self.call("preview_nodemap_canvas", {
                "nodes": [
                    {"id": "s1", "type": "symbol", "data": {"name": "ext3_fill_super", "path": "fs/ext3/super.c"}},
                    {"id": "m1", "type": "mail", "data": {"mid": "<x@y>"}},
                    {"id": "m2", "type": "mail", "data": {"tid": 7}},
                ],
                "verbose": True,
            }))
            self.assertIn("fs/ext3/super.c", [c.kwargs.get("from_file") for c in sym.call_args_list])
        nodes = {n["id"]: n for n in out["canvas"]["nodes"]}
        self.assertEqual(nodes["s1"]["data"]["astId"], 22)  # the named definition file wins
        self.assertEqual(nodes["s1"]["data"]["kind"], "func")
        self.assertNotIn("path", nodes["s1"]["data"])  # consumed by hydration, not a stored card field
        self.assertEqual(nodes["m1"]["data"]["subject"], "[PATCH] ext2: mark as deprecated")
        self.assertEqual(nodes["m1"]["data"]["tid"], 42)
        self.assertEqual(nodes["m1"]["title"], "[PATCH] ext2: mark as deprecated")
        self.assertEqual(nodes["m2"]["data"]["subject"], "Remove ext3 filesystem driver")

    def test_preview_nodemap_canvas_compact_output_reimports_identically(self) -> None:
        nodes = [
            {"id": "s1", "type": "struct", "data": {"name": "inode", "path": "include/linux/fs.h"}},
            {"id": "y1", "type": "symbol", "data": {"name": "ext2_fill_super", "kind": "func"}},
            {"id": "m1", "type": "mail", "data": {"mid": "abc@x", "subject": "ext2: mark as deprecated"}},
            {"id": "c1", "type": "commit", "data": {"commitHash": "abcdef123456"}},
            {"id": "src", "type": "source", "data": {"path": "fs/ext2/super.c", "mode": "range", "start": 10, "end": 20}},
            {"id": "k1", "type": "kconfig", "data": {"symbol": "EXT2_FS"}},
            {"id": "n1", "type": "note", "customTitle": "Hello", "title": "Hello", "data": {"text": "hi"}},
            {"id": "d1", "type": "diff", "data": {"commitHash": "abcdef123456"}},
            {"id": "h1", "type": "history", "data": {"path": "fs/ext2/super.c"}},
            {"id": "kp", "type": "kapi", "data": {}},
            {"id": "dc", "type": "doc", "data": {"path": "Documentation/filesystems/ext2.rst"}},
            {"id": "mt", "type": "maintainer", "data": {"query": "EXT2"}},
            {"id": "cv", "type": "cve", "data": {"cveId": "CVE-2024-1"}},
            {"id": "af", "type": "note", "autoFit": True, "scale": 1.5, "w": 300, "h": 200, "data": {"text": "x"}},
            {"id": "big", "type": "struct", "autoFit": True, "baseW": 2000, "baseH": 1500, "w": 2000, "h": 1500,
             "data": {"name": "task_struct"}},
            {"id": "g1", "type": "group", "title": "G"},
        ]
        edges = [
            {"id": "e1", "from": "s1", "to": "y1"},
            {"id": "e2", "from": "y1", "to": "m1", "label": "x", "style": "dashed"},
            {"id": "e3", "from": "m1", "to": "c1", "arrow": "both", "curve": "straight", "fromSide": "left"},
        ]
        args = {"nodes": nodes, "edges": edges, "hydrate": False}
        full = self.assertToolOk(self.call("preview_nodemap_canvas", {**args, "verbose": True}))["canvas"]
        compact = self.assertToolOk(self.call("preview_nodemap_canvas", args))["canvas"]
        full_b = len(json.dumps(full, separators=(",", ":")))
        compact_b = len(json.dumps(compact, separators=(",", ":")))
        self.assertLess(compact_b, full_b * 0.45)
        by_id = {n["id"]: n for n in compact["nodes"]}
        self.assertEqual(by_id["n1"]["customTitle"], "Hello")  # the rename lock stays
        self.assertNotIn("scale", by_id["s1"])
        self.assertNotIn("baseW", by_id["s1"])
        self.assertEqual(by_id["af"]["scale"], 1.5)  # an autoFit node keeps its base size
        self.assertIn("baseW", by_id["af"])
        self.assertEqual({k for k in compact["edges"][0]}, {"id", "from", "to"})
        self.assertEqual(compact["edges"][2]["fromSide"], "left")

        # JSON export in the same call: minified and compacted, still the importable document
        exp = self.assertToolOk(self.call("preview_nodemap_canvas", {**args, "export_format": "json"}))
        self.assertNotIn("canvas", exp)
        self.assertEqual(json.loads(exp["export"]), compact)
        both = self.assertToolOk(self.call("preview_nodemap_canvas", {**args, "export_format": "mermaid", "include_canvas": True}))
        self.assertIn("canvas", both)
        self.assertTrue(both["export"].startswith(("flowchart", "graph")))

        # Round trip through the webapp importer itself: identical normalized canvases
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not installed")
        model = Path(__file__).resolve().parent.parent / "webapp/frontend/js/views/nodemap/canvas_model.js"
        script = (
            "const fs = await import('node:fs'); const assert = (await import('node:assert/strict')).default;"
            f"const {{ normalizeCanvas }} = await import({json.dumps(model.as_uri())});"
            "const { full, compact } = JSON.parse(fs.readFileSync(0, 'utf8'));"
            "const a = normalizeCanvas(full), b = normalizeCanvas(compact);"
            "assert.ok(a && b); assert.deepEqual(b, a); console.log('ok', a.nodes.length);"
        )
        proc = subprocess.run([node_bin, "--input-type=module", "-e", script], input=json.dumps({"full": full, "compact": compact}),
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn(f"ok {len(nodes)}", proc.stdout)

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

                # No tool requires a write scope: both tokens see the identical read-only tool set
                def names_for(token: str, msg_id: int) -> set[str]:
                    r = self.client.post(
                        mcp.MCP_PATH,
                        json={"jsonrpc": "2.0", "id": msg_id, "method": "tools/list"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    self.assertEqual(r.status_code, 200)
                    return {t["name"] for t in r.json()["result"]["tools"]}

                ro_names, rw_names = names_for(ro_token, 1), names_for(rw_token, 3)
                self.assertEqual(ro_names, rw_names)
                self.assertIn("preview_nodemap_canvas", ro_names)
                self.assertIn("nest_nodemap_canvas", ro_names)
                self.assertNotIn("create_nodemap_canvas", rw_names)

                for token, msg_id in ((ro_token, 2), (rw_token, 4)):
                    r_call = self.client.post(
                        mcp.MCP_PATH,
                        json={
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "method": "tools/call",
                            "params": {"name": "create_nodemap_canvas", "arguments": {"name": "Blocked"}},
                        },
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    self.assertEqual(r_call.status_code, 200)
                    self.assertEqual(r_call.json()["error"]["code"], mcp.INVALID_PARAMS)
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
            # --allow-writes exposes no extra tools: no tool is write-scoped any more
            self.assertNotIn("create_nodemap_canvas", names)
            self.assertIn("nest_nodemap_canvas", names)
            self.assertEqual(names, set(mcp.TOOLS))
            # Cleanly restored on exit
            self.assertFalse(get_api_server_config()["allow_writes"])


if __name__ == "__main__":
    unittest.main()
