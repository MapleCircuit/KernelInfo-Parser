"""tests/test_kapi_ingest.py - Tests for KAPI catalog builders, ingest, ReadIntents, and gating."""
from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import DBLayout as D
from core import backfills
from db_engine import SQLiteDB
from parser import kapi_catalog as kc
from parser import kapi_facts as kf
from parser import kapi_ingest as ki
from parser.git_ast import release_stats


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)


def _git_commit_tag(root: Path, files: dict[str, str], tag: str) -> str:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", f"Release {tag}"], check=True)
    subprocess.run(["git", "-C", str(root), "tag", tag], check=True)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{tag}^{{commit}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class TestKapiCatalogPure(unittest.TestCase):
    def test_encode_decode_roundtrip(self) -> None:
        payload = {"exports": [{"symbol": "foo_bar", "variant": "EXPORT_SYMBOL", "namespace": "", "path": "a.c", "line": 3}], "total": 1, "counts": {"EXPORT_SYMBOL": 1}}
        blob = kc.encode_catalog(payload)
        self.assertIsInstance(blob, bytes)
        self.assertEqual(kc.decode_catalog(blob), payload)

    def test_summarize_and_release_summary(self) -> None:
        catalogs = {
            "syscalls": {
                "syscalls": [
                    {"nr": 0, "name": "read", "status": "defined", "definition": {"path": "fs/read_write.c"}},
                    {"nr": 1, "name": "ni", "status": "ni_syscall", "definition": None},
                ],
                "unnumbered": [],
            },
            "exports": {
                "exports": [
                    {"symbol": "vfs_read", "variant": "EXPORT_SYMBOL", "namespace": "", "path": "fs/read_write.c", "line": 10},
                ],
                "total": 1,
                "counts": {"EXPORT_SYMBOL": 1},
            },
            "params": {
                "params": [
                    {"kind": "core", "name": "quiet", "path": "init/main.c", "undocumented": False},
                    {"kind": "setup", "name": "foo", "path": "init/main.c", "undocumented": True},
                ],
                "stale_docs": [],
                "total": 2,
            },
            "tracepoints": {
                "tracepoints": [
                    {"name": "sched_switch", "kind": "event", "system": "sched", "path": "include/trace/events/sched.h", "line": 5},
                ],
                "total": 1,
            },
            "modules": {
                "modules": [
                    {"modname": "e1000", "target": "drivers/net/e1000/e1000.o", "license": "GPL", "members": ["drivers/net/e1000/e1000_main.c"]},
                ],
                "total": 1,
            },
            "devices": {
                "entries": [
                    {"bus": "pci", "vendor": 0x8086, "device": 0x100E, "path": "drivers/net/e1000/e1000_main.c", "line": 20},
                ],
                "tables": 1,
            },
        }
        s_sys = kc.summarize_catalog("syscalls", catalogs["syscalls"])
        self.assertEqual(s_sys["total"], 2)
        self.assertEqual(s_sys["by_status"], {"defined": 1, "ni_syscall": 1})

        s_exp = kc.summarize_catalog("exports", catalogs["exports"])
        self.assertEqual(s_exp["unique_symbols"], 1)

        rel = kc.build_release_summary(catalogs)
        self.assertEqual(rel["syscalls_count"], 2)
        self.assertEqual(rel["exports_count"], 1)
        self.assertEqual(rel["params_count"], 2)
        self.assertEqual(rel["tracepoints_count"], 1)
        self.assertEqual(rel["modules_count"], 1)
        self.assertEqual(rel["devices_count"], 1)
        self.assertIn("fs", rel["subsystems"])
        self.assertEqual(rel["subsystems"]["fs"]["syscalls"], 1)
        self.assertEqual(rel["subsystems"]["fs"]["exports"], 1)
        self.assertIn("drivers/net", rel["subsystems"])
        self.assertEqual(rel["subsystems"]["drivers/net"]["modules"], 1)
        self.assertEqual(rel["subsystems"]["drivers/net"]["devices"], 1)


class TestKapiIngestAndReadIntents(unittest.TestCase):
    def setUp(self) -> None:
        self.db = SQLiteDB(":memory:")
        self.db.create_table(D.TABLES)
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "linux"
        self.repo.mkdir()
        _init_git_repo(self.repo)

        files_v1 = {
            "Makefile": (
                "VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 0\n"
                "SRCARCH := $(ARCH)\nifeq ($(ARCH),x86_64)\nSRCARCH := x86\nendif\n"
                "core-y := fs/\n"
                "drivers-y := drivers/\n"
            ),
            "arch/x86/Makefile": "",
            "arch/x86/entry/syscalls/syscall_64.tbl": "0\tcommon\tread\tsys_read\n",
            "fs/Makefile": "obj-y += read_write.o\n",
            "fs/read_write.c": (
                "SYSCALL_DEFINE3(read, unsigned int, fd, char __user *, buf, size_t, count) { return 0; }\n"
                "EXPORT_SYMBOL(vfs_read);\n"
            ),
            "drivers/Makefile": "obj-$(CONFIG_E1000) += e1000.o\n",
            "drivers/e1000.c": (
                "MODULE_LICENSE(\"GPL\");\n"
                "module_param(debug, int, 0644);\n"
                "static const struct pci_device_id e1000_pci_tbl[] = {\n"
                "    { PCI_DEVICE(0x8086, 0x100E) },\n"
                "    { }\n"
                "};\n"
                "MODULE_DEVICE_TABLE(pci, e1000_pci_tbl);\n"
            ),
            "include/trace/events/sched.h": (
                "#define TRACE_SYSTEM sched\n"
                "TRACE_EVENT(sched_switch,\n"
                "    TP_PROTO(int prev),\n"
                "    TP_ARGS(prev),\n"
                "    TP_STRUCT__entry(__field(int, prev)),\n"
                "    TP_fast_assign(__entry->prev = prev;),\n"
                "    TP_printk(\"prev=%d\", __entry->prev)\n"
                ");\n"
            ),
            "Documentation/admin-guide/kernel-parameters.txt": "\tquiet\t\t[KNL] Disable most log messages\n",
        }
        self.sha1 = _git_commit_tag(self.repo, files_v1, "v6.1")
        self.sha1_rc = _git_commit_tag(self.repo, {"fs/read_write.c": files_v1["fs/read_write.c"] + "\n"}, "v6.2-rc1")

        # Seed minimal m_project, m_git_network, m_repository, m_maintainer_person, m_commit, m_git_ref
        self.db.insert_rows("m_project", ["project_id", "name", "slug"], [(1, "Linux", "linux")])
        self.db.insert_rows("m_git_network", ["network_id", "project_id", "name", "root_commit_hash"], [(1, 1, "mainline", bytes.fromhex(self.sha1))])
        self.db.insert_rows("m_repository", ["repo_id", "network_id", "name", "git_url", "default_branch"], [(1, 1, "linux", str(self.repo), "master")])
        self.db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(1, "Test", "test@example.com")])
        self.db.insert_rows(
            "m_commit",
            ["commit_id", "network_id", "commit_hash", "author_id", "author_date", "committer_id", "committer_date", "subject", "message"],
            [
                (10, 1, bytes.fromhex(self.sha1), 1, 1000, 1, 1000, "v6.1", "v6.1"),
                (11, 1, bytes.fromhex(self.sha1_rc), 1, 2000, 1, 2000, "v6.2-rc1", "v6.2-rc1"),
            ],
        )
        self.db.insert_rows(
            "m_git_ref",
            ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id", "tagger_id", "tagger_date", "tag_message"],
            [
                (1, 1, 1, "v6.1", 10, 1, 1000, ""),
                (2, 1, 1, "v6.2-rc1", 11, 1, 2000, ""),
            ],
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_ingest_kapi_release_and_read_intents(self) -> None:
        res = ki.ingest_kapi_release(self.db, str(self.repo), 1, "v6.1", arches=("x86_64",))
        self.assertEqual(res["status"], "built")
        self.assertEqual(res["commit_id"], 10)

        # Idempotent skip on second call
        res2 = ki.ingest_kapi_release(self.db, str(self.repo), 1, "v6.1", arches=("x86_64",))
        self.assertEqual(res2["status"], "skipped")
        self.assertEqual(res2["reason"], "up_to_date")

        # Force rebuild
        res3 = ki.ingest_kapi_release(self.db, str(self.repo), 1, "v6.1", arches=("x86_64",), force=True)
        self.assertEqual(res3["status"], "built")

        with self.db.reader() as reader:
            self.assertEqual(reader.kapi_resolve_commit_id("v6.1", repo_id=1), 10)
            self.assertEqual(reader.kapi_resolve_commit_id(self.sha1[:12], repo_id=1), 10)

            summary = reader.kapi_release_summary(10, arch="x86_64")
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["syscalls_count"], 1)
            self.assertEqual(summary["exports_count"], 1)
            self.assertEqual(summary["params_count"], 1)
            self.assertEqual(summary["tracepoints_count"], 1)
            self.assertEqual(summary["modules_count"], 1)
            self.assertEqual(summary["devices_count"], 1)

            meta_shared = reader.kapi_catalog_meta(10, arch="")
            self.assertEqual({m["kind"] for m in meta_shared}, {"exports", "tracepoints", "devices"})

            exp_row = reader.kapi_catalog(10, "exports", arch="")
            self.assertIsNotNone(exp_row)
            assert exp_row is not None
            exp_payload = kc.decode_catalog(exp_row["data"])
            self.assertEqual(exp_payload["exports"][0]["symbol"], "vfs_read")

            mod_row = reader.kapi_catalog(10, "modules", arch="x86_64")
            self.assertIsNotNone(mod_row)
            assert mod_row is not None
            mod_payload = kc.decode_catalog(mod_row["data"])
            self.assertEqual(mod_payload["modules"][0]["modname"], "e1000")

    def test_ingest_kapi_gating(self) -> None:
        # Default repo_entry (None -> all_main): only v6.1, not v6.2-rc1
        res = ki.ingest_kapi(self.db, str(self.repo), 1, repo_entry={})
        self.assertEqual(res["tags"], 1)
        self.assertEqual(res["built"], 1)

        # Custom with v6.2-rc1 only
        entry = {"preparse": {"api": {"mode": "custom", "versions": ["v6.2-rc1"]}}}
        res_rc = ki.ingest_kapi(self.db, str(self.repo), 1, repo_entry=entry)
        self.assertEqual(res_rc["tags"], 1)
        self.assertEqual(res_rc["built"], 1)
        with self.db.reader() as reader:
            self.assertIsNotNone(reader.kapi_release_summary(11, arch="x86_64"))

    def test_insights_read_intents_and_backfill_handler(self) -> None:
        self.assertIs(backfills.BACKFILL_HANDLERS["kapi"], backfills.backfill_kapi)
        self.db.insert_rows(
            "m_release_segment",
            ["segment_id", "repo_id", "tag", "tag_hash", "chain", "base_tag", "base_hash", "commits", "merges", "diff_base_tag", "files_changed", "lines_added", "lines_removed", "format"],
            [(1, 1, "v6.1", bytes.fromhex(self.sha1), "mainline", "", b"\x00" * 20, 1, 0, "", 1, 1, 0, release_stats.FORMAT)],
        )
        self.db.insert_rows(
            "m_release_pull",
            ["segment_id", "pull_idx", "merge_hash", "p1_hash", "p2_hash", "merge_date", "commits", "subject", "source_url", "ref_name", "sender", "sec_id", "sec_name", "dirs_json", "body", "format"],
            [(1, 0, b"\x01" * 20, b"\x02" * 20, b"\x03" * 20, 1000, 1, "Merge tag", "", "", "Linus", 0, "", "[]", "", release_stats.MW_FORMAT)],
        )
        self.db.insert_rows(
            "m_release_section_health",
            ["segment_id", "sec_name", "sec_id", "status", "commits", "reviewed_commits", "authors_json", "activity_json", "pat_hash", "format"],
            [(1, "", 0, "", 1, 0, "{}", "[]", b"\x00" * 16, release_stats.HEALTH_FORMAT)],
        )
        self.db.insert_rows(
            "m_release_person",
            ["segment_id", "person_id", "role_type", "commits"],
            [(1, 1, 1, 5)],
        )
        self.db.insert_rows(
            "m_person_first_release",
            ["repo_id", "person_id", "segment_id"],
            [(1, 1, 1)],
        )
        with self.db.reader() as reader:
            segs = reader.release_segments(1, chain="mainline")
            self.assertEqual(len(segs), 1)
            self.assertEqual(segs[0]["tag"], "v6.1")
            pulls = reader.release_pulls([1])
            self.assertEqual(len(pulls), 1)
            self.assertEqual(pulls[0]["subject"], "Merge tag")
            health = reader.release_section_health([1])
            self.assertEqual(len(health), 1)
            counts = reader.release_person_counts([1])
            self.assertEqual(counts[0]["commits"], 5)
            firsts = reader.person_first_releases(1, [1])
            self.assertEqual(firsts[0]["segment_id"], 1)

    def test_processing_kapi_wrapper(self) -> None:
        from pipeline import external_steps

        with (
            mock.patch.object(external_steps, "feature_enabled", return_value=True),
            mock.patch.object(external_steps.G, "DB", side_effect=lambda: contextlib.nullcontext(self.db)),
        ):
            res = external_steps.processing_kapi(str(self.repo), 1, "v6.1", commit_id=10)
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get("status"), "built")
        with self.db.reader() as reader:
            self.assertIsNotNone(reader.kapi_release_summary(10, arch="x86_64"))

    def test_pure_extract_and_parallel_ingest_kapi(self) -> None:
        # Pure extractor works with zero DB dependency and writes cleanly via write_kapi_release_payload
        payload = ki.extract_kapi_release_payload(str(self.repo), "v6.1", arches=("x86_64",))
        self.assertEqual(payload["tag"], "v6.1")
        self.assertGreater(len(payload["new_facts"]), 0)
        self.assertGreater(len(payload["catalog_rows"]), 0)
        self.assertEqual(len(payload["summary_rows"]), 1)

        ki.write_kapi_release_payload(self.db, 10, payload)
        # Re-extracting with existing_facts skips re-extracting already-known blobs
        existing = ki._load_existing_facts(self.db, [sha for sha, _ in payload["new_facts"]])
        payload2 = ki.extract_kapi_release_payload(str(self.repo), "v6.1", arches=("x86_64",), existing_facts=existing)
        self.assertEqual(payload2["new_facts"], [])

        # Multi-tag parallel ingest_kapi across v6.1 and v6.2-rc1
        res = ki.ingest_kapi(self.db, str(self.repo), 1, tags=["v6.1", "v6.2-rc1"], force=True, workers=2)
        self.assertEqual(res["built"], 2)
        self.assertEqual(res["errors"], [])
        with self.db.reader() as reader:
            self.assertIsNotNone(reader.kapi_release_summary(10, arch="x86_64"))
            self.assertIsNotNone(reader.kapi_release_summary(11, arch="x86_64"))

        # Incremental skip after parallel build
        res_skip = ki.ingest_kapi(self.db, str(self.repo), 1, tags=["v6.1", "v6.2-rc1"], workers=2)
        self.assertEqual(res_skip["built"], 0)
        self.assertEqual(res_skip["skipped"], 2)

        # Single-tag multi-core fact extraction (>= 200 blobs triggers fi.extract with workers > 1)
        extra_files = {f"drivers/extra/mod_{i:03d}.c": f"EXPORT_SYMBOL(sym_{i});\n" for i in range(205)}
        sha_many = _git_commit_tag(self.repo, extra_files, "v6.3")
        with mock.patch.object(ki.fi, "extract", wraps=ki.fi.extract) as spy_extract:
            many_payload = ki.extract_kapi_release_payload(str(self.repo), "v6.3", arches=("x86_64",), workers=2)
        self.assertTrue(spy_extract.called)
        self.assertGreaterEqual(len(many_payload["new_facts"]), 205)

        # Backfill KAPI passes workers=None (auto CPU saturation)
        self.db.insert_rows(
            "m_commit",
            ["commit_id", "network_id", "commit_hash", "author_id", "author_date", "committer_id", "committer_date", "subject", "message"],
            [(12, 1, bytes.fromhex(sha_many), 1, 3000, 1, 3000, "v6.3", "v6.3")],
        )
        self.db.insert_rows(
            "m_git_ref",
            ["ref_id", "repo_id", "ref_type", "ref_name", "commit_id", "tagger_id", "tagger_date", "tag_message"],
            [(3, 1, 1, "v6.3", 12, 1, 3000, "")],
        )
        bf_res = backfills.backfill_kapi(self.db, str(self.repo), 1)
        self.assertGreaterEqual(bf_res["built"], 1)


if __name__ == "__main__":
    unittest.main()
