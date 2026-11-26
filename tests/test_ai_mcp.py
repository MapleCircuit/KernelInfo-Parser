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
        self.assertEqual(len(mixed[1]["result"]["tools"]), len(mcp.TOOLS))
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
        self.assertEqual(names, [route.endpoint.__name__ for route in routes])
        self.assertEqual(len(names), len(set(names)))

        # Catalog parity: every REST GET endpoint is a tool and is advertised in /api/v1/capabilities
        catalog_get = {e["path"] for e in v1_router._ENDPOINT_CATALOG if e["method"] == "GET"}
        self.assertEqual(catalog_get, {route.path for route in routes})

        for tool in tools:
            self.assertRegex(tool["name"], r"^[a-z][a-z0-9_]*$")
            self.assertTrue(tool["description"])
            self.assertEqual(
                tool["annotations"],
                {"title": tool["title"], "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            )
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("repo", schema["properties"])
            self.assertTrue(set(schema.get("required", [])) <= set(schema["properties"]), tool["name"])
            self.assertNotIn("repo", schema.get("required", []))
            for key, prop in schema["properties"].items():
                self.assertIn(prop.get("type"), _ALLOWED_TYPES, f"{tool['name']}.{key}: {prop}")
                self.assertNotIn("title", prop)
                self.assertNotIn("anyOf", prop)
                if prop["type"] == "array":
                    self.assertEqual(prop["items"], {"type": "string"})
            json.dumps(schema)  # JSON-serializable

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
        self.assertEqual(len(frames[2]["result"]["tools"]), len(mcp.TOOLS))
        self.assertEqual(frames[3], [{"jsonrpc": "2.0", "id": 3, "result": {}}])
        self.assertTrue(frames[4]["result"]["isError"])
        self.assertEqual(frames[5]["error"]["code"], mcp.PARSE_ERROR)

    def test_oversized_lines_are_rejected_and_skipped(self) -> None:
        limit = 64
        stream = io.BytesIO(b"x" * 200 + b"\n" + b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n' + b"tail-without-newline")
        lines = list(mcp_stdio.read_lines(stream, limit))
        self.assertEqual(len(lines), 3)
        self.assertEqual(len(lines[0]), limit + 1)
        self.assertEqual(json.loads(lines[1]), {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        with patch.object(mcp, "MAX_BODY_BYTES", limit):
            out = io.BytesIO()
            mcp_stdio.serve(lines, out)
        frames = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(frames[0]["error"]["code"], mcp.INVALID_REQUEST)
        self.assertEqual(frames[1]["result"], {})
        self.assertEqual(frames[2]["error"]["code"], mcp.PARSE_ERROR)

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
            self.assertEqual(len(frames[1]["result"]["tools"]), len(mcp.TOOLS))
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


if __name__ == "__main__":
    unittest.main()
