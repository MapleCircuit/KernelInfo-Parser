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


if __name__ == "__main__":
    unittest.main()
