"""tests/test_webapp_nodemap.py - NodeMap struct layouts with preprocessor guards and source slices
(`parser/struct_source.py`, `webapp/backend/services/nodemap_service.py`, webapp/API_ENDPOINTS.md section 4.14)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from parser.struct_source import (
    extract_struct_layout,
    find_definition_span,
    outer_conditions,
)
from tests.db_fixtures import make_db, patch_reader
from webapp.backend.routers.nodemap import router as nodemap_router
from webapp.backend.services import nodemap_service as ns
from webapp.backend.services.symbol_service import SymbolService

SRC = """#include <x.h>
#ifdef CONFIG_A
/* { #if never */
struct foo
{
	int a, *b;  // } #if never
	unsigned int f1:3, f2 : 4;
	int : 0;
	char buf[16][2];
	void (*fn)(int, char *);
#ifdef CONFIG_B
	u64 x __aligned(8);
	union {
		int u1;
#if defined(Z)
		int u2;
#elif Y
		int u3;
#else
		int u4;
#endif
	};
#endif
	struct list_head list;
	struct { int q; } named, other;
	enum e { A, B } ev;
	DECLARE_BITMAP(bm, 64);
} __packed;
#endif
#define M(x) \\
   do { } while (0)
typedef struct bar { int z; } bar_t;
typedef union { int i; } uni_t;
enum col { R, G };
int proto(int x);
static int
func(int a) __must_hold(x)
{ if (a) { return "}"; } return 0; }
"""


def _walk(items):
    for it in items:
        yield it
        if it["kind"] == "aggregate":
            yield from _walk(it["children"])


class StructSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.span = find_definition_span(SRC, "foo")
        self.layout = extract_struct_layout(SRC, self.span[1], self.span[2])
        self.items = self.layout["items"]
        self.by_name = {i["name"]: i for i in _walk(self.items) if i["kind"] != "directive" and i["name"]}

    def test_spans(self) -> None:
        self.assertEqual(self.span, ("struct", 4, 28))
        self.assertEqual(find_definition_span(SRC, "M"), ("macro", 30, 31))
        self.assertEqual(find_definition_span(SRC, "bar"), ("struct", 32, 32))
        self.assertEqual(find_definition_span(SRC, "bar_t"), ("struct", 32, 32))
        self.assertEqual(find_definition_span(SRC, "uni_t"), ("union", 33, 33))
        self.assertEqual(find_definition_span(SRC, "col"), ("enum", 34, 34))
        self.assertEqual(find_definition_span(SRC, "func"), ("function", 36, 38))
        self.assertEqual(find_definition_span(SRC, "func", "function"), ("function", 36, 38))
        self.assertIsNone(find_definition_span(SRC, "func", "struct"))
        self.assertIsNone(find_definition_span(SRC, "proto"))
        self.assertIsNone(find_definition_span(SRC, "missing"))
        self.assertIsNone(find_definition_span(SRC, "foo", "bogus"))

    def test_struct_with_attribute_between_name_and_brace(self) -> None:
        text = "struct a __attribute__((packed)) {\n int x;\n};\nstruct s *get(void) {\n return 0;\n}\n"
        self.assertEqual(find_definition_span(text, "a"), ("struct", 1, 3))
        self.assertEqual(find_definition_span(text, "get"), ("function", 4, 6))
        self.assertIsNone(find_definition_span(text, "s"))

    def test_outer_conds(self) -> None:
        self.assertEqual(outer_conditions(SRC, 4), ["defined(CONFIG_A)"])
        self.assertEqual(outer_conditions(SRC, 1), [])
        self.assertEqual(outer_conditions(SRC, 30), [])

    def test_fields(self) -> None:
        f = self.by_name
        self.assertEqual((f["a"]["type"], f["a"]["decl"], f["a"]["line"]), ("int", "int a", 6))
        self.assertEqual((f["b"]["type"], f["b"]["decl"]), ("int *", "int *b"))
        self.assertEqual((f["f1"]["bits"], f["f2"]["bits"], f["f2"]["type"]), (3, 4, "unsigned int"))
        self.assertEqual(f["buf"]["array"], "[16][2]")
        self.assertEqual(f["buf"]["type"], "char")
        self.assertEqual((f["fn"]["type"], f["fn"]["decl"]), ("void (*)(int, char *)", "void (*fn)(int, char *)"))
        self.assertEqual(f["x"]["type"], "u64")
        self.assertEqual(f["list"]["type"], "struct list_head")
        self.assertEqual(f["bm"]["type"], "DECLARE_BITMAP")
        anon = [i for i in self.items if i["kind"] == "field" and i["name"] == "" and i["bits"] == 0]
        self.assertEqual(len(anon), 1)

    def test_comment_content_ignored(self) -> None:
        self.assertEqual([d["line"] for d in self.items if d["kind"] == "directive"], [11, 23])

    def test_conditions_and_depth(self) -> None:
        f = self.by_name
        self.assertEqual(f["a"]["conds"], [])
        self.assertEqual(f["x"]["conds"], ["defined(CONFIG_B)"])
        self.assertEqual(f["u2"]["conds"], ["defined(CONFIG_B)", "(defined(Z))"])
        self.assertEqual(f["u3"]["conds"], ["defined(CONFIG_B)", "!((defined(Z)))", "(Y)"])
        self.assertEqual(f["u4"]["conds"], ["defined(CONFIG_B)", "!((defined(Z)))", "!((Y))"])
        self.assertEqual(f["list"]["conds"], [])
        self.assertTrue(self.layout["has_guards"])
        union = next(i for i in self.items if i["kind"] == "aggregate" and i["tag"] == "union")
        self.assertEqual((union["name"], union["type_name"], union["line"], union["line_e"]), ("", "", 13, 22))
        self.assertEqual(union["conds"], ["defined(CONFIG_B)"])
        dirs = [(d["directive"], d["expr"], d["depth"]) for d in union["children"] if d["kind"] == "directive"]
        self.assertEqual(dirs, [("if", "defined(Z)", 1), ("elif", "Y", 1), ("else", "", 1), ("endif", "", 1)])
        outer = [(d["directive"], d["depth"]) for d in self.items if d["kind"] == "directive"]
        self.assertEqual(outer, [("ifdef", 0), ("endif", 0)])

    def test_multiple_declarators_on_aggregate(self) -> None:
        self.assertEqual(self.by_name["named"]["kind"], "aggregate")
        self.assertEqual(self.by_name["named"]["children"][0]["name"], "q")
        self.assertEqual(self.by_name["other"]["kind"], "field")
        self.assertEqual(self.by_name["ev"]["type"], "enum e { ... }")

    def test_named_aggregate_member_and_no_guards(self) -> None:
        text = "struct o {\n\tstruct in { int a; } v;\n\tunion { int x; long y; };\n};\n"
        out = extract_struct_layout(text, 1, 4)
        self.assertFalse(out["has_guards"])
        first, second = out["items"]
        self.assertEqual((first["name"], first["type_name"], first["tag"]), ("v", "struct in", "struct"))
        self.assertEqual((second["name"], second["type_name"], len(second["children"])), ("", "", 2))

    def test_no_body(self) -> None:
        self.assertEqual(extract_struct_layout("int x;\n", 1, 1), {"items": [], "has_guards": False})

    def test_if0_elif_chain_with_guarded_nested_union(self) -> None:
        """`#if 0` keeps its members (condition `(0)`); an `#elif` branch guards a union whose own `#ifdef` nests."""
        src = ("struct s {\n\tint a;\n#if 0\n\tint dead;\n#elif defined(CONFIG_A)\n\tunion {\n#ifdef CONFIG_B\n"
               "\t\tlong b;\n#else\n\t\tint c; /* \"}\" */\n#endif\n\t} u;\n#else\n\tchar *msg; // \"{\"\n#endif\n};\n")
        self.assertEqual(find_definition_span(src, "s", "struct"), ("struct", 1, 16))
        items = extract_struct_layout(src, 1, 16)["items"]
        by = {it.get("name") or f"#{it.get('directive')}@{it['line']}": it for it in items}
        self.assertEqual(by["dead"]["conds"], ["(0)"])
        u = by["u"]
        self.assertEqual(u["conds"], ["!((0))", "(defined(CONFIG_A))"])
        inner = {it.get("name") or it["directive"]: it for it in u["children"]}
        self.assertEqual(inner["b"]["conds"], [*u["conds"], "defined(CONFIG_B)"])
        self.assertEqual(inner["c"]["conds"], [*u["conds"], "!(defined(CONFIG_B))"])
        self.assertEqual((inner["ifdef"]["depth"], inner["else"]["depth"], inner["endif"]["depth"]), (1, 1, 1))
        self.assertEqual(by["msg"]["conds"], ["!((0))", "!((defined(CONFIG_A)))"])
        self.assertEqual([it["directive"] for it in items if it["kind"] == "directive"], ["if", "elif", "else", "endif"])


DEFS = [
    {"def_id": 1, "ast_id": 1, "name": "foo", "type_id": 1, "blob_id": 7, "line_s": 4, "line_e": 28, "tag_id": 1, "linkage": 0},
    {"def_id": 2, "ast_id": 2, "name": "func", "type_id": 2, "blob_id": 7, "line_s": 36, "line_e": 38, "tag_id": 1, "linkage": 0},
]
TYPES = [{"type_id": 1, "name": "C_structdecl"}, {"type_id": 2, "name": "C_functionproto"}]


def _scope(self, reader, version_name):
    return 1, version_name, {7: ["include/foo.h"]}


def _git(content: bytes | None, is_dir: bool = False):
    return patch(
        "webapp.backend.services.nodemap_service.git_reader.read_file",
        return_value=(content, is_dir, "s" * 40 if content is not None else None),
    )


class NodeMapApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(nodemap_router)
        self.client = TestClient(app)
        ns._LAYOUT_CACHE.clear()
        self.db = make_db("mock", rows={"m_blob_symbol_def": DEFS, "m_type_descriptor": TYPES})

    def _db_patches(self):
        return (patch_reader(self.db), patch.object(SymbolService, "_scope", _scope),
                patch("webapp.backend.services.symbol_service.definition_arch", return_value=("x86_64", None, 64)),
                patch("webapp.backend.services.symbol_service.best_path", lambda paths, scope, bits: paths[0]),
                patch("webapp.backend.services.symbol_service.definition_rank", return_value=0))

    def _get(self, url: str, db: bool, content: bytes | None = None, is_dir: bool = False):
        patches = list(self._db_patches()) if db else [patch.object(SymbolService, "ranked_definitions", return_value=[])]
        patches.append(_git(content, is_dir))
        from contextlib import ExitStack
        with ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            return self.client.get(url)

    def test_struct_from_db(self) -> None:
        r = self._get("/webapi/nodemap/v3.0/struct/foo", True, SRC.encode())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["span_source"], body["guard_source"], body["kind"]), ("db", "source", "struct"))
        self.assertEqual((body["file_path"], body["line_s"], body["line_e"]), ("include/foo.h", 4, 28))
        self.assertEqual(body["outer_conds"], ["defined(CONFIG_A)"])
        self.assertTrue(body["has_guards"])
        self.assertEqual(body["items"][0]["name"], "a")

    def test_struct_prefix_and_kind_filter(self) -> None:
        r = self._get("/webapi/nodemap/v3.0/struct/struct%20foo?kind=union", True, SRC.encode())
        self.assertEqual(r.status_code, 404)
        r = self._get("/webapi/nodemap/v3.0/struct/struct%20foo?kind=struct&line=10&path=include/foo.h", True, SRC.encode())
        self.assertEqual(r.status_code, 200)
        r = self._get("/webapi/nodemap/v3.0/struct/foo?path=other.h", True, SRC.encode())
        self.assertEqual(r.status_code, 200)   # the DB definition is in another file: the hinted file is read from Git
        self.assertEqual((r.json()["span_source"], r.json()["file_path"]), ("git", "other.h"))

    def test_struct_git_fallback(self) -> None:
        r = self._get("/webapi/nodemap/v9.9/struct/bar?path=a/b.h", False, SRC.encode())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["span_source"], body["file_path"], body["line_s"], body["line_e"]), ("git", "a/b.h", 32, 32))
        r = self._get("/webapi/nodemap/v9.9/struct/uni_t?path=a/b.h", False, SRC.encode())
        self.assertEqual(r.json()["kind"], "union")

    def test_struct_errors(self) -> None:
        self.assertEqual(self._get("/webapi/nodemap/v9.9/struct/foo", False, SRC.encode()).status_code, 404)
        self.assertIn("path", self._get("/webapi/nodemap/v9.9/struct/foo", False).json()["detail"])
        self.assertEqual(self._get("/webapi/nodemap/v9.9/struct/foo?path=a.h", False, None).status_code, 404)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/struct/nope?path=a.h", False, SRC.encode()).status_code, 404)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/struct/a%3Bb", False).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/struct/foo?path=../x", False).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/struct/foo?kind=enum&path=a.h", False).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/--output=x/struct/foo?path=a.h", False).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/struct/foo?path=a.h", False, b"\0\1").status_code, 415)

    def test_source_symbol_db_and_context(self) -> None:
        r = self._get("/webapi/nodemap/v3.0/source?symbol=func&context=2", True, SRC.encode())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["path"], "include/foo.h")
        self.assertEqual((body["start_line"], body["end_line"]), (34, 38 + 0 if body["total_lines"] <= 38 else 40))
        self.assertEqual(body["symbol"], {"name": "func", "kind": "function", "line_s": 36, "line_e": 38, "span_source": "db"})
        self.assertEqual(len(body["lines"]), body["end_line"] - body["start_line"] + 1)

    def test_source_symbol_git(self) -> None:
        r = self._get("/webapi/nodemap/v9.9/source?symbol=M&path=a.h", False, SRC.encode())
        body = r.json()
        self.assertEqual(body["symbol"], {"name": "M", "kind": "macro", "line_s": 30, "line_e": 31, "span_source": "git"})
        self.assertEqual(body["lines"][0], "#define M(x) \\")
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?symbol=M", False, SRC.encode()).status_code, 404)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?symbol=zz&path=a.h", False, SRC.encode()).status_code, 404)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?symbol=M&kind=struct&path=a.h", False, SRC.encode()).status_code, 404)

    def test_source_ranges(self) -> None:
        body = "\n".join(f"l{i}" for i in range(1, 11)) + "\n"
        r = self._get("/webapi/nodemap/v9.9/source?path=a.c", False, body.encode()).json()
        self.assertEqual((r["total_lines"], r["start_line"], r["end_line"], r["truncated"], r["symbol"]), (10, 1, 10, False, None))
        r = self._get("/webapi/nodemap/v9.9/source?path=a.c&start=3&end=99", False, body.encode()).json()
        self.assertEqual((r["start_line"], r["end_line"], r["lines"][0]), (3, 10, "l3"))
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=a.c&start=99", False, body.encode()).json()["start_line"], 10)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=a.c&start=5&end=2", False, body.encode()).status_code, 400)

    def test_source_truncation_and_errors(self) -> None:
        big = "\n".join("x" for _ in range(7000)).encode()
        r = self._get("/webapi/nodemap/v9.9/source?path=a.c", False, big).json()
        self.assertEqual((r["truncated"], r["end_line"], len(r["lines"]), r["total_lines"]), (True, 6000, 6000, 7000))
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source", False, big).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=a.c&context=201", False, big).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=a.c&kind=x", False, big).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=a.c&start=0", False, big).status_code, 422)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=..%2Fa", False, big).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=a.c&symbol=a%3Bb", False, big).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=a.c", False, None).status_code, 404)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=d", False, None, True).status_code, 400)
        self.assertEqual(self._get("/webapi/nodemap/v9.9/source?path=a.bin", False, b"\0\0").status_code, 415)


class EmbedFramingHeaderTests(unittest.TestCase):
    """The NodeMap frames the app's own `/?embed=1` shell: same-origin framing only."""

    def _client(self) -> TestClient:
        from starlette.responses import PlainTextResponse

        from webapp.backend.security.middleware import SecurityHeadersMiddleware

        app = FastAPI()

        @app.get("/")
        def shell() -> PlainTextResponse:
            return PlainTextResponse("shell")

        @app.get("/strict")
        def strict() -> PlainTextResponse:
            return PlainTextResponse("svg", headers={"Content-Security-Policy": "sandbox; default-src 'none'", "X-Frame-Options": "ALLOWALL"})

        app.add_middleware(SecurityHeadersMiddleware)
        return TestClient(app)

    def test_default_policy_allows_same_origin_frames_only(self) -> None:
        r = self._client().get("/?embed=1")
        csp = r.headers["content-security-policy"]
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertNotIn("frame-ancestors 'none'", csp)
        self.assertEqual(r.headers["x-frame-options"], "SAMEORIGIN")

    def test_route_policy_is_kept(self) -> None:
        r = self._client().get("/strict")
        self.assertEqual(r.headers["content-security-policy"], "sandbox; default-src 'none'")
        self.assertEqual(r.headers.get_list("x-frame-options"), ["SAMEORIGIN"])


class NodeMapCanvasSchemaAndWebapiTests(unittest.TestCase):
    """Verify Python NodeMap canvas schema parity with `canvas_model.js` and `/webapi/nodemap/canvases` routes."""

    def test_python_schema_matches_frontend_canvas_model_js(self) -> None:
        import re
        from pathlib import Path
        from webapp.backend.services import nodemap_canvas_schema as pcs

        js_path = (
            Path(__file__).resolve().parent.parent
            / "webapp"
            / "frontend"
            / "js"
            / "views"
            / "nodemap"
            / "canvas_model.js"
        )
        js_src = js_path.read_text(encoding="utf-8")

        m_types = re.search(r"export const NODE_TYPES\s*=\s*(?:Object\.freeze\()?\[(.*?)\](?:\))?;", js_src, re.S)
        self.assertIsNotNone(m_types)
        js_node_types = tuple(re.findall(r'"([^"]+)"', m_types.group(1)))
        self.assertEqual(tuple(pcs.NODE_TYPES), js_node_types)

        m_locks = re.search(r"export const LOCK_KINDS\s*=\s*(?:Object\.freeze\()?\[(.*?)\](?:\))?;", js_src, re.S)
        self.assertIsNotNone(m_locks)
        js_lock_kinds = tuple(re.findall(r'"([a-z0-9_-]+)"', m_locks.group(1)))
        for lk in pcs.LOCK_KINDS:
            if lk:
                self.assertIn(lk, js_lock_kinds)

        # Normalization invariants: dangling/self-loop edge pruning and group cycle pruning
        doc = pcs.normalize_canvas(
            {
                "schema": "kip-nodemap-canvas",
                "version": 1,
                "nodes": [
                    {"id": "g1", "type": "group", "title": "G1", "groupId": "g2"},
                    {"id": "g2", "type": "group", "title": "G2", "groupId": "g1"},
                    {"id": "n1", "type": "struct", "data": {"name": "foo"}},
                ],
                "edges": [
                    {"id": "e_self", "from": "n1", "to": "n1"},
                    {"id": "e_dangling", "from": "n1", "to": "missing"},
                    {"id": "e_ok", "from": "n1", "to": "g1"},
                ],
            }
        )
        self.assertIsNotNone(doc)
        by_id = {n["id"]: n for n in doc["nodes"]}
        # Cycle between g1 and g2 must be broken
        self.assertFalse(by_id["g1"]["groupId"] == "g2" and by_id["g2"]["groupId"] == "g1")
        # Self-loop and dangling edges pruned
        self.assertEqual([e["id"] for e in doc["edges"]], ["e_ok"])

    def test_webapi_nodemap_canvases_endpoints(self) -> None:
        import tempfile
        from pathlib import Path
        from webapp.backend.services.nodemap_canvas_service import nodemap_canvas_service

        app = FastAPI()
        app.include_router(nodemap_router)
        client = TestClient(app)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("webapp.backend.services.nodemap_canvas_service.get_nodemap_canvas_dir", return_value=Path(tmp)):
                nodemap_canvas_service.create_canvas(
                    canvas_id="demo-map",
                    name="Demo Canvas",
                    description="Saved for UI test",
                    version="v3.0",
                    canvas={"nodes": [{"id": "n1", "type": "note", "title": "Note", "data": {"text": "hi"}}], "edges": []},
                )
                r_list = client.get("/webapi/nodemap/canvases")
                self.assertEqual(r_list.status_code, 200)
                self.assertEqual(r_list.json()["total"], 1)
                self.assertEqual(r_list.json()["items"][0]["id"], "demo-map")

                r_get = client.get("/webapi/nodemap/canvases/demo-map")
                self.assertEqual(r_get.status_code, 200)
                self.assertEqual(r_get.json()["name"], "Demo Canvas")
                self.assertEqual(len(r_get.json()["canvas"]["nodes"]), 1)


class NestedNodeMapTests(unittest.TestCase):
    """Nested NodeMap references (`nodemap` nodes): cycles, previews, missing targets, hierarchy, attach, group
    (`webapp/backend/services/nodemap_nesting.py`, webapp/API_ENDPOINTS.md section 4.14)."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from webapp.backend.services.nodemap_canvas_service import (
            nodemap_canvas_service,
        )

        self.svc = nodemap_canvas_service
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch("webapp.backend.services.nodemap_canvas_service.get_nodemap_canvas_dir", return_value=Path(self._tmp.name))
        p.start()
        self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(nodemap_router)
        self.client = TestClient(app)

    def _create(self, cid: str, nodes: list | None = None, edges: list | None = None) -> dict:
        r = self.client.post(
            "/webapi/nodemap/canvases",
            json={"id": cid, "name": cid.upper(), "canvas": {"nodes": nodes or [], "edges": edges or []}},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    @staticmethod
    def _ref(nid: str, target: str, **extra) -> dict:
        return {"id": nid, "type": "nodemap", "data": {"canvasId": target}, **extra}

    def _ops(self, cid: str, ops: list) -> object:
        return self.client.post(f"/webapi/nodemap/canvases/{cid}/ops", json={"ops": ops})

    def test_schema_normalizes_nodemap_nodes_and_clears_ports(self) -> None:
        from webapp.backend.services import nodemap_canvas_schema as pcs

        self.assertIn("nodemap", pcs.NODE_TYPES)
        self.assertNotIn("nodemap", pcs.AUTOFIT_TYPES)
        doc = pcs.normalize_canvas({
            "schema": "kip-nodemap-canvas", "version": 1,
            "nodes": [
                self._ref("m1", "child-1"),
                {"id": "m2", "type": "nodemap", "data": {"canvasId": "../etc", "nodes": [{"id": "x"}]}},
                {"id": "s", "type": "struct", "data": {"name": "foo"}},
            ],
            "edges": [{"id": "e1", "from": "s", "to": "m1", "fromField": "a", "toField": "inner"}],
        })
        by_id = {n["id"]: n for n in doc["nodes"]}
        self.assertEqual(by_id["m1"]["data"], {"canvasId": "child-1"})
        self.assertEqual(by_id["m2"]["data"], {"canvasId": ""})
        self.assertEqual(by_id["m1"]["title"], "Nested NodeMap")
        self.assertEqual(doc["edges"][0]["fromField"], "a")
        self.assertIsNone(doc["edges"][0]["toField"])

    def test_arbitrary_depth_and_shared_child(self) -> None:
        self._create("c", [{"id": "c1", "type": "note"}, {"id": "c2", "type": "note", "x": 400}], [{"id": "ce", "from": "c1", "to": "c2"}])
        self._create("b", [self._ref("nb", "c"), {"id": "b1", "type": "note"}])
        self._create("a", [self._ref("na", "b")])
        self._create("p2", [self._ref("np2", "c")])
        h = self.client.get("/webapi/nodemap/canvases/a/hierarchy").json()
        self.assertEqual(h["tree"]["canvas_id"], "a")
        lvl1 = h["tree"]["children"][0]
        self.assertEqual((lvl1["node_id"], lvl1["canvas_id"], lvl1["status"]), ("na", "b", "ok"))
        lvl2 = lvl1["children"][0]
        self.assertEqual((lvl2["canvas_id"], lvl2["counts"]), ("c", {"nodes": 2, "edges": 1, "nested": 0}))
        self.assertNotIn("nodes", lvl2)
        hc = self.client.get("/webapi/nodemap/canvases/c/hierarchy").json()
        self.assertEqual([p["canvas_id"] for p in hc["parents"]], ["b", "p2"])
        self.assertEqual(hc["ancestors"], ["a", "b", "p2"])
        self.assertEqual(hc["invalid_targets"], ["a", "b", "c", "p2"])
        shallow = self.client.get("/webapi/nodemap/canvases/a/hierarchy?depth=1").json()
        self.assertTrue(shallow["tree"]["children"][0]["children_truncated"])
        self.assertEqual(self.client.get("/webapi/nodemap/canvases/a/hierarchy?depth=0").status_code, 422)
        self.assertEqual(self.client.get("/webapi/nodemap/canvases/zz/hierarchy").status_code, 404)

    def test_cycles_rejected_on_every_write(self) -> None:
        self._create("c")
        self._create("b", [self._ref("nb", "c")])
        self._create("a", [self._ref("na", "b")])
        # A -> A (create, attach, ops)
        r = self.client.post("/webapi/nodemap/canvases", json={"id": "s", "canvas": {"nodes": [self._ref("x", "s")]}})
        self.assertEqual(r.status_code, 422)
        self.assertIn("s -> s", r.json()["detail"])
        self.assertEqual(self.client.post("/webapi/nodemap/canvases/a/nested", json={"target_id": "a"}).status_code, 422)
        # A -> B -> A
        r = self._ops("b", [{"op": "upsert_nodes", "nodes": [self._ref("back", "a")]}])
        self.assertEqual(r.status_code, 422)
        self.assertIn("b -> a -> b", r.json()["detail"])
        # A -> B -> C -> A (replace and attach)
        r = self.client.put("/webapi/nodemap/canvases/c", json={"canvas": {"nodes": [self._ref("loop", "a")]}})
        self.assertEqual(r.status_code, 422)
        self.assertIn("c -> a -> b -> c", r.json()["detail"])
        r = self.client.post("/webapi/nodemap/canvases/c/nested", json={"target_id": "a"})
        self.assertEqual(r.status_code, 422)
        # Nothing was written
        self.assertEqual(self.client.get("/webapi/nodemap/canvases/c").json()["revision"], 1)
        self.assertEqual(self.client.get("/webapi/nodemap/canvases/b").json()["nested_refs"], ["c"])

    def test_missing_target_and_port_into_nested_node_rejected(self) -> None:
        self._create("c")
        self._create("a", [self._ref("na", "c"), {"id": "n", "type": "note"}])
        r = self._ops("a", [{"op": "upsert_nodes", "nodes": [self._ref("nx", "ghost")]}])
        self.assertEqual(r.status_code, 422)
        self.assertIn("ghost", r.json()["detail"])
        r = self._ops("a", [{"op": "upsert_edges", "edges": [{"from": "n", "to": "na", "toField": "inner.field"}]}])
        self.assertEqual(r.status_code, 422)
        self.assertIn("as a whole", r.json()["detail"])
        r = self._ops("a", [{"op": "upsert_edges", "edges": [{"id": "ok", "from": "n", "to": "na"}]}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["canvas"]["edges"][0]["to"], "na")

    def test_parent_read_is_lightweight_and_tolerates_missing_children(self) -> None:
        child_nodes = [
            {"id": f"k{i}", "type": "source", "x": i * 30, "y": i * 10, "data": {"path": "kernel/fork.c"}}
            for i in range(260)
        ]
        child_edges = [{"id": f"ke{i}", "from": f"k{i}", "to": f"k{i + 1}"} for i in range(259)]
        self._create("big", child_nodes, child_edges)
        self._create("gone")
        parent = self._create("p", [self._ref("n1", "big"), self._ref("n2", "big"), self._ref("n3", "gone")])
        self.assertEqual(parent["nested_count"], 3)
        self.assertEqual(sorted(parent["nested_maps"]), ["big", "gone"])
        r = self.client.get("/webapi/nodemap/canvases/p").json()
        self.assertEqual(len(r["canvas"]["nodes"]), 3)
        card = r["nested_maps"]["big"]
        self.assertEqual(card["status"], "ok")
        self.assertEqual(card["name"], "BIG")
        self.assertEqual(card["counts"], {"nodes": 260, "edges": 259, "nested": 0})
        prev = card["preview"]
        self.assertTrue(prev["truncated"])
        self.assertLessEqual(len(prev["nodes"]), 200)
        self.assertLessEqual(max(prev["w"], prev["h"]), 1000)
        self.assertEqual(set(prev["nodes"][0]), {"x", "y", "w", "h", "type", "color"})
        self.assertNotIn("kernel/fork.c", str(r["nested_maps"]))
        self.assertNotIn("canvas", card)

        d = self.client.delete("/webapi/nodemap/canvases/gone").json()
        self.assertEqual(d["referenced_by"], ["p"])
        r = self.client.get("/webapi/nodemap/canvases/p")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["nested_maps"]["gone"], {"canvas_id": "gone", "status": "missing"})
        tree = self.client.get("/webapi/nodemap/canvases/p/hierarchy").json()["tree"]
        self.assertEqual([c["status"] for c in tree["children"]], ["ok", "ok", "missing"])
        # A kept dangling reference does not block editing the parent; a new missing target does
        self.assertEqual(self._ops("p", [{"op": "upsert_nodes", "nodes": [{"id": "t", "type": "note"}]}]).status_code, 200)
        self.assertEqual(self._ops("p", [{"op": "upsert_nodes", "nodes": [self._ref("n4", "gone-too")]}]).status_code, 422)

    def test_insertable_listing_and_attach_new_or_existing(self) -> None:
        self._create("c")
        self._create("b", [self._ref("nb", "c")])
        items = self.client.get("/webapi/nodemap/canvases?insertable_into=c").json()["items"]
        self.assertEqual({i["id"]: i["insertable"] for i in items}, {"b": False, "c": False})
        r = self.client.post("/webapi/nodemap/canvases/b/nested", json={"name": "Fresh", "new_canvas_id": "fresh", "node": {"x": 500, "y": 40}})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["target_id"], "fresh")
        node = next(n for n in body["parent"]["canvas"]["nodes"] if n["id"] == body["node_id"])
        self.assertEqual((node["type"], node["x"], node["title"]), ("nodemap", 500, "Fresh"))
        self.assertEqual(body["parent"]["nested_maps"]["fresh"]["counts"], {"nodes": 0, "edges": 0, "nested": 0})
        self.assertEqual(self.client.get("/webapi/nodemap/canvases/fresh").json()["name"], "Fresh")
        r = self.client.post("/webapi/nodemap/canvases/fresh/nested", json={"target_id": "c", "expected_revision": 1})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["created"])
        self.assertEqual(self.client.post("/webapi/nodemap/canvases/fresh/nested", json={"target_id": "c", "expected_revision": 1}).status_code, 409)
        self.assertEqual(self.client.post("/webapi/nodemap/canvases/b/nested", json={"new_canvas_id": "c"}).status_code, 409)

    def test_group_selection_into_nested_canvas(self) -> None:
        self._create("c")
        nodes = [
            {"id": "out", "type": "note", "x": 0, "y": 0},
            {"id": "g", "type": "group", "x": 300, "y": 300, "w": 400, "h": 300},
            {"id": "in1", "type": "note", "x": 320, "y": 350, "groupId": "g"},
            {"id": "in2", "type": "note", "x": 500, "y": 350},
            self._ref("deep", "c", x=700, y=700),
        ]
        edges = [
            {"id": "e_int", "from": "in1", "to": "in2", "waypoints": [{"x": 400, "y": 400}]},
            {"id": "e_in", "from": "out", "to": "in1"},
            {"id": "e_out", "from": "in2", "to": "out", "fromField": "x"},
            {"id": "e_deep", "from": "in2", "to": "deep"},
        ]
        self._create("p", nodes, edges)
        self.client.post("/webapi/nodemap/canvases/p/ops", json={"ops": [{"op": "upsert_tour", "tour": {"id": "t1", "steps": [{"id": "s1", "nodeIds": ["in1", "out", "in2"], "edgeIds": ["e_int", "e_in"]}]}}]})
        r = self.client.post("/webapi/nodemap/canvases/p/group", json={"node_ids": ["g", "in2", "deep"], "name": "Inner", "new_canvas_id": "inner"})
        self.assertEqual(r.status_code, 200, r.text)
        g = r.json()
        nid = g["node_id"]
        self.assertEqual(sorted(g["moved_node_ids"]), ["deep", "g", "in1", "in2"])
        self.assertEqual(sorted(g["moved_edge_ids"]), ["e_deep", "e_int"])
        self.assertEqual(sorted(g["rewired_edge_ids"]), ["e_in", "e_out"])
        self.assertEqual((g["child"]["node_count"], g["child"]["edge_count"], g["child"]["nested_count"]), (4, 2, 1))
        parent = g["parent"]["canvas"]
        self.assertEqual(sorted(n["id"] for n in parent["nodes"]), sorted(["out", nid]))
        pe = {e["id"]: e for e in parent["edges"]}
        self.assertEqual((pe["e_in"]["from"], pe["e_in"]["to"]), ("out", nid))
        self.assertEqual((pe["e_out"]["from"], pe["e_out"]["fromField"]), (nid, None))
        step = parent["tours"][0]["steps"][0]
        self.assertEqual((step["nodeIds"], step["edgeIds"]), ([nid, "out"], ["e_in"]))
        nested = next(n for n in parent["nodes"] if n["id"] == nid)
        self.assertEqual((nested["type"], nested["data"]["canvasId"], nested["x"], nested["y"]), ("nodemap", "inner", 300, 300))
        child = self.client.get("/webapi/nodemap/canvases/inner").json()
        cn = {n["id"]: n for n in child["canvas"]["nodes"]}
        self.assertEqual(cn["in1"]["groupId"], "g")
        self.assertEqual((cn["g"]["x"], cn["g"]["y"]), (80, 80))
        ce = {e["id"]: e for e in child["canvas"]["edges"]}
        self.assertEqual(ce["e_int"]["waypoints"], [{"x": 180, "y": 180}])
        self.assertEqual(child["nested_maps"]["c"]["status"], "ok")
        self.assertEqual(self.client.get("/webapi/nodemap/canvases/p/hierarchy").json()["tree"]["children"][0]["children"][0]["canvas_id"], "c")
        # Invalid selections
        self.assertEqual(self.client.post("/webapi/nodemap/canvases/p/group", json={"node_ids": []}).status_code, 422)
        self.assertEqual(self.client.post("/webapi/nodemap/canvases/p/group", json={"node_ids": ["nope"]}).status_code, 422)
        self.assertEqual(self.client.post("/webapi/nodemap/canvases/p/group", json={"node_ids": ["out"], "expected_revision": 1}).status_code, 409)
        self.assertEqual(self.client.get("/webapi/nodemap/canvases").json()["total"], 3)

    def test_group_rolls_back_child_when_parent_write_fails(self) -> None:
        self._create("p", [{"id": "n1", "type": "note"}, {"id": "n2", "type": "note", "x": 300}])
        real = self.svc._write_envelope_atomic

        def flaky(path, env):
            if env["id"] == "p":
                raise OSError("disk full")
            return real(path, env)

        with patch.object(self.svc, "_write_envelope_atomic", side_effect=flaky), self.assertRaises(OSError):
            self.svc.group_into_nested("p", {"node_ids": ["n1"], "new_canvas_id": "kid"})
        self.assertEqual(self.client.get("/webapi/nodemap/canvases/kid").status_code, 404)
        self.assertEqual(len(self.client.get("/webapi/nodemap/canvases/p").json()["canvas"]["nodes"]), 2)

    def test_writes_can_be_disabled(self) -> None:
        with patch("webapp.backend.routers.nodemap.get_config", return_value={"webapp": {"nodemap_canvas_writes": False}}):
            r = self.client.post("/webapi/nodemap/canvases", json={"id": "x"})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(self.client.get("/webapi/nodemap/canvases").status_code, 200)

    def test_pure_helpers(self) -> None:
        from webapp.backend.services.nodemap_nesting import (
            ancestors_of,
            build_static_preview,
            find_cycle,
        )

        g = {"b": ["c"], "c": ["a"], "d": ["b"]}
        self.assertEqual(find_cycle(g, "a", ["b"]), ["a", "b", "c", "a"])
        self.assertEqual(find_cycle(g, "a", ["d"]), ["a", "d", "b", "c", "a"])
        self.assertIsNone(find_cycle(g, "x", ["b", "missing"]))
        self.assertEqual(ancestors_of(g, "a"), {"b", "c", "d"})
        deep = {f"m{i}": [f"m{i + 1}"] for i in range(5000)}
        self.assertIsNone(find_cycle(deep, "root", ["m0"]))
        self.assertEqual(build_static_preview({"nodes": [], "edges": []})["nodes"], [])


if __name__ == "__main__":
    unittest.main()
