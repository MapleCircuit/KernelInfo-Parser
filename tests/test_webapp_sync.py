"""tests/test_webapp_sync.py - Change tokens, build-bound assets and version stamps (webapp sync)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webapp.backend import app as app_module
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


class TestVersionStamps(unittest.TestCase):
    def test_stamp_changes_when_parsed_or_moved(self) -> None:
        sha = "17d857be649a21ca90008c6dc425d849fa83db5c"
        self.assertEqual(_stamp(sha, None), "17d857be649a.git")
        self.assertEqual(_stamp(sha, 0), "17d857be649a.0")  # parsed before ingestion states existed
        self.assertNotEqual(_stamp(sha, 1761400000), _stamp(sha, 1761500000))  # parsed again
        self.assertNotEqual(_stamp(sha, None), _stamp("02f8c6aee8df" + "0" * 28, None))  # branch moved


if __name__ == "__main__":
    unittest.main()
