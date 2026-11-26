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
            "version": "v3.0", "path": "kernel/foo.c", "arch": "x86_64",
            "headers": [{"header": "linux/foo.h", "resolved": "include/linux/foo.h", "symbols": []}],
        }):
            r_imp = self.client.get("/api/v1/file/imports?path=kernel/foo.c", headers=self.auth)
            self.assertEqual(r_imp.status_code, 200)
            self.assertEqual(r_imp.json()["headers"][0]["header"], "linux/foo.h")

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
            self.assertEqual(r_sym.json()["symbols"][0]["name"], "foo_helper")

        with patch.object(v1_router._symbol_service, "get_symbol_detail", return_value={
            "version": "v3.0", "name": "foo_helper", "file_path": "kernel/foo.c", "line_s": 3,
        }):
            r_def = self.client.get("/api/v1/symbols/definition?name=foo_helper", headers=self.auth)
            self.assertEqual(r_def.status_code, 200)
            self.assertEqual(r_def.json()["file_path"], "kernel/foo.c")

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
