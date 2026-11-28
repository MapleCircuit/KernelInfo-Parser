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

        m_types = re.search(r"export const NODE_TYPES\s*=\s*(?:Object\.freeze\()?\[(.*?)\](?:\))?;", js_src, re.DOTALL)
        self.assertIsNotNone(m_types)
        js_node_types = tuple(re.findall(r'"([^"]+)"', m_types.group(1)))
        self.assertEqual(tuple(pcs.NODE_TYPES), js_node_types)

        m_locks = re.search(r"export const LOCK_KINDS\s*=\s*(?:Object\.freeze\()?\[(.*?)\](?:\))?;", js_src, re.DOTALL)
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
        import json
        import tempfile
        from pathlib import Path

        app = FastAPI()
        app.include_router(nodemap_router)
        client = TestClient(app)

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "linux"
            repo_dir.mkdir()
            (repo_dir / "demo-map.json").write_text(json.dumps({
                "id": "demo-map", "name": "Demo Canvas", "description": "Pre-existing", "version": "v3.0",
                "canvas": {"schema": "kip-nodemap-canvas", "version": 1,
                           "nodes": [{"id": "n1", "type": "note", "title": "Note", "data": {"text": "hi"}}], "edges": []},
            }))
            with patch("webapp.backend.services.nodemap_canvas_service.get_nodemap_canvas_dir", return_value=Path(tmp)), \
                    patch("webapp.backend.services.nodemap_canvas_service.get_active_repo_name", return_value="linux"):
                r_list = client.get("/webapi/nodemap/canvases")
                self.assertEqual(r_list.status_code, 200)
                self.assertEqual(r_list.json()["total"], 1)
                self.assertEqual(r_list.json()["items"][0]["id"], "demo-map")

                r_get = client.get("/webapi/nodemap/canvases/demo-map")
                self.assertEqual(r_get.status_code, 200)
                self.assertEqual(r_get.json()["name"], "Demo Canvas")
                self.assertEqual(len(r_get.json()["canvas"]["nodes"]), 1)
                self.assertEqual(r_get.json()["warnings"], [])
                self.assertEqual(client.get("/webapi/nodemap/canvases/nope").status_code, 404)


def _cv(nodes: list | None = None, edges: list | None = None, tours: list | None = None) -> dict:
    return {"schema": "kip-nodemap-canvas", "version": 1, "nodes": nodes or [], "edges": edges or [], "tours": tours or []}


def _note(nid: str, x: int = 0, **extra) -> dict:
    return {"id": nid, "type": "note", "x": x, "y": 0, "data": {"text": nid}, **extra}


def _nest(nid: str, canvas: dict, **extra) -> dict:
    return {"id": nid, "type": "nodemap", "data": {"canvas": canvas}, **extra}


def _ref(nid: str, target: str, **extra) -> dict:
    return {"id": nid, "type": "nodemap", "data": {"canvasId": target}, **extra}


def _chain() -> dict:
    """A -> B -> C inline tree: A{a1, nb}, B{b1, nc}, C{c1} with one edge per level onto the nested node."""
    c = _cv([_note("c1")])
    b = _cv([_note("b1"), _nest("nc", c, title="C")], [{"id": "eb", "from": "b1", "to": "nc"}])
    return _cv([_note("a1"), _nest("nb", b, title="B")], [{"id": "ea", "from": "a1", "to": "nb", "toField": "x"}])


def _deep(levels: int) -> dict:
    cur = _cv([_note("leaf")])
    for i in range(levels):
        cur = _cv([_nest(f"n{i}", cur)])
    return cur


class NestedNodeMapTests(unittest.TestCase):
    """Nested NodeMaps (`nodemap` nodes): inline `data.canvas` trees, stateless nest/group/hierarchy, read-only
    server canvases and the absence of every canvas write (`webapp/backend/services/nodemap_nesting.py`,
    webapp/API_ENDPOINTS.md section 4.14)."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from webapp.backend.services.nodemap_canvas_service import (
            nodemap_canvas_service,
        )

        self.svc = nodemap_canvas_service
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo_dir = self.base / "linux"
        self.repo_dir.mkdir()
        for target, value in (("get_nodemap_canvas_dir", self.base), ("get_active_repo_name", "linux")):
            p = patch(f"webapp.backend.services.nodemap_canvas_service.{target}", return_value=value)
            p.start()
            self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(nodemap_router)
        self.client = TestClient(app)

    def _store(self, cid: str, nodes: list | None = None, edges: list | None = None) -> None:
        """Place a pre-existing canvas file on disk (the only way a server canvas comes to exist)."""
        import json

        (self.repo_dir / f"{cid}.json").write_text(
            json.dumps({"id": cid, "name": cid.upper(), "version": "v3.0", "canvas": _cv(nodes, edges)}),
            encoding="utf-8",
        )

    def _disk(self) -> list[tuple[str, int, int]]:
        return sorted((str(p.relative_to(self.base)), p.stat().st_size, p.stat().st_mtime_ns) for p in self.base.rglob("*"))

    def _http_error(self, fn, *args, **kwargs) -> int:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as cm:
            fn(*args, **kwargs)
        return cm.exception.status_code

    def test_schema_normalizes_inline_and_reference_nodes(self) -> None:
        from webapp.backend.services import nodemap_canvas_schema as pcs

        self.assertIn("nodemap", pcs.NODE_TYPES)
        self.assertNotIn("nodemap", pcs.AUTOFIT_TYPES)
        doc = pcs.normalize_canvas(_cv(
            [
                _ref("m1", "child-1"),
                {"id": "m2", "type": "nodemap", "data": {"canvasId": "../etc", "nodes": [{"id": "x"}]}},
                {"id": "m3", "type": "nodemap", "data": {
                    "canvas": {"nodes": [_note("i1")]}, "canvasId": "ignored",
                    "summary": {"nodes": 99}, "preview": {"w": 1}}},
                {"id": "s", "type": "struct", "data": {"name": "foo"}},
            ],
            [{"id": "e1", "from": "s", "to": "m1", "fromField": "a", "toField": "inner"}],
        ))
        by_id = {n["id"]: n for n in doc["nodes"]}
        self.assertEqual(by_id["m1"]["data"], {"canvasId": "child-1"})
        self.assertEqual(by_id["m2"]["data"], {"canvasId": ""})
        self.assertEqual(list(by_id["m3"]["data"]), ["canvas"])
        self.assertEqual(by_id["m3"]["data"]["canvas"]["schema"], "kip-nodemap-canvas")
        self.assertEqual([n["id"] for n in by_id["m3"]["data"]["canvas"]["nodes"]], ["i1"])
        self.assertEqual(by_id["m1"]["title"], "Nested NodeMap")
        self.assertEqual(doc["edges"][0]["fromField"], "a")
        self.assertIsNone(doc["edges"][0]["toField"])
        # The inline embedding budget is shared with Sub-Maps: level 9 is cut to an unset node
        cut = pcs.normalize_canvas(_deep(9))
        from webapp.backend.services.nodemap_nesting import tree_stats
        self.assertEqual(tree_stats(cut)["nested_depth"], 8)

    def test_no_canvas_write_routes_or_service_writes(self) -> None:
        import inspect
        import re

        from webapp.backend.services import nodemap_canvas_service as mod

        self._store("p", [_note("p1")])
        before = self._disk()
        body = {"id": "x", "canvas": _cv([_note("n")]), "ops": [], "node_ids": ["p1"], "target_id": "p"}
        for method, url in (
            ("post", "/webapi/nodemap/canvases"),
            ("put", "/webapi/nodemap/canvases/p"),
            ("patch", "/webapi/nodemap/canvases/p"),
            ("delete", "/webapi/nodemap/canvases/p"),
            ("post", "/webapi/nodemap/canvases/p/ops"),
            ("post", "/webapi/nodemap/canvases/p/nested"),
            ("post", "/webapi/nodemap/canvases/p/group"),
        ):
            kw = {} if method == "delete" else {"json": body}
            r = getattr(self.client, method)(url, **kw)
            self.assertIn(r.status_code, (404, 405), f"{method.upper()} {url} -> {r.status_code}")
        self.assertEqual(self._disk(), before)
        for gone in ("create_canvas", "replace_canvas", "delete_canvas", "mutate_canvas", "expand_canvas",
                     "layout_canvas", "attach_nested", "group_into_nested"):
            self.assertFalse(hasattr(self.svc, gone), gone)
        src = inspect.getsource(mod)
        for primitive in ("write_text", "write_bytes", "os.replace", ".unlink(", "mkstemp", "json.dump(", ".mkdir(", "rmtree"):
            self.assertNotIn(primitive, src, primitive)
        self.assertIsNone(re.search(r"open\([^)]*['\"][wax+]", src))

    def test_server_reference_chain_reads_inline(self) -> None:
        self._store("p", [_note("p1"), _ref("rq", "q", title="Q"), _ref("rx", "gone")], [{"id": "e", "from": "p1", "to": "rq"}])
        self._store("q", [_note("q1"), _ref("rr", "r")])
        self._store("r", [_note("r1"), _ref("back", "p")])
        before = self._disk()
        r = self.client.get("/webapi/nodemap/canvases/p")
        self.assertEqual(r.status_code, 200, r.text)
        rec = r.json()
        self.assertNotIn("nested_maps", rec)
        by_id = {n["id"]: n for n in rec["canvas"]["nodes"]}
        q = by_id["rq"]["data"]
        self.assertNotIn("canvasId", q)
        self.assertEqual(q["summary"], {"title": "Q", "nodes": 2, "edges": 0, "nested": 1, "depth": 1})
        self.assertNotIn("preview", q)
        r_inline = {n["id"]: n for n in q["canvas"]["nodes"]}["rr"]["data"]["canvas"]
        self.assertEqual({n["id"]: n for n in r_inline["nodes"]}["back"]["data"], {})
        self.assertEqual(by_id["rx"]["data"], {})
        self.assertEqual(
            sorted((w["code"], w["canvas_id"], tuple(w["path"])) for w in rec["warnings"]),
            [("missing_reference", "gone", ("p",)), ("reference_cycle", "p", ("p", "q", "r"))],
        )
        with_preview = self.svc.get_canvas("p", include_preview=True)
        self.assertIn("preview", {n["id"]: n for n in with_preview["canvas"]["nodes"]}["rq"]["data"])
        h = self.client.get("/webapi/nodemap/canvases/p/hierarchy", params={"depth": 3}).json()
        self.assertNotIn("invalid_targets", h)
        self.assertEqual(h["tree"]["children"][0]["canvas_id"], "q")
        self.assertEqual(h["ancestors"], ["q", "r"])
        listed = self.client.get("/webapi/nodemap/canvases", params={"insertable_into": "p"}).json()
        self.assertEqual(listed["total"], 3)
        self.assertNotIn("insertable", listed["items"][0])
        self.assertEqual(self._disk(), before)

    def test_preview_inline_chain_annotations_and_bounds(self) -> None:
        out = self.svc.preview_canvas({"canvas": _chain(), "hydrate": False, "auto_layout": None})
        self.assertEqual((out["nested_count"], out["nested_depth"], out["total_node_count"]), (2, 2, 5))
        nb = {n["id"]: n for n in out["canvas"]["nodes"]}["nb"]
        self.assertEqual(nb["data"]["summary"], {"title": "B", "nodes": 2, "edges": 1, "nested": 1, "depth": 1})
        self.assertNotIn("preview", nb["data"])
        self.assertIsNone(out["canvas"]["edges"][0]["toField"])
        nc = {n["id"]: n for n in nb["data"]["canvas"]["nodes"]}["nc"]
        self.assertEqual(nc["data"]["summary"]["depth"], 0)
        # Round trip: the returned canvas is importable input again; summaries are recomputed
        again = self.svc.preview_canvas({"canvas": out["canvas"], "hydrate": False, "auto_layout": None, "include_preview": True})
        nb2 = {n["id"]: n for n in again["canvas"]["nodes"]}["nb"]
        self.assertEqual(nb2["data"]["summary"], nb["data"]["summary"])
        self.assertEqual(len(nb2["data"]["preview"]["nodes"]), 2)
        # Layout runs at every level
        laid = self.svc.preview_canvas({"canvas": _chain(), "hydrate": False, "auto_layout": "dag"})
        self.assertEqual(laid["nested_depth"], 2)
        # Unresolved references are kept and reported
        warn = self.svc.preview_canvas({"canvas": _cv([_ref("r", "local-x")]), "hydrate": False})["warnings"]
        self.assertEqual([w["code"] for w in warn], ["unresolved_reference"])

    def test_inline_validation_rejects_internal_edges_depth_cycles_and_size(self) -> None:
        from webapp.backend.services import nodemap_nesting as nn

        internal = _chain()
        internal["edges"].append({"id": "bad", "from": "a1", "to": "c1"})
        self.assertEqual(self._http_error(self.svc.preview_canvas, {"canvas": internal, "hydrate": False}), 422)
        self.assertEqual(self._http_error(self.svc.preview_canvas, {"canvas": _chain(), "hydrate": False,
                                                                     "edges": [{"id": "x", "from": "b1", "to": "a1"}]}), 422)
        self.assertEqual(self._http_error(self.svc.preview_canvas, {"canvas": _deep(9), "hydrate": False}), 422)
        self.svc.preview_canvas({"canvas": _deep(8), "hydrate": False, "auto_layout": None})
        loop = _cv()
        loop["nodes"].append(_nest("self", loop))
        self.assertEqual(self._http_error(self.svc.preview_canvas, {"canvas": loop, "hydrate": False}), 422)
        errors, _ = nn.inline_problems(_cv([_nest("a", _cv([_note(f"x{i}") for i in range(30)]))]), max_total_nodes=20)
        self.assertEqual([e["code"] for e in errors], ["nested_too_large"])
        errors, _ = nn.inline_problems(internal)
        self.assertEqual(errors[0]["code"], "internal_edge_target")
        self.assertEqual((errors[0]["edge_id"], errors[0]["node_id"]), ("bad", "c1"))

    def test_nest_new_inline_and_server_children(self) -> None:
        self._store("q", [_note("q1"), _ref("rr", "r")])
        self._store("r", [_note("r1")])
        before = self._disk()
        out = self.svc.nest_canvas({"canvas": _chain(), "path": ["nb", "nc"], "child": _cv([_note("d1")]),
                                    "name": "D", "node": {"id": "nd", "x": 40, "y": 50}})
        self.assertEqual((out["node_id"], out["path"], out["nested_depth"], out["total_node_count"]), ("nd", ["nb", "nc"], 3, 7))
        from webapp.backend.services.nodemap_nesting import canvas_at_path

        level_c = canvas_at_path(out["canvas"], ["nb", "nc"])
        nd = {n["id"]: n for n in level_c["nodes"]}["nd"]
        self.assertEqual((nd["x"], nd["y"], nd["title"]), (40, 50, "D"))
        self.assertEqual([n["id"] for n in nd["data"]["canvas"]["nodes"]], ["d1"])
        empty = self.svc.nest_canvas({"canvas": _cv([_note("a")])})
        self.assertEqual(empty["nested_count"], 1)
        self.assertEqual({n["id"]: n for n in empty["canvas"]["nodes"]}[empty["node_id"]]["data"]["summary"]["nodes"], 0)
        copied = self.svc.nest_canvas({"canvas": _cv(), "server_canvas_id": "q"})
        node = copied["canvas"]["nodes"][0]
        self.assertEqual(node["title"], "Q")
        self.assertEqual(copied["nested_depth"], 2)
        self.assertEqual(copied["total_node_count"], 1 + 2 + 1)
        for bad in (
            {"canvas": _cv(), "child": _cv(), "server_canvas_id": "q"},
            {"canvas": _cv(), "path": ["missing"]},
            {"canvas": _cv([_note("a")]), "node": {"id": "a"}},
            {"canvas": "nope"},
            {"canvas": _deep(8), "path": [f"n{i}" for i in range(7, -1, -1)]},
        ):
            self.assertEqual(self._http_error(self.svc.nest_canvas, bad), 422, bad)
        self.assertEqual(self._http_error(self.svc.nest_canvas, {"canvas": _cv(), "server_canvas_id": "gone"}), 404)
        self.assertEqual(self._disk(), before)

    def test_group_selection_into_inline_child(self) -> None:
        canvas = _cv(
            [{"id": "g", "type": "group", "x": 0, "y": 0, "w": 900, "h": 500},
             _note("a", 100, groupId="g"), _note("b", 400, groupId="g"), _note("c", 700)],
            [{"id": "e1", "from": "a", "to": "b"}, {"id": "e2", "from": "b", "to": "c", "fromField": "f",
                                                     "waypoints": [{"x": 1, "y": 2}]}],
            [{"id": "t", "title": "T", "steps": [{"id": "s1", "nodeIds": ["a", "b", "c"], "edgeIds": ["e1", "e2"]}]}],
        )
        out = self.svc.group_canvas({"canvas": canvas, "node_ids": ["a", "b"], "name": "AB", "node": {"id": "nm"}})
        self.assertEqual((out["node_id"], out["moved_node_ids"], out["moved_edge_ids"], out["rewired_edge_ids"]),
                         ("nm", ["a", "b"], ["e1"], ["e2"]))
        by_id = {n["id"]: n for n in out["canvas"]["nodes"]}
        self.assertEqual(sorted(by_id), ["c", "g", "nm"])
        nm = by_id["nm"]
        self.assertEqual((nm["title"], nm["groupId"]), ("AB", "g"))
        child = nm["data"]["canvas"]
        self.assertEqual(sorted(n["id"] for n in child["nodes"]), ["a", "b"])
        self.assertTrue(all(n["groupId"] is None for n in child["nodes"]))
        self.assertEqual([e["id"] for e in child["edges"]], ["e1"])
        e2 = out["canvas"]["edges"][0]
        self.assertEqual((e2["from"], e2["to"], e2["fromField"], e2["waypoints"]), ("nm", "c", None, []))
        step = out["canvas"]["tours"][0]["steps"][0]
        self.assertEqual((step["nodeIds"], step["edgeIds"]), (["nm", "c"], ["e2"]))
        nested = self.svc.group_canvas({"canvas": _chain(), "path": ["nb"], "node_ids": ["b1", "nc"]})
        self.assertEqual((nested["nested_depth"], nested["moved_edge_ids"]), (3, ["eb"]))
        for bad in ({"canvas": canvas, "node_ids": []}, {"canvas": canvas, "node_ids": ["zz"]},
                    {"canvas": canvas, "node_ids": "a"}, {"canvas": canvas, "node_ids": ["a"], "path": ["c"]}):
            self.assertEqual(self._http_error(self.svc.group_canvas, bad), 422, bad)

    def test_inline_hierarchy(self) -> None:
        h = self.svc.inline_hierarchy({"canvas": _chain(), "depth": 1})
        self.assertEqual((h["source"], h["nested_depth"], h["total_node_count"]), ({"kind": "inline"}, 2, 5))
        self.assertEqual(h["tree"]["counts"], {"nodes": 2, "edges": 1, "nested": 1})
        child = h["tree"]["children"][0]
        self.assertEqual((child["node_id"], child["title"], child["depth"], child["children"]), ("nb", "B", 1, []))
        self.assertTrue(child["children_truncated"])
        full = self.svc.inline_hierarchy({"canvas": _chain()})
        self.assertEqual(full["tree"]["children"][0]["children"][0]["node_id"], "nc")
        self._store("p", [_ref("rq", "q")])
        self._store("q", [_note("q1")])
        srv = self.svc.inline_hierarchy({"canvas_id": "p"})
        self.assertEqual((srv["source"]["kind"], srv["nested_depth"]), ("server", 1))
        for bad in ({}, {"canvas": _cv(), "canvas_id": "p"}, {"canvas": _cv(), "depth": 0}, {"canvas": _cv(), "depth": True}):
            self.assertEqual(self._http_error(self.svc.inline_hierarchy, bad), 422, bad)

    def test_high_quality_preview_flag_survives_normalization_inlining_and_validation(self) -> None:
        """`data.highQualityPreview` is a boolean display option: kept only when true, carried through
        reference inlining, accepted by strict validation as a bool only; never a picture payload."""
        from api.routers.nodemap import validate_nodemap_payload_strict
        from webapp.backend.services import nodemap_canvas_schema as pcs
        from webapp.backend.services.nodemap_nesting import inline_references

        doc = pcs.normalize_canvas(_cv([
            {"id": "r", "type": "nodemap", "data": {"canvasId": "kid", "highQualityPreview": True, "png": "data:image/png;base64,AA"}},
            {"id": "i", "type": "nodemap", "data": {"canvas": _cv([_note("x")]), "highQualityPreview": True}},
            {"id": "f", "type": "nodemap", "data": {"canvasId": "kid", "highQualityPreview": "yes"}},
            _ref("p", "kid"),
        ]))
        by_id = {n["id"]: n for n in doc["nodes"]}
        self.assertEqual(by_id["r"]["data"], {"canvasId": "kid", "highQualityPreview": True})
        self.assertIs(by_id["i"]["data"]["highQualityPreview"], True)
        self.assertEqual(by_id["f"]["data"], {"canvasId": "kid"})
        self.assertEqual(by_id["p"]["data"], {"canvasId": "kid"})
        kid = pcs.normalize_canvas(_cv([_note("k1")]))
        host = pcs.normalize_canvas(_cv([
            {"id": "r", "type": "nodemap", "data": {"canvasId": "kid", "highQualityPreview": True}},
            {"id": "m", "type": "nodemap", "data": {"canvasId": "gone", "highQualityPreview": True}},
        ]))
        warn = inline_references(host, lambda cid: kid if cid == "kid" else None)
        self.assertEqual([w["code"] for w in warn], ["missing_reference"])
        inlined = {n["id"]: n["data"] for n in host["nodes"]}
        self.assertEqual(set(inlined["r"]), {"canvas", "highQualityPreview"})
        self.assertEqual(inlined["m"], {"highQualityPreview": True})
        self.assertNotIn("data:image", str(host))
        ok = validate_nodemap_payload_strict({"nodes": [{"id": "n", "type": "nodemap", "data": {"canvasId": "kid", "highQualityPreview": False}}]})
        self.assertEqual(ok, [])
        bad = validate_nodemap_payload_strict({"nodes": [{"id": "n", "type": "nodemap", "data": {"canvasId": "kid", "highQualityPreview": "on"}}]})
        self.assertEqual([e["type"] for e in bad], ["bool_type"])
        extra = validate_nodemap_payload_strict({"nodes": [{"id": "n", "type": "nodemap", "data": {"canvasId": "kid", "snapshot": "data:image/svg+xml,x"}}]})
        self.assertEqual([e["type"] for e in extra], ["extra_forbidden"])
        # the typed request model (REST / MCP bodies) accepts the flag as a boolean only
        from pydantic import ValidationError

        from api.routers.nodemap import NodemapNodeDataInput

        self.assertIs(NodemapNodeDataInput(canvasId="kid", highQualityPreview=True).highQualityPreview, True)
        with self.assertRaises(ValidationError):
            NodemapNodeDataInput(canvasId="kid", highQualityPreview="on")
        with self.assertRaises(ValidationError):
            NodemapNodeDataInput(canvasId="kid", snapshot="data:image/svg+xml,x")

    def test_pure_helpers(self) -> None:
        from webapp.backend.services.nodemap_canvas_schema import normalize_canvas
        from webapp.backend.services.nodemap_nesting import (
            ancestors_of,
            build_static_preview,
            inline_references,
            tree_stats,
        )

        g = {"b": ["c"], "c": ["a"], "d": ["b"]}
        self.assertEqual(ancestors_of(g, "a"), {"b", "c", "d"})
        self.assertEqual(build_static_preview({"nodes": [], "edges": []})["nodes"], [])
        big = normalize_canvas(_cv([_note(f"x{i}") for i in range(30)]))
        host = normalize_canvas(_cv([_ref("r1", "big"), _ref("r2", "big")]))
        warn = inline_references(host, lambda cid: big, max_total_nodes=50)
        self.assertEqual([w["code"] for w in warn], ["nested_too_large"])
        self.assertEqual(tree_stats(host)["total_node_count"], 32)

if __name__ == "__main__":
    unittest.main()
