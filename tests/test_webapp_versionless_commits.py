"""tests/test_webapp_versionless_commits.py - Versionless commit routes and GitService/commit_query defaults."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.DBLayout import (
    m_bridge_commit_person,
    m_commit,
    m_commit_file,
    m_commit_parent,
    m_file_name,
    m_git_ref,
    m_ingest_state,
    m_maintainer_person,
    m_repository,
)
from tests.db_fixtures import make_db, patch_reader
from webapp.backend.routers import commits as commits_router
from webapp.backend.services import commit_query as cq
from webapp.backend.services.git_service import GitService


class TestVersionlessCommitService(unittest.TestCase):
    def _make_commit_db(self, *, with_commits: bool = True):
        rows = {
            "m_repository": [{"repo_id": 1, "network_id": 1, "name": "linux", "git_url": "/tmp/linux", "default_branch": "master"}],
            "m_git_ref": [{"ref_id": 1, "repo_id": 1, "ref_type": 1, "ref_name": "v3.0", "commit_id": 100}],
            "m_maintainer_person": [{"person_id": 1, "name": "Linus Torvalds", "email": "torvalds@linux-foundation.org"}],
        }
        if with_commits:
            rows["m_commit"] = [
                {
                    "commit_id": 100, "network_id": 1, "commit_hash": b"\x02" * 20,
                    "author_id": 1, "author_date": 1311200000, "committer_id": 1, "committer_date": 1311200000,
                    "subject": "Linux 3.0", "message": "Linux 3.0",
                },
                {
                    "commit_id": 250, "network_id": 1, "commit_hash": b"\xaa" * 20,
                    "author_id": 1, "author_date": 1700000000, "committer_id": 1, "committer_date": 1700000000,
                    "subject": "latest: fix race", "message": "latest: fix race",
                },
            ]
        return make_db("sqlite", tables=[m_repository, m_git_ref, m_ingest_state, m_commit, m_commit_file, m_maintainer_person], rows=rows)

    def test_commit_detail_reports_parents_and_committer(self) -> None:
        """The commit details node (NodeMap) shows the committer and spawns the parents by hash."""
        rows = {
            "m_maintainer_person": [
                {"person_id": 1, "name": "Ada Author", "email": "ada@example.org"},
                {"person_id": 2, "name": "Carl Committer", "email": "carl@example.org"},
            ],
            "m_commit": [
                {"commit_id": 10, "network_id": 1, "commit_hash": b"\x11" * 20, "author_id": 1, "author_date": 1000,
                 "committer_id": 1, "committer_date": 1000, "subject": "base", "message": "base"},
                {"commit_id": 11, "network_id": 1, "commit_hash": b"\x22" * 20, "author_id": 1, "author_date": 2000,
                 "committer_id": 2, "committer_date": 2100, "subject": "child", "message": "child\n\nSigned-off-by: Ada"},
            ],
            "m_commit_parent": [{"commit_id": 11, "parent_commit_id": 10, "parent_idx": 0}],
        }
        db = make_db("sqlite", tables=[m_commit, m_commit_parent, m_commit_file, m_file_name, m_bridge_commit_person, m_maintainer_person], rows=rows)
        svc = GitService()
        with patch_reader(db), patch.object(svc, "_fill_missing_commit_files"):
            detail = svc.get_commit_detail("11")
        self.assertEqual(detail["parents"], ["11" * 20])
        self.assertEqual(detail["committer"]["name"], "Carl Committer")
        self.assertEqual(detail["committer"]["email"], "carl@example.org")
        self.assertEqual(detail["committer_date"], 2100)
        self.assertFalse(detail["is_merge"])

    def test_get_commits_allows_empty_version_and_valid_git_rev(self) -> None:
        db = self._make_commit_db(with_commits=True)
        svc = GitService()
        with patch_reader(db), patch("webapp.backend.services.git_service.get_active_network_id", return_value=1):
            res = svc.get_commits("", page=1, limit=10, query="latest")
            self.assertEqual(res["version"], "")
            self.assertEqual(len(res["commits"]), 1)
            self.assertEqual(res["commits"][0]["subject"], "latest: fix race")

            # Valid Git SHA not in m_version succeeds via rev-parse instead of 404
            with patch.object(svc, "run_git", return_value=("a" * 40 + "\n", "", 0)):
                res_sha = svc.get_commits("a" * 40, page=1, limit=10, query="")
                self.assertEqual(res_sha["version"], "a" * 40)
                self.assertEqual(len(res_sha["commits"]), 2)

    def test_get_commit_timeline_without_version_anchors_on_newest_commit(self) -> None:
        db = self._make_commit_db(with_commits=True)
        svc = GitService()
        with patch_reader(db), patch("webapp.backend.services.git_service.get_active_network_id", return_value=1):
            res = svc.get_commit_timeline("", limit=10, direction="past")
            self.assertEqual(res["version"], "")
            self.assertEqual(len(res["timeline"]), 2)
            # Starts at newest commit (250), not v3.0 (100)
            self.assertEqual(res["timeline"][0]["commit_id"], 250)
            self.assertEqual(res["timeline"][0]["subject"], "latest: fix race")

            for gran in ("year", "month", "week", "day"):
                res_gran = svc.get_commit_timeline("", limit=10, direction="past", granularity=gran)
                self.assertEqual(len(res_gran["timeline"]), 2)
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as ctx:
                svc.get_commit_timeline("", limit=10, direction="past", granularity="decade")
            self.assertEqual(ctx.exception.status_code, 400)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestVersionlessCommitGitAndRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name)
        cls.repo = repo
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_COMMITTER_NAME": "C",
            "GIT_COMMITTER_EMAIL": "c@x.org",
        }

        def commit(path: str, message: str, author: str, date: str) -> None:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(message)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "-m", message, f"--author={author}", f"--date={date}"],
                check=True,
                env={**env, "GIT_COMMITTER_DATE": date},
            )

        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "master"], check=True, env=env)
        commit("net/a.c", "net: first commit", "Ann <ann@x.org>", "2024-01-01T10:00:00")
        subprocess.run(["git", "-C", str(repo), "tag", "v6.8"], check=True, env=env)
        commit("net/b.c", "net: second commit", "Bob <bob@x.org>", "2024-02-01T10:00:00")
        cls.head_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env,
        ).stdout.strip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_versionless_query_and_git_timeline_fallback(self) -> None:
        from webapp.backend.services import release_service as rs

        empty_db = make_db(
            "sqlite",
            tables=[m_repository, m_git_ref, m_ingest_state, m_commit, m_commit_file, m_maintainer_person],
            rows={"m_repository": [{"repo_id": 1, "network_id": 1, "name": "linux", "git_url": str(self.repo), "default_branch": "master"}]},
        )
        svc = GitService()
        with patch_reader(empty_db), \
                patch("webapp.backend.services.git_service.get_active_network_id", return_value=1), \
                patch("webapp.backend.services.git_service.get_repo_dir", lambda: self.repo), \
                patch.object(cq, "get_repo_dir", lambda: self.repo), \
                patch.object(rs, "get_repo_dir", lambda: self.repo), \
                patch("webapp.backend.services.commit_facts.commit_ids", return_value={}):
            rs._TAGS_CACHE.clear()
            tl = svc.get_commit_timeline("", limit=10)
            self.assertEqual(tl["version"], "")
            self.assertEqual([c["commit_hash"] for c in tl["timeline"]], [self.head_sha, tl["timeline"][1]["commit_hash"]])

            q_res = cq.query_commits("", paths=["net/b.c"])
            self.assertEqual(q_res["version"], "")
            self.assertEqual([c["subject"] for c in q_res["commits"]], ["net: second commit"])

    def test_versionless_fastapi_routes(self) -> None:
        api_app = FastAPI()
        api_app.include_router(commits_router.router)
        client = TestClient(api_app)
        with patch("webapp.backend.routers.commits.git_service.get_commits", return_value={"version": "", "commits": []}) as m_list, \
                patch("webapp.backend.routers.commits.git_service.get_commit_detail", return_value={"commit_hash": self.head_sha}) as m_det, \
                patch("webapp.backend.routers.commits.git_service.get_commit_diff", return_value={"commit_hash": self.head_sha, "files": []}) as m_diff, \
                patch("webapp.backend.routers.commits.git_service.get_commit_timeline", return_value={"version": "", "timeline": []}) as m_tl, \
                patch("webapp.backend.routers.commits.git_service.resolve_commit_mail", return_value={"resolved": False}) as m_mail, \
                patch("webapp.backend.services.commit_query.query_commits", return_value={"version": "", "commits": []}) as m_q, \
                patch("webapp.backend.services.release_service.first_containing", return_value={"sha": self.head_sha, "indexed": True, "released": False}), \
                patch("webapp.backend.services.release_service.ensure_release_index", return_value={"state": "done"}), \
                patch("webapp.backend.services.commit_relations.relations", return_value={"state": "done", "sha": self.head_sha, "fixes": [], "fixed_by": [], "reverts": [], "reverted_by": [], "upstream": [], "backports": []}), \
                patch("webapp.backend.services.commit_relations.status", return_value={"state": "done"}), \
                patch("webapp.backend.services.commit_facts.git_facts", return_value={}), \
                patch("webapp.backend.services.commit_facts.commit_ids", return_value={}), \
                patch("webapp.backend.services.commit_facts.release_badges", return_value={}), \
                patch("webapp.backend.services.cve_service.for_commit", return_value={"fixes": [], "introduced": [], "fixes_upstream": []}), \
                patch("webapp.backend.services.commit_relations.relation_counts", return_value=({self.head_sha: {"fixes": 0}}, set())):
            self.assertEqual(client.get("/api/commits/list?q=test").status_code, 200)
            m_list.assert_called_once_with("", 1, 50, "test")

            self.assertEqual(client.get("/api/commits/search?q=te").status_code, 200)

            self.assertEqual(client.get(f"/api/commits/detail/{self.head_sha}").status_code, 200)
            m_det.assert_called_once_with(self.head_sha)

            self.assertEqual(client.get(f"/api/commits/diff/{self.head_sha}").status_code, 200)
            m_diff.assert_called_once_with(self.head_sha, 3)

            self.assertEqual(client.get("/api/commits/timeline?limit=20").status_code, 200)
            m_tl.assert_called_once_with("", limit=20, direction="past", cursor_id=None)

            for gran in ("year", "month", "week", "day", "MONTH"):
                self.assertEqual(client.get(f"/api/commits/timeline?limit=10&granularity={gran}").status_code, 200)
                self.assertEqual(client.get(f"/api/commits/v6.8/timeline?limit=10&granularity={gran}").status_code, 200)
            self.assertEqual(client.get("/api/commits/timeline?granularity=hour").status_code, 400)
            self.assertEqual(client.get("/api/commits/v6.8/timeline?granularity=invalid").status_code, 400)

            self.assertEqual(client.post(f"/api/commits/resolve_mail/{self.head_sha}").status_code, 200)
            m_mail.assert_called_once_with(self.head_sha)

            self.assertEqual(client.get("/api/commits/query?author=ann").status_code, 200)
            m_q.assert_called_once()
            self.assertEqual(m_q.call_args.args[0], "")

            self.assertEqual(client.get(f"/api/commits/released/{self.head_sha}").status_code, 200)
            self.assertEqual(client.post("/api/commits/released", json={"hashes": [self.head_sha]}).status_code, 200)
            self.assertEqual(client.get(f"/api/commits/relations/{self.head_sha}").status_code, 200)
            self.assertEqual(client.post("/api/commits/relations", json={"hashes": [self.head_sha]}).status_code, 200)


class TestCommitAndTreeConcurrencyHardening(unittest.TestCase):
    """BE-001, BE-002, BE-011 regression: commit digit validation, TreeResolver locking, RelationIndex write lock."""

    def test_get_commit_detail_rejects_non_ascii_digits_and_tree_latin1_paths(self) -> None:
        import threading

        from fastapi import HTTPException

        from tests.db_fixtures import make_db
        from webapp.backend.services.git_service import GitService
        from webapp.backend.services.tree_resolver import (
            GitTreeResolver,
            TreeResolverService,
        )

        svc = GitService()
        with patch_reader(make_db("mock")), \
                patch("webapp.backend.services.git_service.get_active_network_id", return_value=1):
            with self.assertRaises(HTTPException) as ctx:
                svc.get_commit_detail("²")
            self.assertEqual(ctx.exception.status_code, 400)

        # GitTreeResolver replaces invalid UTF-8 bytes in paths without surrogates
        resolver = GitTreeResolver()
        bad_ls_tree = b"100644 blob " + b"a" * 40 + b"\tdrivers/\xffbad.c\0"
        with patch.object(resolver, "resolve_commit_sha", return_value="1" * 40), \
                patch.object(resolver, "_run_git", return_value=bad_ls_tree):
            tree = resolver.list_tree("v3.0")
        self.assertEqual(tree, {"drivers/\ufffdbad.c": "a" * 40})

        # Concurrent TreeResolverService lookups share _lock cleanly
        db = make_db("mock")
        ts = TreeResolverService()
        barrier = threading.Barrier(4)
        results: list[dict[int, int]] = []

        def worker() -> None:
            barrier.wait()
            results.append(ts.resolve_tree(0, reader=db))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [{}, {}, {}, {}])

    def test_relation_index_write_acquires_lock(self) -> None:
        import threading
        from contextlib import nullcontext
        from unittest.mock import MagicMock

        from webapp.backend.services.commit_relations import RelationIndex

        idx = RelationIndex()
        fake_store = MagicMock()
        fake_space = MagicMock()
        fake_space.batch.return_value = nullcontext()
        fake_space.store.return_value = fake_store
        # Acquire _write_lock on main thread briefly, verify background write blocks until release
        entered = threading.Event()
        with idx._write_lock:
            t = threading.Thread(target=lambda: (idx._write(fake_space, [], [], []), entered.set()))
            t.start()
            self.assertFalse(entered.wait(timeout=0.05))
        self.assertTrue(entered.wait(timeout=2.0))
        t.join()


if __name__ == "__main__":
    unittest.main()
