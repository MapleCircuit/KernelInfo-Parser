"""tests/test_ai_api.py - Integration and unit tests for /api/v1/* endpoints in api/."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.routers import v1 as v1_router
from core import config as core_config
from core.DBLayout import TABLES
from tests.db_fixtures import make_db, patch_reader

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
}


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _init_sample_git_repo(repo: Path, files: dict[str, str], tag: str) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "master")
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"Initial commit for {tag}")
    _git(repo, "tag", tag)
    return _git(repo, "rev-parse", "HEAD")


class TestAiApiEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_cfg = copy.deepcopy(core_config._ACTIVE_CONFIG)
        self._orig_cfg_path = core_config._LOADED_CONFIG_PATH
        # reset_config() first: it drops the variables sync_environ exported, which a restored snapshot
        # would otherwise bring back as user-set overrides for later test modules
        core_config.reset_config()
        self._orig_env = dict(os.environ)

        self._tmp = tempfile.TemporaryDirectory()
        self.linux_dir = Path(self._tmp.name) / "linux"
        self.man_dir = Path(self._tmp.name) / "man-pages"

        self.linux_sha = _init_sample_git_repo(
            self.linux_dir,
            {
                "include/linux/foo.h": "struct foo {\n    int a;\n};\n",
                "kernel/foo.c": "\n".join(
                    [
                        "/* SPDX-License-Identifier: GPL-2.0 */",
                        "#include <linux/foo.h>",
                        "int foo_helper(int x) {",
                        "    return x + 1;",
                        "}",
                        "int main_entry(void) {",
                        "    return foo_helper(41);",
                        "}",
                    ]
                )
                + "\n",
                "Kconfig": "config FOO_BAR\n\tbool \"Foo Bar support\"\n\thelp\n\t  Enable foo bar.\n",
                "MAINTAINERS": "FOO SUBSYSTEM\nM:\tAlice Dev <alice@example.com>\nS:\tMaintained\nF:\tkernel/foo.c\n",
                "Documentation/foo.rst": ".. _foo_doc:\n\nFoo Title\n=========\n\nDetails.\n",
            },
            "v3.0",
        )
        self.man_sha = _init_sample_git_repo(
            self.man_dir,
            {
                "man2/read.2": ".TH READ 2 2024-01-01 \"Linux man-pages 6.10\"\n.SH NAME\nread \\- read from a file descriptor\n",
            },
            "man-pages-6.10",
        )

        self.cfg_path = Path(self._tmp.name) / "config.json"
        self.cfg_path.write_text(
            json.dumps(
                {
                    "workspace": {"auto_detect_repos": False},
                    "repositories": [
                        {
                            "name": "linux",
                            "project": "linux",
                            "path": str(self.linux_dir),
                            "default": True,
                            "default_branch": "master",
                        },
                        {
                            "name": "man-pages",
                            "project": "man-pages",
                            "path": str(self.man_dir),
                            "default_branch": "master",
                        },
                    ],
                    "api": {
                        "default_version": "v3.0",
                        "keys": {"test-agent": "ai-secret-token"},
                    },
                }
            ),
            encoding="utf-8",
        )
        core_config.init_config(self.cfg_path)

        self.db = make_db("mock", tables=TABLES)
        self.db.insert_rows("m_project", ["project_id", "name", "slug"], [(1, "Linux", "linux"), (2, "Man Pages", "man-pages")])
        self.db.insert_rows(
            "m_git_network",
            ["network_id", "project_id", "name"],
            [(1, 1, "linux"), (2, 2, "man-pages")],
        )
        self.db.insert_rows(
            "m_repository",
            ["repo_id", "network_id", "name", "git_url"],
            [(1, 1, "linux", "https://example.com/linux.git"), (2, 2, "man-pages", "https://example.com/man-pages.git")],
        )
        self.db.insert_rows(
            "m_maintainer_person",
            ["person_id", "name", "email"],
            [(1, "Test Author", "author@example.com")],
        )
        self.db.insert_rows(
            "m_commit",
            ["commit_id", "network_id", "commit_hash", "author_id", "author_date", "committer_id", "committer_date", "subject", "message"],
            [
                (10, 1, bytes.fromhex(self.linux_sha), 1, 1700000000, 1, 1700000000, "Initial commit for v3.0", "Initial commit for v3.0"),
                (20, 2, bytes.fromhex(self.man_sha), 1, 1700000100, 1, 1700000100, "Initial commit for man-pages-6.10", ""),
            ],
        )
        self.db.insert_rows(
            "m_git_ref",
            ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id"],
            [
                (1, 1, 1, "v3.0", 10),
                (2, 1, 2, "master", 10),
                (3, 2, 1, "man-pages-6.10", 20),
            ],
        )
        self.db.insert_rows(
            "m_ingest_state",
            ["network_id", "commit_id", "status", "updated_at"],
            [(1, 10, 2, 1700000500), (2, 20, 2, 1700000600)],
        )

        self._reader_patch = patch_reader(self.db)
        self._reader_patch.start()
        self.client = TestClient(create_app())
        self.auth = {"Authorization": "Bearer ai-secret-token"}

    def tearDown(self) -> None:
        self._reader_patch.stop()
        core_config.reset_config()
        os.environ.clear()
        os.environ.update(self._orig_env)
        core_config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core_config._LOADED_CONFIG_PATH = self._orig_cfg_path
        self._tmp.cleanup()

    def test_capabilities_and_multi_repo_selection(self) -> None:
        r = self.client.get("/api/v1/capabilities", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["active_repo"], "linux")
        self.assertEqual(body["default_version"], "v3.0")
        self.assertEqual(body["openapi_url"], "/api/openapi.json")
        self.assertTrue(body["features"]["kconfig"])
        self.assertGreaterEqual(len(body["endpoints"]), 20)

        # Repo selection via X-KI-Repo header and ?repo= query parameter
        r_hdr = self.client.get("/api/v1/capabilities", headers={**self.auth, "X-KI-Repo": "man-pages"})
        self.assertEqual(r_hdr.status_code, 200)
        self.assertEqual(r_hdr.json()["active_repo"], "man-pages")
        self.assertFalse(r_hdr.json()["features"]["kconfig"])
        self.assertTrue(r_hdr.json()["features"]["man"])

        r_qs = self.client.get("/api/v1/repos?repo=man-pages", headers=self.auth)
        self.assertEqual(r_qs.status_code, 200)
        self.assertEqual(r_qs.json()["active"], "man-pages")
        active_flags = {p["repo"]: p["active"] for p in r_qs.json()["repositories"]}
        self.assertEqual(active_flags, {"linux": False, "man-pages": True})

    def test_repos_branches_versions_status_and_pagination(self) -> None:
        r_br = self.client.get("/api/v1/branches?limit=1&offset=0", headers=self.auth)
        self.assertEqual(r_br.status_code, 200)
        self.assertEqual(r_br.json()["repo"], "linux")
        self.assertGreaterEqual(len(r_br.json()["branches"]), 1)

        r_ver = self.client.get("/api/v1/versions?q=v3&indexed_only=true&limit=10", headers=self.auth)
        self.assertEqual(r_ver.status_code, 200)
        self.assertEqual(r_ver.json()["versions"][0]["vname"], "v3.0")
        self.assertFalse(r_ver.json()["truncated"])

        r_status = self.client.get("/api/v1/status", headers=self.auth)
        self.assertEqual(r_status.status_code, 200)
        self.assertEqual(r_status.json()["repository"], "linux")
        self.assertEqual(r_status.json()["catalog"]["parsed"], 1)

    def test_filesystem_tree_search_slice_outline_imports_and_refs(self) -> None:
        r_tree = self.client.get("/api/v1/tree?limit=2&offset=0", headers=self.auth)
        self.assertEqual(r_tree.status_code, 200)
        self.assertTrue(r_tree.json()["truncated"])
        self.assertEqual(len(r_tree.json()["entries"]), 2)

        r_fs = self.client.get("/api/v1/files/search?q=foo.c", headers=self.auth)
        self.assertEqual(r_fs.status_code, 200)
        self.assertEqual(r_fs.json()["files"][0]["path"], "kernel/foo.c")

        # Line slice with truncation and exact end_line
        r_slice = self.client.get("/api/v1/file?path=kernel/foo.c&start_line=1&limit=3", headers=self.auth)
        self.assertEqual(r_slice.status_code, 200)
        b = r_slice.json()
        self.assertTrue(b["truncated"])
        self.assertEqual((b["start_line"], b["end_line"], b["total_lines"]), (1, 3, 8))
        self.assertIn("foo_helper", b["content"])

        r_exact = self.client.get("/api/v1/file?path=kernel/foo.c&start_line=3&end_line=5", headers=self.auth)
        self.assertEqual(r_exact.status_code, 200)
        self.assertFalse(r_exact.json()["truncated"])

        # Traversal and bad revision rejected with 400
        self.assertEqual(self.client.get("/api/v1/file?path=../etc/passwd", headers=self.auth).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/file?path=kernel/foo.c&version=-bad", headers=self.auth).status_code, 400)

        with patch.object(v1_router.outline_service, "get_outline", return_value={
            "version": "v3.0", "path": "kernel/foo.c", "indexed": True,
            "items": [
                {"name": "foo_helper", "kind": "function", "line_s": 3, "line_e": 5, "children": []},
                {"name": "main_entry", "kind": "function", "line_s": 6, "line_e": 8, "children": []},
            ],
            "truncated": False,
        }):
            r_out = self.client.get("/api/v1/file/outline?path=kernel/foo.c&limit=1&offset=0", headers=self.auth)
            self.assertEqual(r_out.status_code, 200)
            self.assertTrue(r_out.json()["truncated"])
            self.assertEqual(r_out.json()["items"][0]["name"], "foo_helper")

        with patch.object(v1_router.imports_service, "get_imports", return_value={
            "version": "v3.0",
            "path": "kernel/foo.c",
            "indexed": True,
            "layout": "tree",
            "reachable": True,
            "arch_used": "x86_64",
            "context": {"entry_file": "kernel/foo.c", "config_blocks": []},
            "reason": None,
            "items": [
                {
                    "header": "linux/foo.h",
                    "resolved_path": "include/linux/foo.h",
                    "line": 2,
                    "active": True,
                    "symbols": [{"name": "foo", "kind": "struct", "def_line": 1, "use_lines": [5], "total_uses": 1}],
                }
            ],
        }):
            r_imp = self.client.get("/api/v1/file/imports?path=kernel/foo.c", headers=self.auth)
            self.assertEqual(r_imp.status_code, 200)
            imp_body = r_imp.json()
            self.assertEqual(imp_body["repo"], "linux")
            self.assertEqual(imp_body["items"][0]["header"], "linux/foo.h")
            self.assertTrue(imp_body["indexed"])
            self.assertEqual(imp_body["layout"], "tree")
            self.assertTrue(imp_body["reachable"])
            self.assertEqual(imp_body["arch_used"], "x86_64")
            self.assertEqual(imp_body["total"], 1)
            self.assertFalse(imp_body["truncated"])

        with patch.object(v1_router._fs_service, "get_file_references", return_value={
            "version": "v3.0", "path": "include/linux/foo.h", "included_by": [{"file": "kernel/foo.c", "line": 2}],
            "sourced_by": [], "documented_in": [], "built_by": [],
        }):
            r_refs = self.client.get("/api/v1/file/references?path=include/linux/foo.h", headers=self.auth)
            self.assertEqual(r_refs.status_code, 200)
            self.assertEqual(r_refs.json()["included_by"][0]["file"], "kernel/foo.c")

    def test_symbols_struct_callgraph_callpath_and_grep(self) -> None:
        with patch.object(v1_router._symbol_service, "search_symbols", return_value=[
            {"name": "foo_helper", "type_name": "function"},
            {"name": "foo_bar", "type_name": "function"},
        ]):
            r_sym = self.client.get("/api/v1/symbols/search?q=foo&limit=1", headers=self.auth)
            self.assertEqual(r_sym.status_code, 200)
            self.assertTrue(r_sym.json()["truncated"])
            self.assertEqual(r_sym.json()["total"], 2)
            self.assertEqual(r_sym.json()["symbols"][0]["name"], "foo_helper")

        with patch.object(v1_router._symbol_service, "get_symbol_detail", return_value={
            "version": "v3.0", "name": "foo_helper", "file_path": "kernel/foo.c", "line_s": 3,
        }):
            r_def = self.client.get("/api/v1/symbols/definition?name=foo_helper", headers=self.auth)
            self.assertEqual(r_def.status_code, 200)
            self.assertEqual(r_def.json()["file_path"], "kernel/foo.c")
            # Graceful fallback when scope_service.locate raises an unexpected exception
            with patch("webapp.backend.services.scope_service.scope_service.locate", side_effect=RuntimeError("db offline")):
                r_def_fallback = self.client.get(
                    "/api/v1/symbols/definition?name=foo_helper&from_file=kernel/foo.c&line=7",
                    headers=self.auth,
                )
                self.assertEqual(r_def_fallback.status_code, 200)
                self.assertEqual(r_def_fallback.json()["file_path"], "kernel/foo.c")

        self.assertEqual(self.client.get("/api/v1/symbols/definition?name=bad;ident", headers=self.auth).status_code, 400)

        with patch.object(v1_router._symbol_service, "get_symbol_xref", return_value={
            "version": "v3.0", "arch": "x86_64", "definition": {"file": "kernel/foo.c", "line_s": 3},
            "definitions": [{"file": "kernel/foo.c", "line_s": 3}],
            "references": [
                {"file": "kernel/foo.c", "line": 7, "type": "Call"},
                {"file": "kernel/foo.c", "line": 8, "type": "Call"},
            ],
        }):
            r_xref = self.client.get("/api/v1/symbols/references?name=foo_helper&limit=1", headers=self.auth)
            self.assertEqual(r_xref.status_code, 200)
            self.assertTrue(r_xref.json()["truncated"])
            self.assertEqual(r_xref.json()["total"], 2)

        with patch.object(v1_router.hover_service, "hover", return_value={"version": "v3.0", "name": "foo_helper", "found": True}):
            r_hov = self.client.get("/api/v1/symbols/hover?name=foo_helper", headers=self.auth)
            self.assertEqual(r_hov.status_code, 200)
            self.assertTrue(r_hov.json()["found"])
            with patch("webapp.backend.services.scope_service.scope_service.locate", side_effect=RuntimeError("boom")):
                r_hov_fb = self.client.get(
                    "/api/v1/symbols/hover?name=foo_helper&from_file=kernel/foo.c&line=7",
                    headers=self.auth,
                )
                self.assertEqual(r_hov_fb.status_code, 200)
                self.assertTrue(r_hov_fb.json()["found"])

        with patch.object(v1_router._pahole_service, "get_struct_layout", return_value={"struct": "foo", "size": 4}):
            r_st = self.client.get("/api/v1/struct?name=foo", headers=self.auth)
            self.assertEqual(r_st.status_code, 200)
            self.assertEqual(r_st.json()["size"], 4)

        with patch.object(v1_router._callgraph_service, "get_function_callgraph", return_value={
            "version": "v3.0", "file_path": "kernel/foo.c", "line_s": 3, "line_e": 5,
            "callers": [{"name": "main_entry"}, {"name": "other_entry"}], "callees": [], "truncated": False,
        }):
            r_cg = self.client.get("/api/v1/callgraph?function=foo_helper&limit=1", headers=self.auth)
            self.assertEqual(r_cg.status_code, 200)
            self.assertTrue(r_cg.json()["truncated"])
            self.assertEqual(r_cg.json()["callers_total"], 2)

        with patch("api.routers.v1.callpath_state", return_value={"state": "done", "result": {"paths": []}}):
            r_cp = self.client.get("/api/v1/callpath?from=main_entry&to=foo_helper", headers=self.auth)
            self.assertEqual(r_cp.status_code, 200)
            self.assertEqual(r_cp.json()["repo"], "linux")

        # Real git grep against the temporary repository
        r_grep = self.client.get("/api/v1/grep?q=foo_helper&path=kernel/", headers=self.auth)
        self.assertEqual(r_grep.status_code, 200)
        self.assertEqual(r_grep.json()["matches"][0]["path"], "kernel/foo.c")
        self.assertEqual(self.client.get("/api/v1/grep?q=foo&path=../secret", headers=self.auth).status_code, 400)

    def test_commits_blame_history_and_diff(self) -> None:
        r_commits = self.client.get("/api/v1/commits?limit=10", headers=self.auth)
        self.assertEqual(r_commits.status_code, 200)
        self.assertEqual(r_commits.json()["commits"][0]["commit_hash"], self.linux_sha)

        r_detail = self.client.get(f"/api/v1/commits/{self.linux_sha}", headers=self.auth)
        self.assertEqual(r_detail.status_code, 200)
        self.assertEqual(r_detail.json()["subject"], "Initial commit for v3.0")

        r_cdiff = self.client.get(f"/api/v1/commits/{self.linux_sha}/diff?limit=2", headers=self.auth)
        self.assertEqual(r_cdiff.status_code, 200)
        self.assertTrue(r_cdiff.json()["truncated"])
        self.assertEqual(len(r_cdiff.json()["files"]), 2)

        r_blame = self.client.get("/api/v1/blame?path=kernel/foo.c&start_line=1&end_line=4&limit=2", headers=self.auth)
        self.assertEqual(r_blame.status_code, 200)
        self.assertTrue(r_blame.json()["truncated"])
        self.assertEqual(len(r_blame.json()["lines"]), 2)

        r_hist = self.client.get("/api/v1/history/file?path=kernel/foo.c", headers=self.auth)
        self.assertEqual(r_hist.status_code, 200)
        self.assertEqual(r_hist.json()["commits"][0]["commit_hash"], self.linux_sha)

        with patch.object(v1_router._diff_service, "get_file_diff", return_value={"path": "kernel/foo.c", "hunks": []}):
            r_diff = self.client.get("/api/v1/diff?version_a=v3.0&version_b=master&path=kernel/foo.c", headers=self.auth)
            self.assertEqual(r_diff.status_code, 200)
            self.assertEqual(r_diff.json()["path"], "kernel/foo.c")

    def test_kconfig_maintainers_docs_man_and_cve(self) -> None:
        with patch.object(v1_router._kconfig_service, "search_symbols", return_value={
            "version": "v3.0", "arch": "x86_64", "total": 1, "results": [{"name": "FOO_BAR", "prompt": "Foo Bar"}],
        }):
            r_ks = self.client.get("/api/v1/kconfig/search?q=FOO", headers=self.auth)
            self.assertEqual(r_ks.status_code, 200)
            self.assertEqual(r_ks.json()["symbols"][0]["name"], "FOO_BAR")

        with patch.object(v1_router._kconfig_service, "get_symbol_detail", return_value={
            "version": "v3.0", "name": "FOO_BAR", "type": "bool",
        }):
            r_kd = self.client.get("/api/v1/kconfig/symbol/FOO_BAR", headers=self.auth)
            self.assertEqual(r_kd.status_code, 200)
            self.assertEqual(r_kd.json()["type"], "bool")

        with patch.object(v1_router._kconfig_service, "build_conditions", return_value={
            "version": "v3.0", "arch": "x86_64", "files": {"kernel/foo.c": "CONFIG_FOO_BAR"},
        }):
            r_kb = self.client.get("/api/v1/kconfig/build?path=kernel/foo.c", headers=self.auth)
            self.assertEqual(r_kb.status_code, 200)

        with patch.object(v1_router._kconfig_service, "evaluate", return_value={
            "version": "v3.0", "arch": "x86_64", "defconfig": "arch/x86/configs/x86_64_defconfig",
            "applied": {"FOO_BAR": "y"}, "rejected": {}, "load_report": {},
            "symbols": {"FOO_BAR": {"value": "y"}}, "files": {},
        }) as m_eval:
            r_ke = self.client.get(
                "/api/v1/kconfig/eval?symbol=CONFIG_FOO_BAR&assign=FOO_BAR=y&path=kernel/foo.c", headers=self.auth
            )
            self.assertEqual(r_ke.status_code, 200)
            self.assertEqual(r_ke.json()["symbols"]["FOO_BAR"]["value"], "y")
            _args, kwargs = m_eval.call_args
            self.assertEqual(kwargs["symbols"], ["FOO_BAR"])
            self.assertEqual(kwargs["assignments"], {"FOO_BAR": "y"})
            self.assertEqual(kwargs["paths"], ["kernel/foo.c"])
        self.assertEqual(self.client.get("/api/v1/kconfig/eval", headers=self.auth).status_code, 400)
        self.assertEqual(
            self.client.get("/api/v1/kconfig/eval?symbol=FOO&assign=novalue", headers=self.auth).status_code, 400
        )
        self.assertEqual(
            self.client.get("/api/v1/kconfig/eval?symbol=FOO;DROP", headers=self.auth).status_code, 400
        )

        with patch.object(v1_router._maintainer_service, "match_maintainers", return_value={
            "version": "v3.0", "sections": [{"name": "FOO SUBSYSTEM"}], "recipients": [],
        }):
            r_mt = self.client.get("/api/v1/maintainers?path=kernel/foo.c", headers=self.auth)
            self.assertEqual(r_mt.status_code, 200)
            self.assertEqual(r_mt.json()["mode"], "path")

        with patch.object(v1_router._docs_service, "get_label_index", return_value={
            "version": "v3.0", "source": "git", "labels": {"foo_doc": {"path": "Documentation/foo.rst", "line": 1, "title": "Foo Title"}},
        }):
            r_doc = self.client.get("/api/v1/docs?q=foo", headers=self.auth)
            self.assertEqual(r_doc.status_code, 200)
            self.assertEqual(r_doc.json()["labels"][0]["label"], "foo_doc")

        r_man = self.client.get("/api/v1/man?repo=man-pages&version=man-pages-6.10&name=read", headers=self.auth)
        self.assertEqual(r_man.status_code, 200)
        self.assertEqual(r_man.json()["repo"], "man-pages")
        self.assertEqual(r_man.json()["name"], "read")

        with patch("api.routers.v1.cve_service.catalog", return_value={
            "configured": True, "ingested": True, "total": 2,
            "cves": [{"id": "CVE-2024-0001", "title": "Sample CVE"}],
        }):
            r_cve = self.client.get("/api/v1/cve?limit=1", headers=self.auth)
            self.assertEqual(r_cve.status_code, 200)
            self.assertTrue(r_cve.json()["truncated"])

        with patch("api.routers.v1.cve_service.record", return_value={"id": "CVE-2024-0001", "title": "Sample"}):
            r_cve_one = self.client.get("/api/v1/cve/CVE-2024-0001", headers=self.auth)
            self.assertEqual(r_cve_one.status_code, 200)
            self.assertEqual(r_cve_one.json()["id"], "CVE-2024-0001")

    def test_feature_flags_enforced(self) -> None:
        # On man-pages repo, kernel-only features are disabled by default -> 403
        hdrs = {**self.auth, "X-KI-Repo": "man-pages"}
        for path in (
            "/api/v1/kconfig/search?q=NET",
            "/api/v1/kconfig/symbol/NET",
            "/api/v1/kconfig/build?path=README",
            "/api/v1/kconfig/eval?symbol=NET",
            "/api/v1/maintainers?q=net",
            "/api/v1/docs?q=net",
            "/api/v1/cve",
            "/api/v1/cve/CVE-2024-0001",
            "/api/v1/struct?name=foo",
            "/api/v1/callgraph?function=foo",
            "/api/v1/callpath?from=a&to=b",
        ):
            r = self.client.get(path, headers=hdrs)
            self.assertEqual(r.status_code, 403, f"Expected 403 for {path}, got {r.status_code}")

        # On linux repo when man feature is explicitly disabled -> 403
        with patch.object(
            core_config,
            "get_repository",
            return_value={"name": "linux", "project": "linux", "path": str(self.linux_dir), "features": {"man": False}},
        ):
            # Disable on active repo config entry
            cfg = core_config.get_config()
            cfg["repositories"][0]["features"] = {"man": False}
            self.assertEqual(self.client.get("/api/v1/man?name=read", headers=self.auth).status_code, 403)

    def test_commit_search_backports_and_relations_mail_parity(self) -> None:
        from api.routers import common as api_common

        main, bp1, bp2, other = "a" * 40, "b" * 40, "c" * 40, "d" * 40
        every_row = [
            {"commit_hash": bp2, "subject": "ext2: mark as deprecated"},
            {"commit_hash": bp1, "subject": "ext2: mark as deprecated"},
            {"commit_hash": other, "subject": "ext2: other"},
            {"commit_hash": main, "subject": "ext2: mark as deprecated"},
        ]
        folded = [{**every_row[3], "backport_count": 2, "backport_hashes": [bp2, bp1]}, every_row[2]]

        def listing(ver, page=1, limit=50, query="", *, offset=None, collapse_backports=False):
            # the SQL fold (list_network_commits) is the service's job; the router passes the flag and the offset
            rows = folded if collapse_backports else every_row
            start = 0 if offset is None else offset
            return {"version": "v3.0", "commits": rows[start:start + limit]}

        with patch.object(api_common._git_service, "get_commits", side_effect=listing):
            body = self.client.get("/api/v1/commits?q=ext2", headers=self.auth).json()
            self.assertEqual([c["commit_hash"] for c in body["commits"]], [main, other])
            self.assertEqual(body["commits"][0]["backport_count"], 2)
            self.assertEqual(body["total"], 2)
            every = self.client.get("/api/v1/commits?q=ext2&backports=include", headers=self.auth).json()
            self.assertEqual(len(every["commits"]), 4)
            self.assertNotIn("backport_count", every["commits"][0])
        self.assertEqual(self.client.get("/api/v1/commits?q=x&backports=bogus", headers=self.auth).status_code, 422)

        stored = [{"message_id": "blob@x", "thread_id": 5, "relation_type": 1, "match_tier": 3,
                   "match_tier_name": "Blob Hash Match", "lore_url": "https://lore/blob@x/"}]
        with patch("api.routers.v1._relations_one", return_value={"commit": main, "links": ["<link@x>", "blob@x"]}), \
                patch.object(api_common._git_service, "get_commit_linked_emails", return_value=list(stored)) as linked:
            rel = self.client.get(f"/api/v1/commits/{main}/relations", headers=self.auth).json()
        linked.assert_called_once_with(main)
        self.assertEqual([e["message_id"] for e in rel["linked_emails"]], ["blob@x", "link@x"])
        self.assertEqual(rel["linked_emails"][0]["match_tier"], 3)
        self.assertEqual(rel["linked_emails"][1]["match_tier"], 1)

    def test_pagination_offsets_commits_and_man_path_validation(self) -> None:
        # 1. Non-multiple offset in /api/v1/commits and exact-multiple truncated=False
        r_exact_mult = self.client.get("/api/v1/commits?limit=1&offset=0", headers=self.auth)
        self.assertEqual(r_exact_mult.status_code, 200)
        self.assertEqual(len(r_exact_mult.json()["commits"]), 1)
        self.assertFalse(r_exact_mult.json()["truncated"])
        self.assertEqual(r_exact_mult.json()["total"], 1)

        with patch("api.routers.v1.run_commit_query", return_value={
            "commits": [
                {"commit_hash": "a" * 40, "subject": "c1"},
                {"commit_hash": "b" * 40, "subject": "c2"},
                {"commit_hash": "c" * 40, "subject": "c3"},
            ],
            "total": None,
            "has_more": True,
        }):
            r_non_mult = self.client.get("/api/v1/commits?author=Test&limit=2&offset=1", headers=self.auth)
            self.assertEqual(r_non_mult.status_code, 200)
            b_nm = r_non_mult.json()
            self.assertEqual(len(b_nm["commits"]), 2)
            self.assertEqual(b_nm["commits"][0]["subject"], "c2")
            self.assertTrue(b_nm["truncated"])
            self.assertEqual(b_nm["offset"], 1)
            self.assertEqual(b_nm["limit"], 2)

        # 2. Search/history offset cap validation (offset >= 2000 rejected with 400)
        for url in (
            "/api/v1/files/search?q=foo&offset=2000",
            "/api/v1/kconfig/search?q=FOO&offset=2000",
            "/api/v1/history/file?path=kernel/foo.c&offset=2000",
        ):
            self.assertEqual(self.client.get(url, headers=self.auth).status_code, 400, url)

        # 3. lookup_man(path=...) validates against man_service._repo_dir()
        r_man_path = self.client.get(
            "/api/v1/man?repo=man-pages&version=man-pages-6.10&path=man2/read.2",
            headers=self.auth,
        )
        self.assertEqual(r_man_path.status_code, 200)
        self.assertEqual(r_man_path.json()["mode"], "page")
        self.assertEqual(
            self.client.get("/api/v1/man?repo=man-pages&path=../etc/passwd", headers=self.auth).status_code,
            400,
        )

    def test_p0_parity_endpoints(self) -> None:
        # 1. GET /api/v1/file/resolve (header and object modes)
        self.assertEqual(self.client.get("/api/v1/file/resolve", headers=self.auth).status_code, 400)
        self.assertEqual(
            self.client.get("/api/v1/file/resolve?header=linux/foo.h&object=kernel/foo.o", headers=self.auth).status_code,
            400,
        )
        r_hdr = self.client.get(
            "/api/v1/file/resolve?header=linux/foo.h&from_file=kernel/foo.c",
            headers=self.auth,
        )
        self.assertEqual(r_hdr.status_code, 200)
        self.assertEqual(r_hdr.json()["repo"], "linux")
        self.assertEqual(r_hdr.json()["mode"], "include")
        self.assertEqual(r_hdr.json()["path"], "include/linux/foo.h")

        with patch.object(v1_router._fs_service, "resolve_object", return_value={
            "found": True, "path": "kernel/foo.c", "kind": "file", "candidates": ["kernel/foo.c"],
        }):
            r_obj = self.client.get("/api/v1/file/resolve?object=kernel/foo.o", headers=self.auth)
            self.assertEqual(r_obj.status_code, 200)
            self.assertEqual(r_obj.json()["mode"], "object")
            self.assertEqual(r_obj.json()["path"], "kernel/foo.c")

        # 2. GET /api/v1/implementations (struct and function modes)
        self.assertEqual(self.client.get("/api/v1/implementations", headers=self.auth).status_code, 400)
        self.assertEqual(
            self.client.get("/api/v1/implementations?struct=file_operations&function=foo_read", headers=self.auth).status_code,
            400,
        )
        with patch("api.routers.v1.ops_index_state", return_value={
            "state": "done",
            "result": {
                "version": "v3.0",
                "struct": "file_operations",
                "field": "read",
                "source": "db",
                "fields": ["read"],
                "instances": [{"var": "foo_fops", "file": "kernel/foo.c", "line": 4}],
                "by_field": {"read": [{"func": "foo_helper", "var": "foo_fops", "file": "kernel/foo.c", "line": 4}]},
            },
        }):
            r_impl = self.client.get("/api/v1/implementations?struct=file_operations&field=read", headers=self.auth)
            self.assertEqual(r_impl.status_code, 200)
            self.assertEqual(r_impl.json()["repo"], "linux")
            self.assertEqual(r_impl.json()["mode"], "struct")
            self.assertEqual(r_impl.json()["struct"], "file_operations")

        with patch("api.routers.v1.ops_uses_state", return_value={
            "state": "done",
            "result": {
                "version": "v3.0",
                "function": "foo_helper",
                "source": "db",
                "uses": [{"struct": "file_operations", "field": "read", "var": "foo_fops", "file": "kernel/foo.c", "line": 4}],
            },
        }):
            r_fn_impl = self.client.get("/api/v1/implementations?function=foo_helper", headers=self.auth)
            self.assertEqual(r_fn_impl.status_code, 200)
            self.assertEqual(r_fn_impl.json()["mode"], "function")
            self.assertEqual(len(r_fn_impl.json()["uses"]), 1)

        # 3. GET /api/v1/commits/{commit_hash}/relations and /released
        with patch("api.routers.v1._relations_one", return_value={
            "commit_hash": self.linux_sha, "fixes": [], "fixed_by": [], "mentions": [], "mentioned_by": [], "Duplicate_Commits": [],
        }):
            r_rel = self.client.get(f"/api/v1/commits/{self.linux_sha}/relations", headers=self.auth)
            self.assertEqual(r_rel.status_code, 200)
            self.assertEqual(r_rel.json()["repo"], "linux")
            self.assertEqual(r_rel.json()["commit_hash"], self.linux_sha)

        with patch("api.routers.v1._released_one", return_value={
            "commit_hash": self.linux_sha, "release": "v3.0", "first_rc": None, "contains": ["v3.0"],
        }):
            r_rel_in = self.client.get(f"/api/v1/commits/{self.linux_sha}/released", headers=self.auth)
            self.assertEqual(r_rel_in.status_code, 200)
            self.assertEqual(r_rel_in.json()["release"], "v3.0")

        # 4. GET /api/v1/symbols/scope and extended /api/v1/symbols/references
        with patch("webapp.backend.services.scope_service.scope_service.resolve", return_value={
            "indexed": True, "function": {"name": "foo_helper", "line_s": 3, "line_e": 5}, "locals": [{"name": "x", "kind": "param"}],
        }):
            r_sc = self.client.get("/api/v1/symbols/scope?path=kernel/foo.c&line=4", headers=self.auth)
            self.assertEqual(r_sc.status_code, 200)
            self.assertEqual(r_sc.json()["repo"], "linux")
            self.assertTrue(r_sc.json()["indexed"])

        with patch("webapp.backend.services.scope_service.scope_service.locate", return_value=(
            {
                "scope": "local",
                "definition": {"file": "kernel/foo.c", "line_s": 3, "line_e": 5},
                "definitions": [{"file": "kernel/foo.c", "line_s": 3, "line_e": 5}],
                "references": [{"file": "kernel/foo.c", "line": 4, "type": "Use"}],
            },
            True,
        )):
            r_local_xref = self.client.get(
                "/api/v1/symbols/references?name=x&from_file=kernel/foo.c&line=4&col=12",
                headers=self.auth,
            )
            self.assertEqual(r_local_xref.status_code, 200)
            self.assertEqual(r_local_xref.json()["scope"], "local")
            self.assertEqual(r_local_xref.json()["total"], 1)

        with patch.object(v1_router._symbol_service, "get_include_symbols", return_value={
            "ast_id": 99,
            "include_text": "<linux/foo.h>",
            "header_file": "include/linux/foo.h",
            "symbols": [{"name": "foo", "kind": "struct"}],
        }):
            r_inc_xref = self.client.get("/api/v1/symbols/references?name=CPPro_include&ast_id=99", headers=self.auth)
            self.assertEqual(r_inc_xref.status_code, 200)
            self.assertEqual(r_inc_xref.json()["header_file"], "include/linux/foo.h")
            self.assertEqual(r_inc_xref.json()["total"], 1)

        # 5. GET /api/v1/nodemap/struct and /api/v1/nodemap/source
        r_nm_st = self.client.get("/api/v1/nodemap/struct?name=foo&path=include/linux/foo.h", headers=self.auth)
        self.assertEqual(r_nm_st.status_code, 200)
        self.assertEqual(r_nm_st.json()["repo"], "linux")
        self.assertEqual(r_nm_st.json()["name"], "foo")
        self.assertEqual(r_nm_st.json()["items"][0]["name"], "a")

        r_nm_src = self.client.get("/api/v1/nodemap/source?path=kernel/foo.c&start=3&end=5", headers=self.auth)
        self.assertEqual(r_nm_src.status_code, 200)
        self.assertEqual(r_nm_src.json()["repo"], "linux")
        self.assertEqual((r_nm_src.json()["start_line"], r_nm_src.json()["end_line"]), (3, 5))

    def test_nodemap_stateless_preview_with_ops_and_export(self) -> None:
        # 1. Stateless preview works in default read-only mode
        r_prev = self.client.post(
            "/api/v1/nodemap/preview",
            headers=self.auth,
            json={
                "name": "Preview Map",
                "version": "v3.0",
                "ops": [
                    {
                        "op": "add_nodes",
                        "hydrate": True,
                        "nodes": [
                            {"id": "n_struct", "type": "struct", "data": {"name": "foo", "hint": "include/linux/foo.h"}},
                            {"id": "n_src", "type": "source", "data": {"path": "kernel/foo.c", "symbol": "foo_helper"}},
                        ],
                    },
                    {
                        "op": "connect",
                        "from": "n_struct",
                        "fromField": "a",
                        "to": "n_src",
                        "label": "uses field a",
                        "lock_kind": "rcu",
                    },
                ],
                "layout": {"mode": "dag"},
                "export_format": "mermaid",
            },
        )
        self.assertEqual(r_prev.status_code, 200, r_prev.text)
        prev_body = r_prev.json()
        self.assertEqual(prev_body["repo"], "linux")
        self.assertEqual(prev_body["node_count"], 2)
        self.assertEqual(prev_body["edge_count"], 1)
        self.assertIn("flowchart LR", prev_body["export"])
        edge0 = prev_body["canvas"]["edges"][0]
        self.assertEqual(edge0["fromField"], "2:a")
        self.assertEqual(edge0["lockKind"], "rcu")

    # ------------------------------------------------------------------ stateless NodeMap (nothing is saved)

    def _canvas_client(self) -> tuple[TestClient, Path]:
        """Client with writes fully enabled and a temp canvas dir; used to prove no route can persist canvases."""
        cfg = core_config.get_config()
        canvas_dir = Path(self._tmp.name) / "canvases"
        (canvas_dir / "linux").mkdir(parents=True, exist_ok=True)
        cfg["api"]["allow_writes"] = True
        cfg["api"]["nodemap_canvas_dir"] = str(canvas_dir)
        cfg["api"]["keys"] = {"writer": {"key": "write-secret", "scopes": ["read", "nodemap:write"]}}
        self.assertEqual(core_config.get_nodemap_canvas_dir(), canvas_dir)
        return TestClient(create_app()), canvas_dir

    @staticmethod
    def _note(nid: str, text: str = "x") -> dict:
        return {"id": nid, "type": "note", "x": 0, "y": 0, "w": 200, "h": 100, "data": {"text": text}}

    @classmethod
    def _chain(cls) -> dict:
        """A -> B -> C inline: root holds `na` (nodemap) holding `nb` (nodemap) holding a leaf note."""
        c = {"schema": "kip-nodemap-canvas", "version": 1, "nodes": [cls._note("leaf", "deep")], "edges": []}
        b = {"schema": "kip-nodemap-canvas", "version": 1,
             "nodes": [{"id": "nc", "type": "nodemap", "x": 0, "y": 0, "w": 300, "h": 200, "data": {"canvas": c}}], "edges": []}
        return {"schema": "kip-nodemap-canvas", "version": 1,
                "nodes": [cls._note("top"), {"id": "nb", "type": "nodemap", "x": 400, "y": 0, "w": 300, "h": 200, "data": {"canvas": b}}],
                "edges": [{"id": "e1", "from": "top", "to": "nb"}]}

    @staticmethod
    def _tree_files(root: Path) -> list[str]:
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def test_nodemap_canvas_write_routes_removed_and_nothing_persisted(self) -> None:
        client, canvas_dir = self._canvas_client()
        hdr = {"Authorization": "Bearer write-secret"}
        before = self._tree_files(canvas_dir)
        base = "/api/v1/nodemap/canvases"
        attempts = [
            ("post", base, {"id": "m", "name": "M"}),
            ("put", f"{base}/m", {"name": "M"}),
            ("delete", f"{base}/m", None),
            ("post", f"{base}/m/ops", {"ops": []}),
            ("post", f"{base}/m/expand", {"mode": "kconfig", "symbol": "X"}),
            ("post", f"{base}/m/layout", {"mode": "dag"}),
            ("post", f"{base}/m/nested", {}),
            ("post", f"{base}/m/group", {"node_ids": ["a"]}),
        ]
        for method, url, body in attempts:
            kwargs = {"json": body} if body is not None else {}
            r = getattr(client, method)(url, headers=hdr, **kwargs)
            self.assertIn(r.status_code, (404, 405), f"{method.upper()} {url} -> {r.status_code}")
        self.assertEqual(self._tree_files(canvas_dir), before)
        self.assertEqual(before, [])
        # catalog has no write-scoped entry and capabilities report read-only
        caps = client.get("/api/v1/capabilities", headers=hdr).json()
        self.assertTrue(caps["read_only"])
        self.assertTrue(caps["mcp"]["read_only"])
        self.assertFalse([e for e in caps["endpoints"] if e.get("scope")])
        nodemap_eps = {(e["method"], e["path"]) for e in caps["endpoints"] if "/nodemap/" in e["path"]}
        self.assertEqual(
            {m for m, _ in nodemap_eps if m != "GET"} , {"POST"},
        )
        self.assertEqual(
            sorted(p for m, p in nodemap_eps if m == "POST"),
            ["/api/v1/nodemap/group", "/api/v1/nodemap/hierarchy", "/api/v1/nodemap/nest", "/api/v1/nodemap/preview"],
        )

    def test_nodemap_readonly_access_to_preexisting_server_canvases(self) -> None:
        client, canvas_dir = self._canvas_client()
        hdr = {"Authorization": "Bearer write-secret"}
        def put(cid: str, nodes: list) -> None:
            doc = {"id": cid, "name": f"Canvas {cid}", "canvas": {"schema": "kip-nodemap-canvas", "version": 1, "nodes": nodes, "edges": []}}
            (canvas_dir / "linux" / f"{cid}.json").write_text(json.dumps(doc), encoding="utf-8")
        put("q", [self._note("qn")])
        put("p", [self._note("pn"), {"id": "ref_q", "type": "nodemap", "x": 0, "y": 0, "w": 300, "h": 200, "data": {"canvasId": "q"}},
                  {"id": "ref_gone", "type": "nodemap", "x": 0, "y": 300, "w": 300, "h": 200, "data": {"canvasId": "nope"}}])
        before = {n: (canvas_dir / n).read_text() for n in self._tree_files(canvas_dir)}

        r_list = client.get("/api/v1/nodemap/canvases", headers=hdr)
        self.assertEqual(r_list.status_code, 200)
        self.assertEqual(sorted(i["id"] for i in r_list.json()["items"]), ["p", "q"])
        self.assertNotIn("insertable", json.dumps(r_list.json()))

        r_get = client.get("/api/v1/nodemap/canvases/p", headers=hdr)
        self.assertEqual(r_get.status_code, 200, r_get.text)
        body = r_get.json()
        self.assertNotIn("nested_maps", body)
        by_id = {n["id"]: n for n in body["canvas"]["nodes"]}
        inner = by_id["ref_q"]["data"]["canvas"]
        self.assertEqual([n["id"] for n in inner["nodes"]], ["qn"])
        self.assertNotIn("canvasId", by_id["ref_q"]["data"])
        self.assertEqual(by_id["ref_q"]["data"]["summary"]["nodes"], 1)
        self.assertNotIn("preview", by_id["ref_q"]["data"])
        self.assertNotIn("canvas", by_id["ref_gone"]["data"])
        self.assertTrue(any(w["code"] == "missing_reference" and w["canvas_id"] == "nope" for w in body["warnings"]))
        r_prev = client.get("/api/v1/nodemap/canvases/p?include_preview=true", headers=hdr)
        self.assertIn("preview", {n["id"]: n for n in r_prev.json()["canvas"]["nodes"]}["ref_q"]["data"])

        r_exp = client.get("/api/v1/nodemap/canvases/p/export?format=json", headers=hdr)
        self.assertEqual(r_exp.status_code, 200)
        self.assertIn('"canvas"', r_exp.json()["content"])
        r_h = client.get("/api/v1/nodemap/canvases/p/hierarchy", headers=hdr)
        self.assertEqual(r_h.status_code, 200)
        self.assertNotIn("invalid_targets", r_h.json())
        self.assertEqual(client.get("/api/v1/nodemap/canvases/missing", headers=hdr).status_code, 404)

        r_sh = client.post("/api/v1/nodemap/hierarchy", headers=hdr, json={"canvas_id": "p"})
        self.assertEqual(r_sh.status_code, 200, r_sh.text)
        self.assertEqual(client.post("/api/v1/nodemap/hierarchy", headers=hdr, json={}).status_code, 422)
        self.assertEqual(
            client.post("/api/v1/nodemap/hierarchy", headers=hdr, json={"canvas_id": "p", "canvas": {}}).status_code, 422
        )
        after = {n: (canvas_dir / n).read_text() for n in self._tree_files(canvas_dir)}
        self.assertEqual(after, before)

    def test_nodemap_stateless_preview_nest_group_hierarchy(self) -> None:
        client, canvas_dir = self._canvas_client()
        hdr = {"Authorization": "Bearer write-secret"}

        # preview: recursive normalize A->B->C, summaries always, preview geometry only on request
        r = client.post("/api/v1/nodemap/preview", headers=hdr, json={"canvas": self._chain(), "hydrate": False})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["nested_depth"], 2)
        self.assertEqual(out["nested_count"], 2)
        nb = {n["id"]: n for n in out["canvas"]["nodes"]}["nb"]["data"]
        self.assertEqual(nb["summary"]["depth"], 1)
        self.assertNotIn("preview", nb)
        nc = nb["canvas"]["nodes"][0]["data"]
        self.assertEqual(nc["canvas"]["nodes"][0]["id"], "leaf")
        r2 = client.post("/api/v1/nodemap/preview", headers=hdr, json={"canvas": self._chain(), "hydrate": False, "include_preview": True})
        self.assertIn("preview", {n["id"]: n for n in r2.json()["canvas"]["nodes"]}["nb"]["data"])

        # nest: empty child, inline child, nested target path, server canvas copied read-only
        root = {"schema": "kip-nodemap-canvas", "version": 1, "nodes": [self._note("a")], "edges": []}
        r_n = client.post("/api/v1/nodemap/nest", headers=hdr, json={"canvas": root, "name": "Child B", "node": {"id": "nb"}})
        self.assertEqual(r_n.status_code, 200, r_n.text)
        n1 = r_n.json()
        self.assertEqual(n1["node_id"], "nb")
        self.assertEqual(n1["nested_count"], 1)
        r_n2 = client.post(
            "/api/v1/nodemap/nest", headers=hdr,
            json={"canvas": n1["canvas"], "path": ["nb"], "child": {"schema": "kip-nodemap-canvas", "version": 1, "nodes": [self._note("z")], "edges": []}, "node": {"id": "nc"}},
        )
        self.assertEqual(r_n2.status_code, 200, r_n2.text)
        n2 = r_n2.json()
        self.assertEqual(n2["nested_depth"], 2)
        self.assertEqual(n2["path"], ["nb"])
        top = {n["id"]: n for n in n2["canvas"]["nodes"]}
        self.assertEqual(top["nb"]["data"]["canvas"]["nodes"][0]["data"]["canvas"]["nodes"][0]["id"], "z")
        # both child and server_canvas_id -> 422; unknown path -> 422; depth over 8 -> 422
        self.assertEqual(client.post("/api/v1/nodemap/nest", headers=hdr, json={"canvas": root, "child": root, "server_canvas_id": "x"}).status_code, 422)
        self.assertEqual(client.post("/api/v1/nodemap/nest", headers=hdr, json={"canvas": root, "path": ["ghost"]}).status_code, 422)
        deep = {"schema": "kip-nodemap-canvas", "version": 1, "nodes": [self._note("leaf")], "edges": []}
        for i in range(9):
            deep = {"schema": "kip-nodemap-canvas", "version": 1, "nodes": [{"id": f"d{i}", "type": "nodemap", "x": 0, "y": 0, "w": 300, "h": 200, "data": {"canvas": deep}}], "edges": []}
        self.assertEqual(client.post("/api/v1/nodemap/preview", headers=hdr, json={"canvas": deep, "hydrate": False}).status_code, 422)
        self.assertEqual(client.post("/api/v1/nodemap/nest", headers=hdr, json={"canvas": deep}).status_code, 422)

        # group: moves nodes + internal edges, rewires boundary edges, retargets tour steps
        g_canvas = {
            "schema": "kip-nodemap-canvas", "version": 1,
            "nodes": [self._note("a"), self._note("b"), self._note("c")],
            "edges": [{"id": "ab", "from": "a", "to": "b"}, {"id": "bc", "from": "b", "to": "c"}],
            "tours": [{"id": "t1", "title": "T", "steps": [{"nodeIds": ["a"], "title": "s"}]}],
        }
        r_g = client.post("/api/v1/nodemap/group", headers=hdr, json={"canvas": g_canvas, "node_ids": ["a", "b"], "name": "AB", "node": {"id": "grp"}})
        self.assertEqual(r_g.status_code, 200, r_g.text)
        g = r_g.json()
        self.assertEqual(sorted(g["moved_node_ids"]), ["a", "b"])
        self.assertEqual(g["rewired_edge_ids"], ["bc"])
        gn = {n["id"]: n for n in g["canvas"]["nodes"]}
        self.assertEqual(sorted(gn), ["c", "grp"])
        self.assertEqual(sorted(n["id"] for n in gn["grp"]["data"]["canvas"]["nodes"]), ["a", "b"])
        self.assertEqual([e["id"] for e in gn["grp"]["data"]["canvas"]["edges"]], ["ab"])
        bc = next(e for e in g["canvas"]["edges"] if e["id"] == "bc")
        self.assertEqual((bc["from"], bc["to"]), ("grp", "c"))
        self.assertEqual(g["canvas"]["tours"][0]["steps"][0]["nodeIds"], ["grp"])
        self.assertEqual(client.post("/api/v1/nodemap/group", headers=hdr, json={"canvas": g_canvas, "node_ids": []}).status_code, 422)
        self.assertEqual(client.post("/api/v1/nodemap/group", headers=hdr, json={"canvas": g_canvas, "node_ids": ["nope"]}).status_code, 422)

        # parent edge targeting a node that exists only inside a nested child -> 422
        bad = {**g["canvas"], "edges": [*g["canvas"]["edges"], {"id": "bad", "from": "c", "to": "a"}]}
        self.assertEqual(client.post("/api/v1/nodemap/preview", headers=hdr, json={"canvas": bad, "hydrate": False}).status_code, 422)

        # inline hierarchy
        r_h = client.post("/api/v1/nodemap/hierarchy", headers=hdr, json={"canvas": self._chain(), "depth": 8})
        self.assertEqual(r_h.status_code, 200, r_h.text)
        h = r_h.json()
        self.assertEqual(h["nested_depth"], 2)
        self.assertEqual(h["tree"]["children"][0]["node_id"], "nb")
        self.assertEqual(client.post("/api/v1/nodemap/hierarchy", headers=hdr, json={"canvas": self._chain(), "depth": 99}).status_code, 422)

        # strictness: unknown node type inside a nested canvas is rejected via the pydantic/strict path
        # and nothing was written
        self.assertEqual(self._tree_files(canvas_dir), [])

    def test_p1_kapi_mail_kconfig_history_pahole_and_maintainers(self) -> None:
        # 1. KAPI endpoints (summary, syscalls, exports, params, tracepoints, modules, devices)
        with patch("webapp.backend.services.kapi_service.summary", return_value={
            "version": "v3.0", "arch": "x86_64", "source": "database", "preparsed": True,
            "counts": {"syscalls": 300, "exports": 10},
        }):
            r_ksum = self.client.get("/api/v1/kapi/summary?arch=x86_64", headers=self.auth)
            self.assertEqual(r_ksum.status_code, 200)
            self.assertEqual(r_ksum.json()["repo"], "linux")
            self.assertEqual(r_ksum.json()["counts"]["syscalls"], 300)

        with patch("webapp.backend.services.kapi_service.syscalls", return_value={
            "state": "done",
            "result": {
                "arch": "x86_64",
                "srcarch": "x86",
                "sources": [{"kind": "tbl", "path": "arch/x86/entry/syscalls/syscall_64.tbl"}],
                "syscalls": [
                    {"nr": 0, "abi": "common", "name": "read", "entry": "sys_read", "status": "defined"},
                    {"nr": 1, "abi": "common", "name": "write", "entry": "sys_write", "status": "defined"},
                ],
                "unnumbered": [{"name": "old_mmap"}],
            },
        }):
            r_sc = self.client.get("/api/v1/kapi/syscalls?q=read&include_unnumbered=true", headers=self.auth)
            self.assertEqual(r_sc.status_code, 200)
            self.assertEqual(r_sc.json()["repo"], "linux")
            self.assertEqual(r_sc.json()["total"], 1)
            self.assertEqual(r_sc.json()["syscalls"][0]["name"], "read")
            self.assertEqual(r_sc.json()["unnumbered_total"], 1)

        with patch("webapp.backend.services.kapi_service.exports", return_value={
            "state": "done",
            "result": {
                "counts": {"EXPORT_SYMBOL": 1, "EXPORT_SYMBOL_GPL": 1},
                "exports": [
                    {"symbol": "foo_helper", "variant": "EXPORT_SYMBOL", "namespace": "", "path": "kernel/foo.c", "line": 5},
                    {"symbol": "bar_gpl", "variant": "EXPORT_SYMBOL_GPL", "namespace": "NS1", "path": "kernel/bar.c", "line": 9},
                ],
            },
        }), patch("webapp.backend.services.kapi_service.export_detail", return_value={
            "version": "v3.0", "symbol": "foo_helper", "sites": [{"path": "kernel/foo.c", "line": 5, "owner": {"modname": "vmlinux"}}],
        }):
            r_ex_list = self.client.get("/api/v1/kapi/exports?variant=EXPORT_SYMBOL_GPL", headers=self.auth)
            self.assertEqual(r_ex_list.status_code, 200)
            self.assertEqual(r_ex_list.json()["repo"], "linux")
            self.assertEqual(r_ex_list.json()["mode"], "list")
            self.assertEqual(r_ex_list.json()["total"], 1)
            self.assertEqual(r_ex_list.json()["exports"][0]["symbol"], "bar_gpl")

            r_ex_one = self.client.get("/api/v1/kapi/exports?name=foo_helper", headers=self.auth)
            self.assertEqual(r_ex_one.status_code, 200)
            self.assertEqual(r_ex_one.json()["repo"], "linux")
            self.assertEqual(r_ex_one.json()["mode"], "detail")
            self.assertEqual(r_ex_one.json()["sites"][0]["path"], "kernel/foo.c")

        with patch("webapp.backend.services.kapi_service.params", return_value={
            "state": "done",
            "result": {
                "arch": "x86_64",
                "doc_path": "Documentation/kernel-parameters.txt",
                "params": [
                    {"kind": "module", "name": "debug", "full_name": "foo.debug", "path": "kernel/foo.c", "description": "Enable debug"},
                ],
            },
        }), patch("webapp.backend.services.kapi_service.param_doc", return_value={
            "path": "Documentation/kernel-parameters.txt", "line": 12, "text": "foo.debug=\tEnable debug",
        }):
            r_pm = self.client.get("/api/v1/kapi/params?q=debug", headers=self.auth)
            self.assertEqual(r_pm.status_code, 200)
            self.assertEqual(r_pm.json()["repo"], "linux")
            self.assertEqual(r_pm.json()["total"], 1)
            r_pmd = self.client.get("/api/v1/kapi/params?doc_line=12", headers=self.auth)
            self.assertEqual(r_pmd.status_code, 200)
            self.assertEqual(r_pmd.json()["mode"], "doc")

        with patch("webapp.backend.services.kapi_service.tracepoints", return_value={
            "state": "done",
            "result": {
                "tracepoints": [
                    {"name": "sched_switch", "system": "sched", "kind": "event", "path": "include/trace/events/sched.h", "line": 20},
                ],
            },
        }), patch("webapp.backend.services.kapi_service.tracepoint_callsites", return_value={
            "name": "sched_switch",
            "call_sites": [{"path": "kernel/sched/core.c", "line": 100, "text": "trace_sched_switch(...)"}],
            "truncated": False,
        }):
            r_tp_list = self.client.get("/api/v1/kapi/tracepoints?system=sched", headers=self.auth)
            self.assertEqual(r_tp_list.status_code, 200)
            self.assertEqual(r_tp_list.json()["repo"], "linux")
            self.assertEqual(r_tp_list.json()["total"], 1)

            r_tp_one = self.client.get("/api/v1/kapi/tracepoints?name=sched_switch", headers=self.auth)
            self.assertEqual(r_tp_one.status_code, 200)
            self.assertEqual(r_tp_one.json()["mode"], "detail")
            self.assertEqual(r_tp_one.json()["call_sites"][0]["path"], "kernel/sched/core.c")
            self.assertEqual(r_tp_one.json()["tracepoint"]["system"], "sched")

        with patch("webapp.backend.services.kapi_service.modules", return_value={
            "state": "done",
            "result": {
                "arch": "x86_64",
                "modules": [
                    {"modname": "e1000e", "target": "drivers/net/e1000e/e1000e.ko", "description": "Intel PRO/1000", "required_symbols": ["E1000E"]},
                ],
            },
        }), patch("webapp.backend.services.kapi_service.module_detail", return_value={
            "modname": "e1000e", "license": "GPL", "params": [], "exports": [], "maintainers": [],
        }):
            r_mod_list = self.client.get("/api/v1/kapi/modules?q=e1000e", headers=self.auth)
            self.assertEqual(r_mod_list.status_code, 200)
            self.assertEqual(r_mod_list.json()["repo"], "linux")
            self.assertEqual(r_mod_list.json()["total"], 1)

            r_mod_one = self.client.get("/api/v1/kapi/modules?name=e1000e", headers=self.auth)
            self.assertEqual(r_mod_one.status_code, 200)
            self.assertEqual(r_mod_one.json()["mode"], "detail")
            self.assertEqual(r_mod_one.json()["license"], "GPL")

        with patch("webapp.backend.services.device_index_service.index_state", return_value={
            "state": "done",
            "result": {
                "tables": 1,
                "entries": [{"bus": "pci", "vendor": 0x8086, "device": 0x10D3, "table": "e1000_pci_tbl", "path": "drivers/net/e1000e/netdev.c", "line": 50, "text": "PCI_VDEVICE(INTEL, 0x10D3)"}],
            },
        }), patch("webapp.backend.services.device_index_service.match_text", return_value={
            "lines": [{"input": "8086:10d3", "ids": ["pci 8086:10d3"], "matches": [{"module": "e1000e"}]}],
            "unmatched": [],
        }):
            r_dev_list = self.client.get("/api/v1/kapi/devices?bus=pci", headers=self.auth)
            self.assertEqual(r_dev_list.status_code, 200)
            self.assertEqual(r_dev_list.json()["repo"], "linux")
            self.assertEqual(r_dev_list.json()["mode"], "list")
            self.assertEqual(r_dev_list.json()["total"], 1)

            r_dev_match = self.client.get("/api/v1/kapi/devices?match=8086:10d3", headers=self.auth)
            self.assertEqual(r_dev_match.status_code, 200)
            self.assertEqual(r_dev_match.json()["mode"], "match")
            self.assertEqual(r_dev_match.json()["lines"][0]["matches"][0]["module"], "e1000e")

        # 2. Mail / LKML endpoints (search, message, thread, merged)
        with patch("webapp.backend.services.mailing_list_service.mailing_list_service.search", return_value={
            "total": 1, "results": [{"message_id": "msg1@example.com", "subject": "[PATCH] foo"}],
        }), patch("webapp.backend.services.mailing_list_service.mailing_list_service.get_message", return_value={
            "message_id": "msg1@example.com", "subject": "[PATCH] foo", "body": "Hello",
        }), patch("webapp.backend.services.mailing_list_service.mailing_list_service.get_thread", return_value={
            "thread_id": 42, "messages": [{"message_id": "msg1@example.com", "subject": "[PATCH] foo"}],
        }), patch("webapp.backend.services.mailing_list_service.mailing_list_service.get_thread_versions", return_value={
            "series": [{"version": 1, "thread_id": 42}],
        }), patch("api.routers.mail.merged_commits", return_value={
            "source": "db", "commits": [{"sha": self.linux_sha, "release": "v3.0"}],
        }):
            r_ms = self.client.get("/api/v1/mail/search?q=foo&filter=patch", headers=self.auth)
            self.assertEqual(r_ms.status_code, 200)
            self.assertEqual(r_ms.json()["repo"], "linux")
            self.assertEqual(r_ms.json()["results"][0]["message_id"], "msg1@example.com")

            r_mm = self.client.get("/api/v1/mail/message?mid=msg1@example.com", headers=self.auth)
            self.assertEqual(r_mm.status_code, 200)
            self.assertEqual(r_mm.json()["repo"], "linux")
            self.assertEqual(r_mm.json()["body"], "Hello")

            self.assertEqual(self.client.get("/api/v1/mail/thread", headers=self.auth).status_code, 400)
            r_mt = self.client.get("/api/v1/mail/thread?tid=42&include_versions=true", headers=self.auth)
            self.assertEqual(r_mt.status_code, 200)
            self.assertEqual(r_mt.json()["repo"], "linux")
            self.assertEqual(r_mt.json()["total"], 1)
            self.assertEqual(r_mt.json()["versions"]["series"][0]["version"], 1)

            self.assertEqual(self.client.get("/api/v1/mail/merged", headers=self.auth).status_code, 400)
            r_mrg = self.client.get("/api/v1/mail/merged?mid=msg1@example.com", headers=self.auth)
            self.assertEqual(r_mrg.status_code, 200)
            self.assertEqual(r_mrg.json()["repo"], "linux")
            self.assertEqual(r_mrg.json()["commits"][0]["sha"], self.linux_sha)

        # 3. Kconfig & Kbuild endpoints (graph, solve, defconfigs, history, kbuild)
        with patch.object(v1_router._kconfig_service, "get_graph", return_value={
            "symbol": "FOO_BAR", "nodes": [{"id": "FOO_BAR"}], "edges": [],
        }), patch.object(v1_router._kconfig_service, "autosolve", return_value={
            "target_symbol": "FOO_BAR", "value": "y", "changes": [{"symbol": "FOO_BAR", "value": "y"}],
        }), patch.object(v1_router._kconfig_service, "get_defconfigs", return_value={
            "arch": "x86_64", "defconfigs": ["arch/x86/configs/x86_64_defconfig"],
        }), patch.object(v1_router._kconfig_service, "list_fragments", return_value={
            "fragments": ["kernel/configs/kvm_guest.config"],
        }), patch.object(v1_router._kconfig_service, "get_defconfig_content", return_value={
            "file_path": "arch/x86/configs/x86_64_defconfig", "content": "CONFIG_FOO_BAR=y\n",
        }), patch("webapp.backend.services.history_service.history_service.get_kconfig_history", return_value={
            "symbol": "FOO_BAR", "entries": [{"version": "v3.0", "status": "added"}],
        }), patch("webapp.backend.services.kbuild_index_service.index_state", return_value={
            "state": "done", "result": {"arch": "x86_64", "objects": {}, "files": {}},
        }), patch("webapp.backend.services.kbuild_index_service.objects_for_file", return_value=[
            {"target": "kernel/foo.o", "modname": "vmlinux"},
        ]), patch("webapp.backend.services.kbuild_index_service.objects_for_module", return_value={
            "modules": {"e1000e": {"objects": ["drivers/net/e1000e/netdev.o"], "required_symbols": ["E1000E"]}},
            "unmatched": [],
        }), patch("webapp.backend.services.kbuild_index_service.module_gates", return_value=["E1000E"]):
            r_kg = self.client.get("/api/v1/kconfig/graph?symbol=CONFIG_FOO_BAR", headers=self.auth)
            self.assertEqual(r_kg.status_code, 200)
            self.assertEqual(r_kg.json()["repo"], "linux")
            self.assertEqual(r_kg.json()["symbol"], "FOO_BAR")

            r_ksol = self.client.get("/api/v1/kconfig/solve?symbol=FOO_BAR&value=y&assign=NET=y", headers=self.auth)
            self.assertEqual(r_ksol.status_code, 200)
            self.assertEqual(r_ksol.json()["repo"], "linux")
            self.assertEqual(r_ksol.json()["changes"][0]["symbol"], "FOO_BAR")

            r_kdef = self.client.get("/api/v1/kconfig/defconfigs", headers=self.auth)
            self.assertEqual(r_kdef.status_code, 200)
            self.assertEqual(r_kdef.json()["mode"], "list")
            self.assertIn("arch/x86/configs/x86_64_defconfig", r_kdef.json()["defconfigs"])
            self.assertIn("kernel/configs/kvm_guest.config", r_kdef.json()["fragments"])

            r_kdef_c = self.client.get("/api/v1/kconfig/defconfigs?file_path=Kconfig", headers=self.auth)
            self.assertEqual(r_kdef_c.status_code, 200)
            self.assertEqual(r_kdef_c.json()["mode"], "content")

            r_khist = self.client.get("/api/v1/kconfig/history?symbol=FOO_BAR", headers=self.auth)
            self.assertEqual(r_khist.status_code, 200)
            self.assertEqual(r_khist.json()["repo"], "linux")
            self.assertEqual(r_khist.json()["entries"][0]["version"], "v3.0")

            self.assertEqual(self.client.get("/api/v1/kconfig/kbuild", headers=self.auth).status_code, 400)
            r_kb_file = self.client.get("/api/v1/kconfig/kbuild?path=kernel/foo.c", headers=self.auth)
            self.assertEqual(r_kb_file.status_code, 200)
            self.assertEqual(r_kb_file.json()["mode"], "file")
            self.assertEqual(r_kb_file.json()["objects"][0]["target"], "kernel/foo.o")

            r_kb_mod = self.client.get("/api/v1/kconfig/kbuild?module=e1000e&gates=true", headers=self.auth)
            self.assertEqual(r_kb_mod.status_code, 200)
            self.assertEqual(r_kb_mod.json()["mode"], "modules")
            self.assertIn("e1000e", r_kb_mod.json()["modules"])
            self.assertEqual(r_kb_mod.json()["module_gates"], ["E1000E"])

        # 4. History & Pahole (symbol history, line-range commits, struct compare, struct history)
        with patch("webapp.backend.services.history_service.history_service.get_symbol_history", return_value={
            "symbol": "foo_helper", "path": "kernel/foo.c", "entries": [{"version": "v3.0", "status": "added"}],
        }), patch("webapp.backend.services.history_service.history_service.get_change_commits", return_value={
            "path": "kernel/foo.c", "commits": [{"sha": self.linux_sha, "subject": "Initial commit for v3.0"}],
        }), patch("webapp.backend.services.pahole_compare.compare", return_value={
            "struct": "foo", "a": {"size": 4}, "b": {"size": 8}, "members": [],
        }), patch("webapp.backend.services.pahole_compare.history_state", return_value={
            "state": "done", "result": {"struct": "foo", "entries": [{"version": "v3.0", "size": 4}]},
        }):
            r_sh = self.client.get("/api/v1/history/symbol?path=kernel/foo.c&name=foo_helper", headers=self.auth)
            self.assertEqual(r_sh.status_code, 200)
            self.assertEqual(r_sh.json()["repo"], "linux")
            self.assertEqual(r_sh.json()["symbol"], "foo_helper")

            r_lh = self.client.get("/api/v1/history/lines?path=kernel/foo.c&line_s=3&line_e=5", headers=self.auth)
            self.assertEqual(r_lh.status_code, 200)
            self.assertEqual(r_lh.json()["repo"], "linux")
            self.assertEqual(r_lh.json()["total"], 1)

            r_scmp = self.client.get("/api/v1/struct/compare?name=foo&version_a=v3.0&version_b=master", headers=self.auth)
            self.assertEqual(r_scmp.status_code, 200)
            self.assertEqual(r_scmp.json()["repo"], "linux")
            self.assertEqual(r_scmp.json()["b"]["size"], 8)

            r_shist = self.client.get("/api/v1/struct/history?name=foo", headers=self.auth)
            self.assertEqual(r_shist.status_code, 200)
            self.assertEqual(r_shist.json()["repo"], "linux")
            self.assertEqual(r_shist.json()["entries"][0]["size"], 4)

        # 5. Maintainers (multi-path lookup, match POST, person/developers/credits)
        with patch.object(v1_router._maintainer_service, "match_maintainers", return_value={
            "version": "v3.0", "sections": [{"name": "FOO SUBSYSTEM"}], "recipients": [{"email": "alice@example.com"}],
        }) as m_match, patch.object(v1_router._maintainer_service, "get_person_profile", return_value={
            "name": "Alice Dev", "email": "alice@example.com", "sections": ["FOO SUBSYSTEM"],
        }), patch.object(v1_router._maintainer_service, "get_developers", return_value={
            "version": "v3.0", "developers": [{"name": "Alice Dev", "email": "alice@example.com"}],
        }), patch.object(v1_router._maintainer_service, "get_credits", return_value={
            "version": "v3.0", "entries": [{"name": "Alice Dev", "email": "alice@example.com"}],
        }):
            r_mp = self.client.get("/api/v1/maintainers?path=kernel/foo.c&path=include/linux/foo.h", headers=self.auth)
            self.assertEqual(r_mp.status_code, 200)
            self.assertEqual(r_mp.json()["repo"], "linux")
            self.assertEqual(r_mp.json()["paths"], ["kernel/foo.c", "include/linux/foo.h"])
            self.assertEqual(m_match.call_args.kwargs["paths"], ["kernel/foo.c", "include/linux/foo.h"])

            r_mmatch = self.client.post(
                "/api/v1/maintainers/match",
                headers=self.auth,
                json={"patch_text": "--- a/kernel/foo.c\n+++ b/kernel/foo.c\n", "paths": ["kernel/foo.c"]},
            )
            self.assertEqual(r_mmatch.status_code, 200)
            self.assertEqual(r_mmatch.json()["repo"], "linux")
            self.assertEqual(r_mmatch.json()["recipients"][0]["email"], "alice@example.com")
            self.assertEqual(self.client.post("/api/v1/maintainers/match", headers=self.auth, json={}).status_code, 400)

            r_per = self.client.get("/api/v1/maintainers/person?email=alice@example.com", headers=self.auth)
            self.assertEqual(r_per.status_code, 200)
            self.assertEqual(r_per.json()["repo"], "linux")
            self.assertEqual(r_per.json()["mode"], "profile")
            self.assertEqual(r_per.json()["name"], "Alice Dev")

            r_devs = self.client.get("/api/v1/maintainers/person?mode=developers&q=Alice", headers=self.auth)
            self.assertEqual(r_devs.status_code, 200)
            self.assertEqual(r_devs.json()["mode"], "developers")
            self.assertEqual(r_devs.json()["total"], 1)

            r_cred = self.client.get("/api/v1/maintainers/person?mode=credits&q=Alice", headers=self.auth)
            self.assertEqual(r_cred.status_code, 200)
            self.assertEqual(r_cred.json()["mode"], "credits")
            self.assertEqual(r_cred.json()["total"], 1)

    def test_p2_diffs_insights_watch_and_branch_tags(self) -> None:
        # 1. Diffs (blobs, kconfig, kapi, man)
        with patch.object(v1_router._diff_service, "get_blob_diff", return_value={
            "version_a": "v3.0", "path_a": "kernel/foo.c", "version_b": "master", "path_b": "include/linux/foo.h", "hunks": [],
        }), patch.object(v1_router._diff_service, "get_kconfig_diff", return_value={
            "added": [], "removed": [], "changed": [{"name": "FOO_BAR"}],
        }), patch("webapp.backend.services.kapi_diff_service.diff", return_value={
            "state": "done", "result": {"counts": {"syscalls": 1}, "syscalls": {"added": ["getcpu"], "removed": [], "changed": []}},
        }), patch.object(v1_router.man_service, "diff", return_value={
            "a": "man-pages-6.10", "b": "master", "repo": "man-pages",
            "summary": {"added": 1, "modified": 0, "deleted": 0},
            "entries": [{"name": "read", "section": "2", "status": "added", "path": "man2/read.2"}],
        }):
            r_bd = self.client.get(
                "/api/v1/diff/blobs?version_a=v3.0&path_a=kernel/foo.c&version_b=master&path_b=include/linux/foo.h",
                headers=self.auth,
            )
            self.assertEqual(r_bd.status_code, 200)
            self.assertEqual(r_bd.json()["repo"], "linux")
            # Both paths are validated against the repo jail
            self.assertEqual(
                self.client.get(
                    "/api/v1/diff/blobs?version_a=v3.0&path_a=../etc/passwd&version_b=master&path_b=kernel/foo.c",
                    headers=self.auth,
                ).status_code,
                400,
            )
            self.assertEqual(
                self.client.get(
                    "/api/v1/diff/blobs?version_a=v3.0&path_a=kernel/foo.c&version_b=master&path_b=../etc/passwd",
                    headers=self.auth,
                ).status_code,
                400,
            )

            r_kd = self.client.get("/api/v1/diff/kconfig?version_a=v3.0&version_b=master", headers=self.auth)
            self.assertEqual(r_kd.status_code, 200)
            self.assertEqual(r_kd.json()["repo"], "linux")
            self.assertEqual(r_kd.json()["changed"][0]["name"], "FOO_BAR")

            r_kad = self.client.get("/api/v1/diff/kapi?version_a=v3.0&version_b=master&kinds=syscalls", headers=self.auth)
            self.assertEqual(r_kad.status_code, 200)
            self.assertEqual(r_kad.json()["repo"], "linux")
            self.assertEqual(r_kad.json()["counts"]["syscalls"], 1)

            r_md = self.client.get(
                "/api/v1/diff/man?repo=man-pages&version_a=man-pages-6.10&version_b=master&path=man2/read.2",
                headers=self.auth,
            )
            self.assertEqual(r_md.status_code, 200)
            self.assertEqual(r_md.json()["repo"], "man-pages")
            self.assertEqual(r_md.json()["total"], 1)
            self.assertEqual(
                self.client.get(
                    "/api/v1/diff/man?repo=man-pages&version_a=man-pages-6.10&version_b=master&path=../etc/passwd",
                    headers=self.auth,
                ).status_code,
                400,
            )

        # 2. Insights (stable series/summary/missing, subsystems, cves)
        with patch("webapp.backend.services.stable_service.stable_series", return_value=[
            {"series": "v3.0", "stable_tags": ["v3.0.1"], "last": "v3.0.1"},
        ]), patch("webapp.backend.services.stable_service.stable_summary", return_value={
            "series": "v3.0", "commits": 12, "backports": 10,
        }), patch("webapp.backend.services.stable_service.missing_report", return_value={
            "state": "done", "result": {"series": "v3.0", "candidates": [{"sha": self.linux_sha, "subject": "Fix foo"}]},
        }), patch("webapp.backend.services.insights_service.subsystem_health", return_value={
            "state": "done",
            "result": {
                "release": "v3.0",
                "window": 4,
                "sections": [{"sec_id": 1, "name": "FOO SUBSYSTEM", "commits": 5, "flags": ["single_author"]}],
            },
        }), patch.object(v1_router.cve_service, "report", return_value={
            "state": "done",
            "result": {
                "release": "v3.0",
                "configured": True,
                "ingested": True,
                "cves": [{"id": "CVE-2024-0001", "state": "affected", "score": 7.8}],
            },
        }):
            r_st_ser = self.client.get("/api/v1/insights/stable", headers=self.auth)
            self.assertEqual(r_st_ser.status_code, 200)
            self.assertEqual(r_st_ser.json()["repo"], "linux")
            self.assertEqual(r_st_ser.json()["mode"], "series")
            self.assertEqual(r_st_ser.json()["total"], 1)

            r_st_sum = self.client.get("/api/v1/insights/stable?series=v3.0&mode=summary", headers=self.auth)
            self.assertEqual(r_st_sum.status_code, 200)
            self.assertEqual(r_st_sum.json()["mode"], "summary")
            self.assertEqual(r_st_sum.json()["commits"], 12)

            r_st_mis = self.client.get("/api/v1/insights/stable?series=v3.0", headers=self.auth)
            self.assertEqual(r_st_mis.status_code, 200)
            self.assertEqual(r_st_mis.json()["mode"], "missing")
            self.assertEqual(r_st_mis.json()["total"], 1)

            r_sub = self.client.get("/api/v1/insights/subsystems?flag=single_author", headers=self.auth)
            self.assertEqual(r_sub.status_code, 200)
            self.assertEqual(r_sub.json()["repo"], "linux")
            self.assertEqual(r_sub.json()["total"], 1)

            r_sub_one = self.client.get("/api/v1/insights/subsystems?sec_id=1", headers=self.auth)
            self.assertEqual(r_sub_one.status_code, 200)
            self.assertEqual(r_sub_one.json()["mode"], "section")
            self.assertEqual(r_sub_one.json()["section"]["name"], "FOO SUBSYSTEM")

            r_icve = self.client.get("/api/v1/insights/cves?state=affected&cvss_op=ge&cvss_score=7.0", headers=self.auth)
            self.assertEqual(r_icve.status_code, 200)
            self.assertEqual(r_icve.json()["repo"], "linux")
            self.assertEqual(r_icve.json()["total"], 1)

        # 3. Watch changes & branch tags
        r_watch = self.client.post(
            "/api/v1/watch/changes",
            headers=self.auth,
            json={
                "from": "v3.0",
                "to": "master",
                "items": [{"key": "w1", "type": "file", "path": "kernel/foo.c"}],
            },
        )
        self.assertEqual(r_watch.status_code, 200)
        self.assertEqual(r_watch.json()["repo"], "linux")
        self.assertEqual(r_watch.json()["items"][0]["status"], "same")

        r_btags = self.client.get("/api/v1/branches/tags?branch=master", headers=self.auth)
        self.assertEqual(r_btags.status_code, 200)
        self.assertEqual(r_btags.json()["repo"], "linux")
        self.assertIn("v3.0", r_btags.json()["tags"])

    def test_p3_rest_only_utilities_and_webapi_limit_clamping(self) -> None:
        # 1. P3 REST-only endpoints: compile_commands, treemap, checkpatch
        with patch.object(v1_router._fs_service, "export_compile_commands", return_value=[
            {"directory": "/linux", "file": "kernel/foo.c", "command": "gcc -c kernel/foo.c"},
        ]):
            r_cc = self.client.get("/api/v1/compile_commands?arch=x86_64", headers=self.auth)
            self.assertEqual(r_cc.status_code, 200)
            self.assertEqual(r_cc.json()["repo"], "linux")
            self.assertEqual(r_cc.json()["total"], 1)
            self.assertEqual(r_cc.json()["commands"][0]["file"], "kernel/foo.c")

        r_tm = self.client.get("/api/v1/treemap?max_depth=2", headers=self.auth)
        self.assertEqual(r_tm.status_code, 200)
        self.assertEqual(r_tm.json()["repo"], "linux")
        self.assertGreaterEqual(r_tm.json()["file_count"], 1)

        # Checkpatch returns {enabled: false, repo: "linux"} by default when webapp.tools.checkpatch.enabled is false
        r_cp = self.client.post(
            "/api/v1/checkpatch",
            headers=self.auth,
            json={"patch": "--- a/kernel/foo.c\n+++ b/kernel/foo.c\n@@ -1 +1 @@\n-int a;\n+int b;\n"},
        )
        self.assertEqual(r_cp.status_code, 200)
        self.assertEqual(r_cp.json()["repo"], "linux")
        self.assertFalse(r_cp.json()["enabled"])

        # 2. Verify /webapi/* routes clamp user-supplied limit to <= 200
        from webapp.backend.routers import commits as web_commits, filesystem as web_fs

        with patch.object(web_fs.fs_service, "search_files", return_value={"files": [], "total": 0}) as m_sfiles:
            web_fs.search_files("v3.0", q="foo", limit=500)
            self.assertEqual(m_sfiles.call_args.args[2], 200)

        with patch.object(web_commits.git_service, "get_file_history", return_value={"commits": []}) as m_fhist:
            web_commits.get_file_history("v3.0", path="kernel/foo.c", limit=500)
            self.assertEqual(m_fhist.call_args.args[2], 200)


class TestVersionOrderingAndDefaultResolution(unittest.TestCase):
    """Verify `list_versions` `order` (`desc` default / `asc`) and `default_version` resolution."""

    def setUp(self) -> None:
        self._orig_cfg = copy.deepcopy(core_config._ACTIVE_CONFIG)
        self._orig_cfg_path = core_config._LOADED_CONFIG_PATH
        self._orig_pinned = core_config._API_DEFAULT_VERSION_PINNED
        core_config.reset_config()
        self._orig_env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.linux_dir = Path(self._tmp.name) / "linux"
        _init_sample_git_repo(self.linux_dir, {"README": "hi\n"}, "v3.0")
        self.cfg_path = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        core_config.reset_config()
        os.environ.clear()
        os.environ.update(self._orig_env)
        core_config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core_config._LOADED_CONFIG_PATH = self._orig_cfg_path
        core_config._API_DEFAULT_VERSION_PINNED = self._orig_pinned
        self._tmp.cleanup()

    def test_list_versions_order_desc_default_and_asc(self) -> None:
        self.cfg_path.write_text(
            json.dumps(
                {
                    "workspace": {"auto_detect_repos": False},
                    "repositories": [{"name": "linux", "path": str(self.linux_dir), "default": True}],
                    "api": {"keys": {"agent": "tok"}},
                }
            ),
            encoding="utf-8",
        )
        core_config.init_config(self.cfg_path)
        client = TestClient(create_app())
        hdrs = {"Authorization": "Bearer tok"}
        sample_versions = {
            "total": 5,
            "versions": [
                {"vname": "0.01", "ref_type": 1, "indexed": False},
                {"vname": "v3.0", "ref_type": 1, "indexed": True},
                {"vname": "v6.1", "ref_type": 1, "indexed": True},
                {"vname": "v6.6", "ref_type": 1, "indexed": True},
                {"vname": "master", "ref_type": 2, "indexed": True},
            ],
        }
        with patch.object(v1_router, "get_versions", return_value=sample_versions):
            r_def = client.get("/api/v1/versions", headers=hdrs).json()
            self.assertEqual(r_def["order"], "desc")
            self.assertEqual([v["vname"] for v in r_def["versions"]], ["v6.6", "v6.1", "v3.0", "0.01", "master"])

            r_asc = client.get("/api/v1/versions?order=asc", headers=hdrs).json()
            self.assertEqual(r_asc["order"], "asc")
            self.assertEqual([v["vname"] for v in r_asc["versions"]], ["0.01", "v3.0", "v6.1", "v6.6", "master"])

    def test_default_version_resolves_latest_indexed_mainline_unless_pinned(self) -> None:
        from api.config import resolve_default_version

        sample_versions = {
            "versions": [
                {"vname": "v3.0", "ref_type": 1, "indexed": True},
                {"vname": "v6.1", "ref_type": 1, "indexed": True},
                {"vname": "v6.1.12", "ref_type": 1, "indexed": True},
                {"vname": "v6.2-rc1", "ref_type": 1, "indexed": True},
                {"vname": "v6.6", "ref_type": 1, "indexed": False},
                {"vname": "master", "ref_type": 2, "indexed": True},
            ]
        }

        # Unpinned config resolves to latest indexed final mainline release (v6.1)
        self.cfg_path.write_text(
            json.dumps(
                {
                    "workspace": {"auto_detect_repos": False},
                    "repositories": [{"name": "linux", "path": str(self.linux_dir), "default": True}],
                    "api": {"keys": {"agent": "tok"}},
                }
            ),
            encoding="utf-8",
        )
        core_config.init_config(self.cfg_path)
        with patch("webapp.backend.routers.versions.get_versions", return_value=sample_versions):
            self.assertEqual(resolve_default_version(), "v6.1")
            client = TestClient(create_app())
            with patch.object(v1_router, "projects_catalog", return_value=[]):
                caps = client.get("/api/v1/capabilities", headers={"Authorization": "Bearer tok"}).json()
                self.assertEqual(caps["default_version"], "v6.1")

        # Explicitly pinned api.default_version preserves the pin
        self.cfg_path.write_text(
            json.dumps(
                {
                    "workspace": {"auto_detect_repos": False},
                    "repositories": [{"name": "linux", "path": str(self.linux_dir), "default": True}],
                    "api": {"default_version": "v3.0", "keys": {"agent": "tok"}},
                }
            ),
            encoding="utf-8",
        )
        core_config.init_config(self.cfg_path)
        with patch("webapp.backend.routers.versions.get_versions", return_value=sample_versions):
            self.assertEqual(resolve_default_version(), "v3.0")

        # Env API_DEFAULT_VERSION overrides dynamic resolution
        with (
            patch.dict(os.environ, {"API_DEFAULT_VERSION": "v6.6"}),
            patch("webapp.backend.routers.versions.get_versions", return_value=sample_versions),
        ):
            self.assertEqual(resolve_default_version(), "v6.6")


if __name__ == "__main__":
    unittest.main()


class TestKconfigServiceEvaluate(unittest.TestCase):
    """`KconfigService.evaluate()` (behind GET /api/v1/kconfig/eval) on a real model built from a tiny tree."""

    def test_defconfig_assignments_symbols_and_build_conditions(self) -> None:
        from parser.kconfig_ast.kconfig_model import KconfigEnv, KconfigModelBuilder
        from webapp.backend.services.kconfig_service import KconfigService

        files = {
            "Kconfig": (
                "config MODULES\n\tbool \"modules\"\n\toption modules\n"
                "config FOO\n\tbool \"foo\"\n"
                "config BAR\n\ttristate \"bar\"\n\tdepends on FOO\n"
            ),
        }
        model = KconfigModelBuilder(files.get, KconfigEnv(ARCH="x86_64", SRCARCH="x86", KERNELVERSION="3.0.0")).build("Kconfig")

        class _Entry:
            arch = {"arch": "x86_64"}

            def evaluator(self):
                from parser.kconfig_ast.kconfig_eval import KconfigEvaluator
                return KconfigEvaluator(model)

        svc = KconfigService()
        defconfig = {"arch": "x86_64", "file_path": "arch/x86/configs/x86_64_defconfig", "content": "CONFIG_FOO=y\n"}
        conditions = {"files": {"drivers/bar.c": {"status": "ok", "expr": {"sym": "BAR"}}}}
        with patch.object(svc, "get_defconfig_content", return_value=defconfig), \
                patch.object(svc, "get_model", return_value=_Entry()), \
                patch.object(svc, "build_conditions", return_value=conditions):
            res = svc.evaluate(
                "v3.0", "x86_64", assignments={"MODULES": "y", "BAR": "m", "NOPE": "y"},
                symbols=["FOO", "BAR", "MISSING"], paths=["drivers/bar.c"],
            )
        self.assertEqual(res["symbols"]["FOO"]["value"], "y")
        self.assertEqual(res["symbols"]["BAR"]["value"], "m")
        self.assertFalse(res["symbols"]["MISSING"]["defined"])
        self.assertEqual(res["rejected"], {"NOPE": "y"})
        self.assertEqual(res["files"]["drivers/bar.c"]["result"], "m")
