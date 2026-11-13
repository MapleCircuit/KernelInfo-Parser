"""tests/test_webapp_sync.py - Change tokens, build-bound assets and version stamps (webapp sync)."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_engine import MockDB
from webapp.backend import app as app_module
from tests.db_fixtures import patch_reader
from webapp.backend.routers import versions as versions_router
from webapp.backend.routers.versions import _stamp
from webapp.backend.services import catalog_cache as cc
from webapp.backend.services import sync_service as ss
from starlette.requests import Request


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
        db = MockDB(use_global=False)
        db.insert_rows("m_git_ref", ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id"], [(1, 1, 1, "v3.0", 5)])
        db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(1, 5, 2, 100)])
        svc = ss.SyncService()
        with patch_reader(db), \
             patch.object(ss, "get_active_repo_id", return_value=1), \
             patch.object(ss, "get_active_repo_name", return_value="linux"), \
             patch.object(ss, "clear_parse_dependent_caches") as cleared:
            first = svc.parsed_token()
            db.insert_rows("m_git_ref", ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id"], [(2, 1, 1, "v3.1", 9)])
            db.insert_rows("m_ingest_state", ["network_id", "commit_id", "status", "updated_at"], [(1, 9, ss.INGEST_PARSING, 200)])
            svc._parsed = ss._Ttl(0)
            self.assertEqual(svc.parsed_token(), first)  # an unfinished version changes nothing
            cleared.assert_not_called()
            db.update_rows("m_ingest_state", {"status": 2}, {"commit_id": 9})
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
            with patch.object(ss, "get_reader", side_effect=lambda: (_ for _ in ()).throw(RuntimeError("no db"))), \
                 patch.object(ss, "get_active_repo_name", return_value="linux"), \
                 patch.object(ss, "get_repo_dir", return_value=tmp), \
                 patch.object(ss.subprocess, "run", side_effect=lambda *a, **k: MagicMock(stdout=f"{next(counter)} refs/tags/v3.0")), \
                 patch.object(svc, "projects_token", return_value="r"):
                first = svc.snapshot()["git"]
                self.assertEqual(svc.snapshot()["git"], first)  # cached for 15 s
                self.assertNotEqual(svc.snapshot(fresh=True)["git"], first)  # manual check recomputes

    def test_projects_token_follows_the_repository_list(self) -> None:
        svc = ss.SyncService(Path(tempfile.gettempdir()))
        catalogs = [[{"repo": "linux", "indexed": False}], [{"repo": "linux", "indexed": True}]]
        with patch.object(versions_router, "projects_catalog", side_effect=lambda: catalogs[0]):
            first = svc.projects_token()
            self.assertEqual(len(first), 12)
            catalogs.pop(0)  # the repository got parsed
            self.assertEqual(svc.projects_token(), first)  # cached for 30 s
            self.assertNotEqual(svc.projects_token(fresh=True), first)
        with patch.object(versions_router, "projects_catalog", side_effect=RuntimeError("config unreadable")):
            self.assertEqual(svc.projects_token(fresh=True), "")

    def test_snapshot_lists_the_five_tokens(self) -> None:
        svc = ss.SyncService(Path(tempfile.gettempdir()))
        with patch.object(svc, "db_token", return_value="d"), patch.object(svc, "frontend_build", return_value="f"), \
                patch.object(svc, "parsed_token", return_value="p"), patch.object(svc, "git_token", return_value="g"), \
                patch.object(svc, "projects_token", return_value="r"), patch.object(ss, "get_active_repo_name", return_value="linux"):
            self.assertEqual(svc.snapshot(), {"db": "d", "frontend": "f", "parsed": "p", "git": "g", "projects": "r", "repo": "linux"})

    def test_projects_mark_the_request_repository_active(self) -> None:
        catalog = [{"repo": "linux", "active": False, "tag_views": []}, {"repo": "stable", "active": False, "tag_views": []}]
        with patch.object(versions_router, "projects_catalog", return_value=catalog), \
                patch.object(versions_router, "get_active_repo_name", return_value="stable"):
            res = versions_router.get_projects()
        self.assertEqual((res["total"], res["active"]), (2, "stable"))
        self.assertEqual([p["active"] for p in res["projects"]], [False, True])
        self.assertEqual(list(res["projects"][1]), ["repo", "active", "tag_views"])  # field order kept
        self.assertFalse(catalog[1]["active"])  # the shared catalog is not modified


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


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/api/versions", "query_string": b"", "headers": raw})


def _catalog_rows(n: int = 300) -> list[tuple]:
    """(vid, vname, commit_hash, ref_type, parsed): every third tag only in Git."""
    return [(i + 1 if i % 3 else 0, f"v4.{i}", f"{i:040x}", 1, None if i % 3 == 0 else 1761400000 + i) for i in range(n)]


def _apply_ops(base: list[tuple], ops: list) -> list[tuple]:
    """What the frontend does with a delta (webapp/frontend/js/utils/version_catalog.js)."""
    rows: list[tuple] = []
    for op in ops:
        if isinstance(op[0], int):
            rows.extend(base[op[0]:op[0] + op[1]])
        else:
            rows.append(tuple(list(op) + [None, 1][len(op) - 2:]))
    return rows


class TestCatalogCache(unittest.TestCase):
    def test_built_once_per_repository_state(self) -> None:
        cache = cc.TokenCache()
        tokens = {"parsed": "p1", "git": "g1"}
        seen_previous = []

        def build(previous):
            seen_previous.append(previous)
            return f"v{len(seen_previous)}"

        with patch.object(cc.sync_service, "parsed_token", side_effect=lambda: tokens["parsed"]), \
                patch.object(cc.sync_service, "git_token", side_effect=lambda: tokens["git"]):
            self.assertEqual(cache.get("k", build), "v1")
            self.assertEqual(cache.get("k", build), "v1")  # same state: served from the cache
            tokens["git"] = "g2"  # a ref moved
            self.assertEqual(cache.get("k", build), "v2")
            self.assertEqual(seen_previous, [None, "v1"])  # the build is handed the previous value
            tokens["parsed"] = ""  # token unavailable: the value is only trusted briefly
            self.assertEqual(cache.get("k", build), "v3")
            self.assertEqual(cache.get("k", build), "v3")
            with patch.object(cc, "_UNVERIFIED_TTL_S", 0.0):
                self.assertEqual(cache.get("k", build), "v4")

    def test_concurrent_requests_share_one_build(self) -> None:
        cache = cc.TokenCache()
        started, release = threading.Event(), threading.Event()
        calls, results = [], []

        def build(_previous):
            calls.append(1)
            started.set()
            release.wait(5)
            return "value"

        with patch.object(cc.sync_service, "parsed_token", return_value="p"), \
                patch.object(cc.sync_service, "git_token", return_value="g"):
            threads = [threading.Thread(target=lambda: results.append(cache.get("k", build))) for _ in range(4)]
            for t in threads:
                t.start()
            started.wait(5)
            time.sleep(0.05)
            release.set()
            for t in threads:
                t.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, ["value"] * 4)

    def test_respond_answers_304_for_its_etag_and_gzip_when_accepted(self) -> None:
        body = cc.JsonBody({"x": "y" * 2000})
        r = cc.respond(_request({"Accept-Encoding": "gzip, br"}), body)
        self.assertEqual((r.status_code, r.headers["content-encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(r.body), body.raw)
        self.assertEqual(r.headers["etag"], f'W/"{body.etag}"')
        self.assertEqual(r.headers["cache-control"], "no-cache")
        self.assertIn("X-KI-Repo", r.headers["vary"])
        plain = cc.respond(_request(), body)
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(plain.body, body.raw)
        for inm in (f'W/"{body.etag}"', f'"{body.etag}"', f'"other", W/"{body.etag}"', "*"):
            r = cc.respond(_request({"If-None-Match": inm}), body)
            self.assertEqual((r.status_code, r.body), (304, b""), inm)
        self.assertEqual(cc.respond(_request({"If-None-Match": '"other"'}), body).status_code, 200)
        small = cc.JsonBody({"a": 1})
        self.assertIsNone(small.gz)  # below the gzip threshold
        self.assertNotIn("content-encoding", cc.respond(_request({"Accept-Encoding": "gzip"}), small).headers)
        unvalidated = cc.respond(_request({"If-None-Match": f'W/"{small.etag}"'}), small, cache_control="no-store", etag=False)
        self.assertEqual(unvalidated.status_code, 200)
        self.assertNotIn("etag", unvalidated.headers)


class TestVersionCatalog(unittest.TestCase):
    KEY = ("linux", True)

    def setUp(self) -> None:
        versions_router._HISTORY.clear()

    def tearDown(self) -> None:
        versions_router._HISTORY.clear()

    def _catalog(self, rows: list[tuple], previous=None):
        cat = versions_router.VersionCatalog(self.KEY, rows, versions_router.digest(versions_router.dumps(rows)), previous)
        versions_router._remember_generation(self.KEY, cat.gen, cat.compact)
        return cat

    def test_legacy_layout_and_compact_columns(self) -> None:
        sha = "17d857be649a" + "0" * 28
        rows = [
            (11, "v3.0-rc1", "aa" * 20, 1, 1761400000),
            (0, "v3.0-rc2", "bb" * 20, 1, None),
            (12, "v3.0", sha, 1, 0),
            (13, "master", "", 2, 1761500000),
        ]
        cat = self._catalog(rows)
        legacy = cat.legacy()
        self.assertEqual(legacy["total"], 4)
        self.assertEqual(legacy["versions"][0], {
            "vid": 11, "vname": "v3.0-rc1", "commit_hash": "aa" * 20, "is_prerelease": True,
            "ref_type": 1, "indexed": True, "stamp": "aaaaaaaaaaaa.1761400000",
        })
        self.assertEqual([v["stamp"] for v in legacy["versions"][1:]], ["bbbbbbbbbbbb.git", "17d857be649a.0", ".1761500000"])

        body = json.loads(cat.compact_body.raw)
        self.assertEqual(cat.compact_body.etag, f"c1-{cat.gen}")
        self.assertEqual(body, {
            "g": cat.gen,
            "n": ["v3.0-rc1", "v3.0-rc2", "v3.0", "master"],
            "h": "aaaaaaaaaaaa" "bbbbbbbbbbbb" "17d857be649a" "000000000000",
            "p": [1761400000, None, 0, 1761500000],
            "t": [[3, 2]],
        })
        # The frontend derives each stamp from the columns, identical to the legacy one
        for i, v in enumerate(legacy["versions"]):
            chunk = body["h"][i * 12:(i + 1) * 12]
            parsed = body["p"][i]
            self.assertEqual(f"{'' if chunk == '0' * 12 else chunk}.{'git' if parsed is None else parsed}", v["stamp"])

    def test_delta_rebuilds_the_current_rows(self) -> None:
        rows = _catalog_rows()
        base = self._catalog(rows)
        cur_rows = list(rows)
        cur_rows[30] = (99, "v4.30", rows[30][2], 1, 1761999999)  # finished parsing
        cur_rows.insert(200, (0, "v4.199-rc1", "e" * 40, 1, None))  # a tag was fetched
        del cur_rows[5:7]  # tags were deleted
        cur_rows.append((50, "master", "", 2, 1761500000))  # a branch was parsed
        cur = self._catalog(cur_rows, base)
        body = cur.delta_from(base.gen)
        delta = json.loads(body.raw)
        self.assertEqual((delta["g"], delta["b"]), (cur.gen, base.gen))
        self.assertEqual(_apply_ops(base.compact, delta["d"]), cur.compact)
        self.assertLess(len(body.raw), 200)
        self.assertIs(cur.delta_from(base.gen), body)  # computed once per base

    def test_one_finished_version_is_one_row_between_two_copies(self) -> None:
        rows = _catalog_rows()
        base = self._catalog(rows)
        cur_rows = list(rows)
        cur_rows[30] = (99, "v4.30", rows[30][2], 1, 1761999999)
        cur = self._catalog(cur_rows, base)
        self.assertEqual(json.loads(cur.delta_from(base.gen).raw)["d"],
                         [[0, 30], ["v4.30", rows[30][2][:12], 1761999999], [31, 269]])
        self.assertIs(cur.compact[0], base.compact[0])  # unchanged rows are shared with the previous generation

    def test_unknown_base_or_large_delta_gets_the_full_catalog(self) -> None:
        rows = _catalog_rows()
        base = self._catalog(rows)
        self.assertIsNone(base.delta_from("0123456789abcdef"))
        rewritten = [(vid, name, "f" * 40, rt, parsed) for vid, name, _sha, rt, parsed in rows]  # every commit moved
        self.assertIsNone(self._catalog(rewritten, base).delta_from(base.gen))

    def test_history_is_bounded(self) -> None:
        for i in range(versions_router._HISTORY_MAX + 5):
            versions_router._remember_generation(self.KEY, f"g{i}", [])
        self.assertEqual(len(versions_router._HISTORY[self.KEY]), versions_router._HISTORY_MAX)
        self.assertIsNone(versions_router._generation(self.KEY, "g0"))
        self.assertEqual(versions_router._generation(self.KEY, f"g{versions_router._HISTORY_MAX + 4}"), [])

    def test_unchanged_content_keeps_the_previous_catalog(self) -> None:
        rows = _catalog_rows()
        with patch.object(versions_router, "_version_rows", return_value=rows):
            first = versions_router.VersionCatalog.build(self.KEY)
            self.assertIs(versions_router.VersionCatalog.build(self.KEY, first), first)
        with patch.object(versions_router, "_version_rows", return_value=rows[:-1]):
            self.assertIsNot(versions_router.VersionCatalog.build(self.KEY, first), first)

    def test_endpoint_answers_304_delta_or_full(self) -> None:
        rows = _catalog_rows()
        base = self._catalog(rows)
        cur_rows = list(rows)
        cur_rows[30] = (99, "v4.30", rows[30][2], 1, 1761999999)
        cur = self._catalog(cur_rows, base)
        endpoint = versions_router.versions_endpoint
        with patch.object(versions_router, "version_catalog", return_value=cur):
            self.assertEqual(endpoint(_request({"If-None-Match": f'W/"c1-{cur.gen}"'}), True, True).status_code, 304)
            delta = endpoint(_request({"If-None-Match": f'W/"c1-{base.gen}"'}), True, True)
            self.assertEqual((delta.status_code, delta.headers["cache-control"]), (200, "no-store"))
            self.assertEqual(json.loads(delta.body)["b"], base.gen)
            full = endpoint(_request({"If-None-Match": 'W/"c1-0123456789abcdef"'}), True, True)
            self.assertEqual(json.loads(full.body)["g"], cur.gen)
            # A compact generation never validates the legacy layout
            legacy = endpoint(_request({"If-None-Match": f'W/"c1-{cur.gen}"'}), True, False)
            self.assertEqual(legacy.status_code, 200)
            self.assertEqual(json.loads(legacy.body)["total"], 300)


if __name__ == "__main__":
    unittest.main()
