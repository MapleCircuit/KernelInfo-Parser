"""tests/test_webapp_sync.py - Change tokens, build-bound assets and version stamps (webapp sync)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webapp.backend import app as app_module
from webapp.backend.routers import versions as versions_router
from webapp.backend.routers.versions import _stamp
from webapp.backend.services import sync_service as ss


def _route(app, path: str):
    return next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)


class TestFrontendBuild(unittest.TestCase):
    def test_build_follows_file_contents_not_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "js").mkdir()
            (root / "js" / "app.js").write_text("console.log(1);")
            first = ss.SyncService(root).frontend_build()
            self.assertEqual(len(first), 12)
            # A new service (server restart) over identical files reports the same build
            self.assertEqual(ss.SyncService(root).frontend_build(), first)

            svc = ss.SyncService(root)
            svc.frontend_build()
            (root / "js" / "app.js").write_text("console.log(2);")
            later = time.time_ns() + 2_000_000_000  # an edit seconds later (timestamps are coarse)
            os.utime(root / "js" / "app.js", ns=(later, later))
            svc._frontend_checked = 0.0  # skip the 5 s re-check delay
            self.assertNotEqual(svc.frontend_build(), first)

    def test_unchanged_files_are_not_rehashed_within_the_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.js").write_text("1")
            svc = ss.SyncService(root)
            build = svc.frontend_build()
            with patch.object(svc, "_frontend_files", side_effect=AssertionError("walked again")):
                self.assertEqual(svc.frontend_build(), build)


class TestBuildBoundAssets(unittest.TestCase):
    def setUp(self) -> None:
        self.app = app_module.create_app()
        self.build = ss.sync_service.frontend_build()

    def test_index_binds_assets_to_the_build(self) -> None:
        html = app_module.render_index("abc123def456")
        self.assertIn('content="abc123def456"', html)
        self.assertIn('data-entry="/b/abc123def456/js/app.js"', html)
        self.assertIn('href="/b/abc123def456/css/layout.css"', html)
        self.assertNotIn('"/static/', html)
        self.assertNotIn("__KIP_BUILD__", html)

    def test_current_build_is_served_immutable_and_other_builds_conflict(self) -> None:
        serve = _route(self.app, "/b/{build}/{asset_path:path}")
        ok = serve(self.build, "js/app.js")
        self.assertEqual(ok.status_code, 200)
        self.assertIn("immutable", ok.headers["cache-control"])
        stale = serve("000000000000", "js/app.js")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(json.loads(stale.body)["build"], self.build)

    def test_paths_outside_the_frontend_are_refused(self) -> None:
        serve = _route(self.app, "/b/{build}/{asset_path:path}")
        self.assertEqual(serve(self.build, "../backend/app.py").status_code, 404)
        self.assertEqual(serve(self.build, "js/missing.js").status_code, 404)


class TestTokens(unittest.TestCase):
    def test_parsed_token_ignores_versions_still_being_parsed_and_clears_caches_on_change(self) -> None:
        rows = [{"ref_name": "v3.0", "commit_id": 5, "status": 2, "updated_at": 100}]
        cursor = MagicMock()
        cursor.fetchall.side_effect = lambda: list(rows)
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        svc = ss.SyncService()
        with patch.object(ss, "get_db_cursor", return_value=ctx), \
             patch.object(ss, "get_active_repo_id", return_value=1), \
             patch.object(ss, "get_active_repo_name", return_value="linux"), \
             patch.object(ss, "clear_parse_dependent_caches") as cleared:
            first = svc.parsed_token()
            rows.append({"ref_name": "v3.1", "commit_id": 9, "status": ss.INGEST_PARSING, "updated_at": 200})
            svc._parsed = ss._Ttl(0)
            self.assertEqual(svc.parsed_token(), first)  # an unfinished version changes nothing
            cleared.assert_not_called()
            rows[1]["status"] = 2
            self.assertNotEqual(svc.parsed_token(), first)
            cleared.assert_called_once()

    def test_header_middleware_adds_tokens_to_api_responses_only(self) -> None:
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        mw = ss.SyncHeaderMiddleware(app)
        with patch.object(ss.sync_service, "header_value", return_value="aa.bb.cc"):
            asyncio.run(mw({"type": "http", "path": "/api/versions"}, None, send))
            asyncio.run(mw({"type": "http", "path": "/b/x/js/app.js"}, None, send))
        self.assertIn((b"x-kip-sync", b"aa.bb.cc"), sent[0]["headers"])
        self.assertEqual(sent[2]["headers"], [])

    def test_ttl_cache(self) -> None:
        calls = []
        ttl = ss._Ttl(60)
        for _ in range(3):
            ttl.get("k", lambda: calls.append(1) or "v")
        self.assertEqual(len(calls), 1)
        ttl = ss._Ttl(0)
        ttl.get("k", lambda: calls.append(1) or "v")
        time.sleep(0.001)
        ttl.get("k", lambda: calls.append(1) or "v")
        self.assertEqual(len(calls), 3)

    def test_fresh_snapshot_recomputes_cached_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = ss.SyncService(Path(tmp))
            counter = iter(range(100))
            with patch.object(ss, "get_db_cursor", side_effect=lambda: (_ for _ in ()).throw(RuntimeError("no db"))), \
                 patch.object(ss, "get_active_repo_name", return_value="linux"), \
                 patch.object(ss, "get_repo_dir", return_value=tmp), \
                 patch.object(ss.subprocess, "run", side_effect=lambda *a, **k: MagicMock(stdout=f"{next(counter)} refs/tags/v3.0")):
                first = svc.snapshot()["git"]
                self.assertEqual(svc.snapshot()["git"], first)  # cached for 15 s
                self.assertNotEqual(svc.snapshot(fresh=True)["git"], first)  # manual check recomputes


class TestVersionStamps(unittest.TestCase):
    def test_stamp_changes_when_parsed_or_moved(self) -> None:
        sha = "17d857be649a21ca90008c6dc425d849fa83db5c"
        self.assertEqual(_stamp(sha, None), "17d857be649a.git")
        self.assertEqual(_stamp(sha, 0), "17d857be649a.0")  # parsed before ingestion states existed
        self.assertNotEqual(_stamp(sha, 1761400000), _stamp(sha, 1761500000))  # parsed again
        self.assertNotEqual(_stamp(sha, None), _stamp("02f8c6aee8df" + "0" * 28, None))  # branch moved


class TestBranchTagsWarmup(unittest.TestCase):
    """Startup warm-up of the branch-tags walk (webapp/API.md section 2.12)."""

    def setUp(self) -> None:
        versions_router._BRANCH_TAGS_CACHE.clear()
        versions_router._BRANCH_TAGS_INFLIGHT.clear()

    tearDown = setUp

    def test_concurrent_callers_share_one_walk(self) -> None:
        calls = []
        release = threading.Event()

        def slow_walk(head):
            calls.append(head)
            release.wait(5)
            return ["v3.0", "v3.1"]

        results = []
        with patch.object(versions_router, "_merged_tags", side_effect=slow_walk):
            threads = [threading.Thread(target=lambda: results.append(versions_router.branch_tags("abc"))) for _ in range(3)]
            for t in threads:
                t.start()
            time.sleep(0.2)
            release.set()
            for t in threads:
                t.join(5)
            self.assertEqual(versions_router.branch_tags("abc"), ["v3.0", "v3.1"])  # served from the cache
        self.assertEqual(calls, ["abc"])
        self.assertEqual(results, [["v3.0", "v3.1"]] * 3)
        self.assertEqual(versions_router._BRANCH_TAGS_INFLIGHT, {})

    def test_failed_walk_is_not_cached_and_answers_503(self) -> None:
        with patch.object(versions_router, "_merged_tags", return_value=None) as walk, \
                patch.object(versions_router, "_branch_head", return_value="abc"):
            with self.assertRaises(versions_router.HTTPException) as ctx:
                versions_router.get_branch_tags("master")
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertIsNone(versions_router.branch_tags("abc"))
        self.assertEqual(walk.call_count, 2)  # retried, not served from the cache
        self.assertEqual(len(versions_router._BRANCH_TAGS_CACHE), 0)

    def test_prewarm_walks_the_default_branch_of_available_repositories(self) -> None:
        repos = [{"name": "linux", "path": "/x"}, {"name": "gone", "path": "/missing"}]
        branches = {"branches": [{"name": "stable", "is_default": False}, {"name": "master", "is_default": True}]}
        with patch.object(versions_router, "list_repositories", return_value=repos), \
                patch.object(versions_router, "resolve_repo_path", side_effect=lambda r: MagicMock(exists=lambda: r["name"] == "linux")), \
                patch.object(versions_router, "set_active_repository", return_value=object()) as select, \
                patch.object(versions_router, "reset_active_repository") as reset, \
                patch.object(versions_router, "get_versions") as get_versions, \
                patch.object(versions_router, "get_branches", return_value=branches), \
                patch.object(versions_router, "_branch_head", return_value="abc") as head, \
                patch.object(versions_router, "branch_tags", return_value=["v3.0"]) as tags:
            versions_router.prewarm_catalog()
        select.assert_called_once_with("linux")
        reset.assert_called_once()
        get_versions.assert_called_once_with(True)
        head.assert_called_once_with("master")
        tags.assert_called_once_with("abc")

    def test_prewarm_errors_do_not_escape(self) -> None:
        with patch.object(versions_router, "list_repositories", return_value=[{"name": "linux", "path": "/x"}]), \
                patch.object(versions_router, "resolve_repo_path", return_value=MagicMock(exists=lambda: True)), \
                patch.object(versions_router, "set_active_repository", return_value=object()), \
                patch.object(versions_router, "reset_active_repository") as reset, \
                patch.object(versions_router, "get_versions", side_effect=RuntimeError("db down")):
            with self.assertLogs(versions_router.logger, "WARNING"):
                versions_router.prewarm_catalog()
        reset.assert_called_once()


if __name__ == "__main__":
    unittest.main()
