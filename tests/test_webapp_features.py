"""Unit tests for per-project feature exposure and backend router/service feature gating."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import (
    DEFAULT_KERNEL_FEATURES,
    DEFAULT_KERNEL_INSIGHTS,
    DEFAULT_MANPAGES_FEATURES,
    DEFAULT_MANPAGES_INSIGHTS,
    PROJECT_FEATURE_KEYS,
    PROJECT_INSIGHTS_KEYS,
)
from webapp.backend import config as backend_config
from webapp.backend.routers import (
    commits,
    cve,
    docs,
    insights,
    kapi,
    kconfig,
    mail,
    maintainers,
    man,
    tools,
    versions,
)
from webapp.backend.security.middleware import RepoContextMiddleware
from webapp.backend.services import man_service


class TestWebappFeatureGating(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.linux_dir = Path(self._tmp.name) / "linux"
        self.man_dir = Path(self._tmp.name) / "man-pages"
        self.linux_dir.mkdir()
        self.man_dir.mkdir()

        self.repos = [
            {"name": "linux", "project": "linux", "path": str(self.linux_dir)},
            {"name": "man-pages", "project": "man-pages", "path": str(self.man_dir)},
        ]

        app = FastAPI()
        for r in (
            versions.router,
            kconfig.router,
            kapi.router,
            maintainers.router,
            mail.router,
            cve.router,
            docs.router,
            insights.router,
            commits.router,
            tools.router,
            man.router,
        ):
            app.include_router(r)
        app.add_middleware(RepoContextMiddleware)
        self.client = TestClient(app)

        self._patches = [
            patch.object(backend_config, "get_repositories_config", side_effect=lambda: self.repos),
            patch.object(backend_config, "get_repository", side_effect=self._get_repo),
            patch.object(versions, "list_repositories", side_effect=lambda: self.repos),
            patch.object(man_service, "list_repositories", side_effect=lambda: self.repos),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _get_repo(self, name: str | None = None) -> dict | None:
        if not name:
            return self.repos[0]
        return next((r for r in self.repos if r.get("name") == name), None)

    def test_projects_catalog_exposes_resolved_features(self) -> None:
        self.repos.append({
            "name": "custom-kernel",
            "project": "linux",
            "path": str(self.linux_dir),
            "features": {"kconfig": False, "docs": False},
            "insights": {"milestone_rc": 3},
        })
        self.repos[1]["features"] = {"kconfig": True}

        with patch.object(versions, "get_reader", side_effect=RuntimeError("no db")):
            resp = self.client.get("/api/projects")
        self.assertEqual(resp.status_code, 200)
        by_name = {p["repo"]: p for p in resp.json()["projects"]}

        self.assertEqual(set(by_name["linux"]["features"].keys()), set(PROJECT_FEATURE_KEYS))
        self.assertEqual(by_name["linux"]["features"], DEFAULT_KERNEL_FEATURES)

        expected_man = {**DEFAULT_MANPAGES_FEATURES, "kconfig": True}
        self.assertEqual(by_name["man-pages"]["features"], expected_man)

        expected_custom = {**DEFAULT_KERNEL_FEATURES, "kconfig": False, "docs": False}
        self.assertEqual(by_name["custom-kernel"]["features"], expected_custom)

        self.assertEqual(set(by_name["linux"]["insights"].keys()), set(PROJECT_INSIGHTS_KEYS))
        self.assertEqual(by_name["linux"]["insights"], DEFAULT_KERNEL_INSIGHTS)
        self.assertEqual(by_name["man-pages"]["insights"], DEFAULT_MANPAGES_INSIGHTS)
        self.assertEqual(by_name["custom-kernel"]["insights"]["milestone_rc"], 3)

    def test_config_insights_helpers(self) -> None:
        self.assertEqual(backend_config.get_active_insights_config(), DEFAULT_KERNEL_INSIGHTS)
        token = backend_config.set_active_repository("man-pages")
        try:
            self.assertEqual(backend_config.get_active_insights_config(), DEFAULT_MANPAGES_INSIGHTS)
            custom_entry = {"project": "linux", "insights": {"milestone_rc": 5}}
            self.assertEqual(backend_config.get_active_insights_config(custom_entry)["milestone_rc"], 5)
        finally:
            backend_config.reset_active_repository(token)

    def test_config_feature_helpers(self) -> None:
        token = backend_config.set_active_repository("man-pages")
        try:
            self.assertEqual(backend_config.get_active_features(), DEFAULT_MANPAGES_FEATURES)
            self.assertTrue(backend_config.is_feature_enabled("man"))
            self.assertFalse(backend_config.is_feature_enabled("kconfig"))
            self.assertTrue(backend_config.is_feature_enabled("kconfig", self.repos[0]))
        finally:
            backend_config.reset_active_repository(token)

    def test_manpages_default_disables_kernel_only_routers(self) -> None:
        headers = {"X-KI-Repo": "man-pages"}
        blocked = [
            ("GET", "/api/kconfig/v6.9/tree"),
            ("GET", "/api/kapi/v6.9/syscalls"),
            ("GET", "/api/kapi/diff?a=v6.8&b=v6.9"),
            ("GET", "/api/maintainers/v6.9/overview"),
            ("GET", "/api/mail/status"),
            ("GET", "/api/cve/status"),
            ("GET", "/api/docs/v6.9/labels"),
            ("GET", "/api/insights/v6.9/stable/series"),
            ("GET", "/api/insights/v6.9/subsystems"),
            ("GET", "/api/insights/v6.9/cves"),
            ("POST", "/api/commits/v6.9/resolve_mail/abcdef1"),
            ("GET", "/api/tools/v6.9/pahole/task_struct"),
            ("GET", "/api/tools/v6.9/callgraph/sys_read"),
            ("GET", "/api/diff/kconfig/v6.8/v6.9"),
        ]
        for method, path in blocked:
            r = self.client.request(method, path, headers=headers)
            self.assertEqual(r.status_code, 403, f"Expected 403 for {method} {path}, got {r.status_code}")

    def test_override_enables_kconfig_and_compare_respects_flags(self) -> None:
        self.repos[1]["features"] = {"kconfig": True}
        headers = {"X-KI-Repo": "man-pages"}

        with patch.object(kconfig.kconfig_service, "get_tree", return_value={"ok": True}):
            r = self.client.get("/api/kconfig/man-pages-6.10/tree", headers=headers)
            self.assertEqual(r.status_code, 200)

        with patch.object(tools.diff_service, "get_kconfig_diff", return_value={"ok": True}):
            r = self.client.get("/api/diff/kconfig/man-pages-6.9/man-pages-6.10", headers=headers)
            self.assertEqual(r.status_code, 200)

        # Disabling compare blocks both /tools/diff/file and /diff/kconfig
        self.repos[1]["features"] = {"kconfig": True, "compare": False}
        self.assertEqual(
            self.client.get("/api/tools/diff/file?version_a=a&version_b=b&path=README", headers=headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/diff/kconfig/man-pages-6.9/man-pages-6.10", headers=headers).status_code,
            403,
        )

        # Enabling compare with kconfig=False allows /tools/diff/file but blocks /diff/kconfig
        self.repos[1]["features"] = {"kconfig": False, "compare": True}
        with patch.object(tools.diff_service, "get_file_diff", return_value={"ok": True}):
            self.assertEqual(
                self.client.get("/api/tools/diff/file?version_a=a&version_b=b&path=README", headers=headers).status_code,
                200,
            )
        self.assertEqual(
            self.client.get("/api/diff/kconfig/man-pages-6.9/man-pages-6.10", headers=headers).status_code,
            403,
        )

    def test_kapi_diff_requires_api_and_compare(self) -> None:
        self.repos[0]["features"] = {"api": True, "compare": False}
        self.assertEqual(self.client.get("/api/kapi/diff?a=v6.8&b=v6.9").status_code, 403)
        with patch.object(kapi.k, "syscalls", return_value={"state": "done", "result": {"syscalls": []}}):
            self.assertEqual(self.client.get("/api/kapi/v6.9/syscalls").status_code, 200)

    def test_insights_sub_feature_gating(self) -> None:
        self.repos[0]["features"] = {"insights": False}
        self.assertEqual(self.client.get("/api/insights/v6.9/release-stats").status_code, 403)

        self.repos[0]["features"] = {"insights": True, "commits": False}
        self.assertEqual(self.client.get("/api/insights/v6.9/release-stats").status_code, 403)
        self.assertEqual(self.client.get("/api/insights/release-stats/series?tag=v6.9").status_code, 403)
        self.assertEqual(self.client.get("/api/insights/v6.9/merge-window").status_code, 403)

    def test_commits_and_patch_gating(self) -> None:
        self.repos[0]["features"] = {"commits": False, "patch": True}
        self.assertEqual(self.client.get("/api/commits/v6.9/list").status_code, 403)
        with patch.object(commits.git_service, "format_patch", return_value={"patch": ""}):
            r = self.client.post("/api/version/v6.9/patch/format", json={})
            self.assertEqual(r.status_code, 200)

        self.repos[0]["features"] = {"commits": True, "patch": False}
        self.assertEqual(self.client.post("/api/version/v6.9/patch/format", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/version/v6.9/patch/series", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/tools/v6.9/checkpatch", json={"patch": ""}).status_code, 403)

    def test_man_endpoints_and_service_cross_repo_gating(self) -> None:
        # Disable man on linux repo: /api/man/resolve returns available=False, while /api/man/tags returns 403
        self.repos[0]["features"] = {"man": False}
        r = self.client.get("/api/man/resolve?name=read&section=2&kernel=v6.9")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {
            "available": False,
            "url": "https://man7.org/linux/man-pages/man2/read.2.html",
            "name": "read",
            "section": "2",
        })
        self.assertEqual(self.client.get("/api/man/tags").status_code, 403)

        # Disable man on man-pages repo: manpages_repository() returns None and /api/man/resolve on linux returns unavailable
        self.repos[0]["features"] = {"man": True}
        self.repos[1]["features"] = {"man": False}
        self.assertIsNone(man_service.manpages_repository())
        self.assertEqual(self.client.get("/api/man/diff?a=a&b=b", headers={"X-KI-Repo": "man-pages"}).status_code, 403)

        # Disable compare on man-pages repo with man=True: /api/man/diff returns 403
        self.repos[1]["features"] = {"man": True, "compare": False}
        self.assertEqual(self.client.get("/api/man/diff?a=a&b=b", headers={"X-KI-Repo": "man-pages"}).status_code, 403)

    def test_man_linux_block_respects_api_feature(self) -> None:
        with patch.object(man_service.git_tree_resolver, "resolve_commit_sha", return_value="a" * 40):
            self.repos[0]["features"] = {"api": False}
            blk_off = man_service._linux_block("read", "2", {"fn": ["read"]}, None, "v6.9", "linux")
            self.assertEqual(blk_off["repo"], "linux")
            self.assertIsNone(blk_off["syscall"])

            self.repos[0]["features"] = {"api": True}
            blk_on = man_service._linux_block("read", "2", {"fn": ["read"]}, None, "v6.9", "linux")
            self.assertEqual(blk_on["repo"], "linux")
            self.assertEqual(blk_on["syscall"], "read")
