"""tests/test_distributed_parsing.py - Unit tests for Opt-In Multi-Computer Distributed Parsing."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock
from unittest.mock import MagicMock

from core.DBLayout import (
    TABLES,
    m_ast,
    m_file_blob,
    m_file_name,
    m_ingest_id_range,
    m_maintainer_person,
    m_parse_node,
    m_parse_task,
    m_table_sequence,
)
from core import checkpoints, distributed, ingest_state
from table_engine.te_direct_db import TEDirectDB
from table_engine.te_cached_db import TECachedDB


class MockCursor:
    def __init__(self, db: MockConnection):
        self.db = db
        self.rowcount = 0
        self._last_result: list = []

    def execute(self, sql: str, params: tuple = ()):
        self.db.queries.append((sql, params))
        sql_upper = sql.upper().strip()

        if "SELECT NEXT_ID FROM M_TABLE_SEQUENCE" in sql_upper:
            tid = params[0]
            val = self.db.sequences.get(tid)
            self._last_result = [(val,)] if val is not None else []
        elif "SELECT COALESCE(MAX(" in sql_upper:
            self._last_result = [(100,)]
        elif "SELECT TASK_ID, BASE_REF_NAME" in sql_upper:
            st = params[1]
            self._last_result = [
                (t["task_id"], t.get("base_ref_name", ""), t.get("base_commit_id", 0))
                for t in self.db.tasks if t.get("status") == st
            ]
        elif "SELECT T.TASK_ID" in sql_upper:
            if len(params) >= 7:
                net_id = params[1]
                st = params[2]
                now_ts = params[3]
            else:
                net_id = params[0]
                st = params[1]
                now_ts = params[2]
            self._last_result = [
                (t["task_id"], t.get("commit_id", 0), t.get("worker_id", ""), t.get("ref_name", ""), 0, t.get("status", 2))
                for t in self.db.tasks
                if t.get("network_id", 1) == net_id
                and t.get("status") in (st, distributed.TASK_LEASED, distributed.TASK_RECLAIMING)
                and t.get("lease_expires_at", 0) <= now_ts
            ]
        elif "SELECT TASK_ID" in sql_upper:
            eligible = [
                (t["task_id"], t["chain_name"], t["chain_kind"], t["ref_name"], t["commit_id"], t["base_ref_name"], t["base_commit_id"])
                for t in self.db.tasks if t["status"] == distributed.TASK_ELIGIBLE
            ]
            if "CHAIN_NAME = %S" in sql_upper:
                chain = params[2] if len(params) > 2 else params[-1]
                eligible = [e for e in eligible if e[1] == chain]
            self._last_result = eligible
        elif "SELECT 1 FROM M_PARSE_TASK" in sql_upper:
            if "TASK_ID" in sql_upper:
                t_id, w_id, st, now_ts = params
                matches = [
                    t for t in self.db.tasks
                    if t.get("task_id") == t_id
                    and (t.get("worker_id") == w_id or not t.get("worker_id") or t.get("worker_id") in w_id or w_id in t.get("worker_id"))
                    and t.get("status") == st
                    and t.get("lease_expires_at", 0) > now_ts
                ]
                self._last_result = [(1,)] if matches else []
            elif "M_GIT_REF" in sql_upper:
                cid = params[0]
                matching_refs = [
                    r["ref_name"] for r in getattr(self.db, "git_refs", [])
                    if r.get("commit_id") == cid
                ]
                pinned = [
                    t for t in self.db.tasks
                    if t.get("base_ref_name") in matching_refs and t.get("status") in (0, 1, 2)
                ]
                self._last_result = [(1,)] if pinned else []
            elif "BASE_COMMIT_ID IN" in sql_upper or "BASE_REF_NAME IN" in sql_upper:
                net_id = params[0]
                st_tuple = (params[1], params[2])
                cids = set()
                refs = set()
                for p in params[3:]:
                    if isinstance(p, int):
                        cids.add(p)
                    else:
                        refs.add(str(p))
                matched = [
                    t for t in self.db.tasks
                    if t.get("network_id", 1) == net_id
                    and t.get("status") in st_tuple
                    and ((t.get("base_commit_id") in cids and t.get("base_commit_id", 0) > 0) or (t.get("base_ref_name") in refs and t.get("base_ref_name")))
                ]
                self._last_result = [(1,)] if matched else []
            else:
                base_cid = params[0]
                pinned = [t for t in self.db.tasks if t.get("base_commit_id") == base_cid and t.get("status") in (0, 1, 2)]
                self._last_result = [(1,)] if pinned else []
        elif "SELECT COUNT(*) FROM M_PARSE_TASK" in sql_upper:
            if "STATUS IN" in sql_upper:
                st_params = params[1:]
                matched = [t for t in self.db.tasks if t.get("status") in st_params]
                self._last_result = [(len(matched),)]
            elif "STATUS =" in sql_upper:
                st = params[1]
                matched = [t for t in self.db.tasks if t.get("status") == st]
                self._last_result = [(len(matched),)]
            else:
                self._last_result = [(len(self.db.tasks),)]
        elif "SELECT COUNT(*) FROM M_PARSE_NODE" in sql_upper:
            cutoff = params[2] if len(params) > 2 else 0
            active = [
                n for n in self.db.nodes
                if n.get("role") == params[1] and n.get("heartbeat_at", 2000000000) >= cutoff
            ]
            self._last_result = [(len(active),)]
        elif "SELECT MARKED_TABLE_ID" in sql_upper:
            cid = params[0]
            ranges = self.db.id_ranges.get(cid, [])
            self._last_result = ranges
        elif "SELECT BLOB_ID FROM M_INGEST_BLOB" in sql_upper:
            self._last_result = []
        elif "SELECT RETRY_COUNT FROM M_PARSE_TASK" in sql_upper:
            t_id = params[0]
            matched = [t.get("retry_count", 0) for t in self.db.tasks if t["task_id"] == t_id]
            self._last_result = [(matched[0],)] if matched else [(0,)]
        elif "SELECT STATUS FROM M_PARSE_TASK" in sql_upper:
            b_ref = params[1]
            b_cid = params[2]
            matched = [
                (t["status"],) for t in self.db.tasks
                if t.get("ref_name") == b_ref or (b_cid > 0 and t.get("commit_id") == b_cid)
            ]
            self._last_result = matched
        elif "SELECT STATUS FROM M_INGEST_STATE" in sql_upper:
            self._last_result = []
        elif "SELECT NODE_ID, HOSTNAME, PID, HEARTBEAT_AT FROM M_PARSE_NODE" in sql_upper:
            cutoff = params[2]
            exclude_id = params[3]
            active_other = [
                (n["node_id"], n.get("hostname", ""), n.get("pid", 1), n.get("heartbeat_at", 2000000000))
                for n in self.db.nodes
                if n.get("role") == params[1] and n.get("heartbeat_at", 2000000000) >= cutoff and n.get("node_id") != exclude_id
            ]
            self._last_result = active_other
        elif "SELECT NETWORK_ID, COMMIT_ID, REF_NAME FROM M_PARSE_TASK" in sql_upper:
            t_id = params[0]
            matches = [
                (t.get("network_id", 1), t.get("commit_id", 0), t.get("ref_name", ""))
                for t in self.db.tasks if t["task_id"] == t_id
            ]
            self._last_result = matches if matches else [(1, 0, "")]
        elif "SELECT COMMIT_ID, BASE_COMMIT_ID, BASE_REF_NAME FROM M_PARSE_TASK" in sql_upper:
            t_id = params[0]
            matches = [
                (t.get("commit_id", 0), t.get("base_commit_id", 0), t.get("base_ref_name", ""))
                for t in self.db.tasks if t["task_id"] == t_id
            ]
            self._last_result = matches if matches else [(0, 0, "")]
        elif "SELECT REF_NAME FROM M_PARSE_TASK" in sql_upper:
            t_id = params[0]
            matches = [t["ref_name"] for t in self.db.tasks if t["task_id"] == t_id]
            self._last_result = [(matches[0],)] if matches else []
        elif ("SELECT R.REF_NAME" in sql_upper or "SELECT REF_NAME" in sql_upper) and "FROM M_GIT_REF" in sql_upper:
            if hasattr(self.db, "git_refs") and self.db.git_refs:
                net_id = params[0] if len(params) > 1 else 1
                cid = params[1] if len(params) > 1 else params[0]
                matched = [
                    r for r in self.db.git_refs
                    if r.get("network_id", 1) == net_id and r.get("commit_id") == cid
                ]
                matched.sort(key=lambda r: r.get("ref_type", 1))
                self._last_result = [(matched[0]["ref_name"],)] if matched else []
            else:
                self._last_result = [("v3.0",)]
        elif ("SELECT R.COMMIT_ID" in sql_upper or "SELECT COMMIT_ID" in sql_upper) and "FROM M_GIT_REF" in sql_upper:
            if hasattr(self.db, "git_refs") and self.db.git_refs:
                net_id = params[0] if len(params) > 1 else 1
                r_name = params[1] if len(params) > 1 else params[0]
                matched = [
                    r for r in self.db.git_refs
                    if r.get("network_id", 1) == net_id and r.get("ref_name") == r_name and r.get("commit_id", 0) > 0
                ]
                self._last_result = [(matched[0]["commit_id"],)] if matched else []
            else:
                self._last_result = [(99,)]
        elif "WHERE TASK_ID = %S AND STATUS = %S" in sql_upper or "RECLAIMING EXPIRED LEASE" in sql_upper:
            if "RECLAIMING EXPIRED LEASE" in sql_upper:
                exp_at = params[0]
                t_id = params[1]
                old_st = params[2] if len(params) > 2 else distributed.TASK_LEASED
                updated = 0
                for t in self.db.tasks:
                    if t["task_id"] == t_id and t.get("status") in (old_st, distributed.TASK_LEASED, distributed.TASK_RECLAIMING):
                        t["status"] = distributed.TASK_RECLAIMING
                        t["worker_id"] = "reclaiming"
                        t["lease_expires_at"] = exp_at
                        t["error_msg"] = "Reclaiming expired lease"
                        updated += 1
                self.rowcount = updated
            elif "LEASE_EXPIRES_AT" in sql_upper:
                new_st, w_id, exp_at, t_id, old_st = params
                updated = 0
                for t in self.db.tasks:
                    if t["task_id"] == t_id and t["status"] == old_st:
                        t["status"] = new_st
                        t["worker_id"] = w_id
                        t["lease_expires_at"] = exp_at
                        updated += 1
                self.rowcount = updated
            elif "COMMIT_ID" in sql_upper:
                new_st = params[0]
                cid = params[1]
                t_id = params[2]
                old_st = params[3]
                w_id = params[4] if len(params) > 4 else None
                updated = 0
                for t in self.db.tasks:
                    if t["task_id"] == t_id and t["status"] == old_st:
                        if w_id is None or not t.get("worker_id") or t.get("worker_id") == w_id:
                            t["status"] = new_st
                            t["commit_id"] = cid
                            updated += 1
                self.rowcount = updated
            else:
                self.rowcount = 1
        elif "SELECT" in sql_upper and "PREREQUISITE BASE TASK FAILED" in sql_upper:
            net_id = params[0]
            st = params[1]
            self._last_result = [
                (t.get("commit_id", 0), t.get("ref_name", ""))
                for t in self.db.tasks
                if t.get("network_id", 1) == net_id and t.get("status") == st and t.get("error_msg") == "Prerequisite base task failed"
            ]
        elif "PREREQUISITE BASE TASK FAILED" in sql_upper:
            if "STATUS IN" in sql_upper:
                new_st = params[0]
                net_id = params[1]
                st_tuple = (params[2], params[3])
                if len(params) == 6:
                    cid = params[4]
                    r_name = params[5]
                elif "BASE_REF_NAME" in sql_upper:
                    cid = 0
                    r_name = params[4]
                else:
                    cid = params[4]
                    r_name = ""
            elif len(params) == 5:
                new_st, net_id, old_st, cid, r_name = params
                st_tuple = (old_st,)
            elif len(params) == 4 and "BASE_REF_NAME" in sql_upper:
                new_st, net_id, old_st, r_name = params
                cid = 0
                st_tuple = (old_st,)
            elif len(params) == 4 and "BASE_COMMIT_ID" in sql_upper:
                new_st, net_id, old_st, cid = params
                r_name = ""
                st_tuple = (old_st,)
            else:
                new_st, net_id, old_st = params[:3]
                cid = 0
                r_name = ""
                st_tuple = (old_st,)
            updated = 0
            for t in self.db.tasks:
                if t.get("network_id", 1) == net_id and t.get("status") in st_tuple:
                    if (cid > 0 and t.get("base_commit_id") == cid) or (r_name and t.get("base_ref_name") == r_name):
                        t["status"] = new_st
                        t["error_msg"] = "Prerequisite base task failed"
                        updated += 1
            self.rowcount = updated
        elif "RETRY_COUNT = RETRY_COUNT + 1" in sql_upper:
            err_msg = params[0]
            t_id = params[1]
            w_id = params[2] if len(params) > 2 else None
            updated = 0
            for t in self.db.tasks:
                if t["task_id"] == t_id and t.get("status") in (distributed.TASK_LEASED, distributed.TASK_RECLAIMING):
                    if w_id is None or not t.get("worker_id") or t.get("worker_id") == w_id or t.get("worker_id") == "reclaiming":
                        t["retry_count"] = t.get("retry_count", 0) + 1
                        t["status"] = distributed.TASK_FAILED if t["retry_count"] >= 3 else distributed.TASK_ELIGIBLE
                        t["error_msg"] = err_msg
                        t["worker_id"] = ""
                        updated += 1
            self.rowcount = updated
        elif "SELECT STATUS, RETRY_COUNT FROM M_PARSE_TASK" in sql_upper:
            t_id = params[0]
            matched = [(t.get("status", 4), t.get("retry_count", 3)) for t in self.db.tasks if t["task_id"] == t_id]
            self._last_result = matched if matched else [(distributed.TASK_FAILED, 3)]
        elif "SELECT COMMIT_ID, REF_NAME FROM M_PARSE_TASK" in sql_upper:
            net_id = params[0]
            st = params[1]
            self._last_result = [
                (t.get("commit_id", 0), t.get("ref_name", ""))
                for t in self.db.tasks
                if t.get("network_id", 1) == net_id and t.get("status") == st and t.get("error_msg") == "Prerequisite base task failed"
            ]
        elif "UPDATE M_PARSE_TASK SET STATUS = 4" in sql_upper or f"UPDATE M_PARSE_TASK SET STATUS = {distributed.TASK_FAILED}" in sql_upper:
            err_msg = params[0]
            t_id = params[1]
            for t in self.db.tasks:
                if t["task_id"] == t_id:
                    t["status"] = distributed.TASK_FAILED
                    t["error_msg"] = err_msg
                    t["worker_id"] = ""
            self.rowcount = 1
        elif "UPDATE M_PARSE_TASK SET STATUS = %S, RETRY_COUNT = %S" in sql_upper:
            new_st, new_retries, err_msg, t_id, guard_st = params
            for t in self.db.tasks:
                if t["task_id"] == t_id and t.get("status") != guard_st:
                    t["status"] = new_st
                    t["retry_count"] = new_retries
                    t["error_msg"] = err_msg
                    t["worker_id"] = ""
            self.rowcount = 1
        elif "UPDATE M_PARSE_TASK SET STATUS = %S" in sql_upper and "WHERE TASK_ID = %S" in sql_upper:
            new_st = params[0]
            t_id = params[-1]
            cid = params[1] if len(params) == 3 else None
            for t in self.db.tasks:
                if t["task_id"] == t_id:
                    t["status"] = new_st
                    if cid is not None:
                        t["commit_id"] = cid
            self.rowcount = 1
        elif "UPDATE M_PARSE_TASK SET STATUS" in sql_upper and ("BASE_COMMIT_ID" in sql_upper or "BASE_REF_NAME" in sql_upper):
            new_st = params[0]
            old_st = params[-1]
            updated = 0
            if "BASE_COMMIT_ID = %S OR BASE_REF_NAME = %S" in sql_upper:
                base_cid = params[1]
                base_ref = params[4]
                for t in self.db.tasks:
                    if (t.get("base_commit_id") == base_cid or t.get("base_ref_name") == base_ref) and t["status"] == old_st:
                        t["status"] = new_st
                        t["base_commit_id"] = base_cid
                        updated += 1
            elif "BASE_REF_NAME = %S" in sql_upper:
                base_ref = params[2]
                for t in self.db.tasks:
                    if t.get("base_ref_name") == base_ref and t["status"] == old_st:
                        t["status"] = new_st
                        updated += 1
            else:
                base_cid = params[2]
                for t in self.db.tasks:
                    if t.get("base_commit_id") == base_cid and t["status"] == old_st:
                        t["status"] = new_st
                        updated += 1
            self.rowcount = updated
        elif "INSERT INTO M_PARSE_NODE" in sql_upper:
            if len(params) == 7:
                nid, host, pid, active_cid, now_ts, netid, role = params
            elif len(params) == 6:
                nid, netid, role, host, pid, now_ts = params
            elif len(params) == 5:
                nid, host, pid, active_cid, now_ts = params
                netid = 1
                role = "worker"
            existing = [n for n in self.db.nodes if n["node_id"] == nid]
            if existing:
                existing[0].update({"role": role, "hostname": host, "pid": pid, "heartbeat_at": now_ts})
            else:
                self.db.nodes.append({"node_id": nid, "network_id": netid, "role": role, "hostname": host, "pid": pid, "heartbeat_at": now_ts})
            self.rowcount = 1
        elif "INSERT INTO M_TABLE_SEQUENCE" in sql_upper:
            tid = params[0]
            if len(params) > 1:
                nxt = params[1]
            else:
                nxt = self.db.sequences.get(tid, 100)
            self.db.sequences[tid] = nxt
            self.rowcount = 1
        elif "UPDATE M_TABLE_SEQUENCE" in sql_upper:
            nxt, tid = params[0], params[1]
            self.db.sequences[tid] = nxt
            self.rowcount = 1
        elif "INSERT INTO M_INGEST_ID_RANGE" in sql_upper:
            cid, tid, s_id, e_id = params
            self.db.id_ranges.setdefault(cid, []).append((tid, s_id, e_id))
            self.rowcount = 1
        elif "DELETE FROM" in sql_upper:
            self.rowcount = 1
        else:
            self._last_result = []

    def fetchone(self):
        return self._last_result[0] if self._last_result else None

    def fetchall(self):
        res = list(self._last_result)
        self._last_result = []
        return res

    def query(self, sql: str, params: tuple = (), dictionary: bool = False):
        self.execute(sql, params)
        return self.fetchall()

    def query_one(self, sql: str, params: tuple = (), dictionary: bool = False):
        self.execute(sql, params)
        return self.fetchone()

    def close(self):
        pass


class MockConnection:
    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []
        self.sequences: dict[int, int] = {}
        self.id_ranges: dict[int, list[tuple[int, int, int]]] = {}
        self.tasks: list[dict] = []
        self.nodes: list[dict] = []

    def cursor(self):
        return MockCursor(self)

    def execute(self, sql: str, params: tuple = ()):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur.rowcount

    def executemany(self, sql: str, seq_of_parameters=()):
        cur = self.cursor()
        cnt = 0
        for params in seq_of_parameters:
            cur.execute(sql, params)
            cnt += cur.rowcount
        return cnt

    def query(self, sql: str, params: tuple = (), dictionary: bool = False):
        cur = self.cursor()
        return cur.query(sql, params, dictionary=dictionary)

    def query_one(self, sql: str, params: tuple = (), dictionary: bool = False):
        cur = self.cursor()
        return cur.query_one(sql, params, dictionary=dictionary)

    def query_iter(self, sql: str, params: tuple = (), dictionary: bool = False, chunk_size: int = 2000):
        cur = self.cursor()
        return iter(cur.query(sql, params, dictionary=dictionary))

    def commit(self):
        pass

    def rollback(self):
        pass


class MockDBDriver:
    def __init__(self, cnx: MockConnection):
        self.cnx = cnx

    def execute(self, sql: str, params: tuple = ()):
        return self.cnx.execute(sql, params)

    def executemany(self, sql: str, seq_of_parameters=()):
        return self.cnx.executemany(sql, seq_of_parameters)

    def query(self, sql: str, params: tuple = (), dictionary: bool = False):
        return self.cnx.query(sql, params, dictionary=dictionary)

    def query_one(self, sql: str, params: tuple = (), dictionary: bool = False):
        return self.cnx.query_one(sql, params, dictionary=dictionary)

    def commit(self):
        return self.cnx.commit()

    def rollback(self):
        return self.cnx.rollback()

    def test_tables(self, tables):
        return []

    def get_next_id(self, table):
        return 1

    def select(self, table, data):
        return None

    def insert(self, table, data):
        pass

    def update(self, table, data):
        pass

    def commit_tables_parallel(self, tables_data, max_workers=None, dedicated=False):
        return []

    def reserve_id_block(self, table, block_size, commit_id=0):
        from db_engine.DBHandling import MariaDB
        return MariaDB.reserve_id_block(self, table, block_size, commit_id)


class TestDistributedParsing(unittest.TestCase):
    def test_schema_tables_registered(self):
        """Verify all 66 tables including distributed coordination tables are registered."""
        self.assertEqual(len(TABLES), 66)
        names = {t.table_name for t in TABLES}
        for expected in ("m_table_sequence", "m_ingest_id_range", "m_parse_node", "m_parse_task"):
            self.assertIn(expected, names)
        # Verify unique_keys on m_parse_task uses ("network_id", "ref_name")
        self.assertEqual(m_parse_task.unique_keys, (("network_id", "ref_name"),))

    def test_reserve_id_block_disjoint(self):
        """Verify that concurrent calls to reserve_id_block allocate strictly disjoint ranges."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # Worker A reserves a block for m_ast (50,000)
        start1, end1 = db.reserve_id_block(m_ast, 50_000, commit_id=10)
        self.assertEqual(start1, 100)
        self.assertEqual(end1, 50_099)
        self.assertEqual(cnx.sequences[m_ast.table_id], 50_100)

        # Worker B reserves next block for m_ast (50,000)
        start2, end2 = db.reserve_id_block(m_ast, 50_000, commit_id=11)
        self.assertEqual(start2, 50_100)
        self.assertEqual(end2, 100_099)
        self.assertEqual(cnx.sequences[m_ast.table_id], 100_100)

        # Ensure no overlap
        self.assertGreater(start2, end1)

        # Verify m_ingest_id_range logged blocks
        self.assertIn((m_ast.table_id, start1, end1), cnx.id_ranges[10])
        self.assertIn((m_ast.table_id, start2, end2), cnx.id_ranges[11])

    def test_table_engine_block_allocation(self):
        """Verify TableEngine uses block-allocated sequence IDs in distributed mode."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        te = TEDirectDB()
        te.distributed = True
        te.current_commit_id = 42
        te.start(TABLES, lambda: db)

        # Stage row without explicit ID
        row1 = te.set(m_file_blob.table_id, (None, b"0" * 20, 1))
        row2 = te.set(m_file_blob.table_id, (None, b"1" * 20, 1))

        self.assertEqual(row1[0], 100)
        self.assertEqual(row2[0], 101)
        # Block of 1000 IDs was leased
        self.assertEqual(te._id_limit[m_file_blob.table_id], 1099)

    def test_task_publication_and_worker_claiming(self):
        """Verify Master task publication, dependency unlocking, and worker claiming."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        tags = ["v3.0", "v3.0.1", "v3.0.2", "v3.1-rc1", "v3.1"]
        version_states = {
            "v3.0": (10, "done"),
            "v3.0.1": (11, "new"),
            "v3.0.2": (12, "new"),
            "v3.1-rc1": (13, "new"),
            "v3.1": (14, "new"),
        }

        # Populate tasks into db.tasks
        distributed.publish_delegable_chains(
            db, network_id=1, repo_id=1, all_tags=tags,
            version_states=version_states, delegate_stable=True,
        )

        # Add simulated tasks to mock connection
        cnx.tasks = [
            {"task_id": 1, "chain_name": "stable:v3.0", "chain_kind": "stable", "seq_order": 1,
             "ref_name": "v3.0.1", "commit_id": 11, "base_ref_name": "v3.0", "base_commit_id": 10,
             "status": distributed.TASK_ELIGIBLE, "worker_id": ""},
            {"task_id": 2, "chain_name": "stable:v3.0", "chain_kind": "stable", "seq_order": 2,
             "ref_name": "v3.0.2", "commit_id": 12, "base_ref_name": "v3.0.1", "base_commit_id": 11,
             "status": distributed.TASK_PENDING, "worker_id": ""},
        ]

        # Worker claims next task
        task = distributed.claim_next_task(db, network_id=1, worker_id="worker-node-1")
        self.assertIsNotNone(task)
        self.assertEqual(task["ref_name"], "v3.0.1")
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_LEASED)

        # Worker finishes v3.0.1 -> marks done & unlocks v3.0.2
        distributed.mark_task_done(db, task_id=1, commit_id=11, network_id=1)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_DONE)
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_ELIGIBLE)

    def test_checkpoint_pinning_prevents_compaction(self):
        """Verify is_checkpoint_pinned prevents compacting an ancestor checkpoint into a delta."""
        cnx = MockConnection()
        cnx.tasks = [
            {"task_id": 1, "base_commit_id": 10, "status": distributed.TASK_ELIGIBLE}
        ]

        # commit_id 10 is pinned because task 1 needs it as base
        self.assertTrue(checkpoints.is_checkpoint_pinned(cnx, 10))
        # commit_id 99 is not pinned
        self.assertFalse(checkpoints.is_checkpoint_pinned(cnx, 99))

        # Compaction on pinned commit_id 10 should abort cleanly
        result = checkpoints.compact(cnx, network_id=1, commit_id=10, current=13)
        self.assertIsNone(result)

    def test_worker_waiting_lifecycle(self):
        """Verify worker waiting logic exits only when Master and tasks are finished."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # When queue is empty at startup (Master has not published tasks yet)
        cnx.tasks = []
        cnx.nodes = []
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # When tasks are pending
        cnx.tasks = [{"task_id": 1, "status": distributed.TASK_PENDING}]
        cnx.nodes = [{"node_id": "master:1", "role": "master"}]
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # When no runnable tasks remain but Master is still active
        cnx.tasks = [{"task_id": 1, "status": distributed.TASK_DONE}]
        cnx.nodes = [{"node_id": "master:1", "role": "master"}]
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # When all tasks done and Master has unregistered
        cnx.tasks = [{"task_id": 1, "status": distributed.TASK_DONE}]
        cnx.nodes = []
        self.assertTrue(distributed.is_parsing_finished(db, network_id=1))

    def test_range_isolated_rollback(self):
        """Verify rollback_id_ranges executes bounded range deletes and preserves Git registries."""
        cnx = MockConnection()
        id_ranges = [(m_ast.table_id, 100, 50099)]
        removed = ingest_state.rollback_id_ranges(cnx, commit_id=11, tables=TABLES, id_ranges=id_ranges)
        self.assertGreater(removed, 0)
        # Verify executed queries deleted m_ast between 100 and 50099
        ast_deletes = [
            q for q in cnx.queries if "DELETE FROM M_AST WHERE AST_ID BETWEEN %S AND %S" in q[0].upper()
        ]
        self.assertEqual(len(ast_deletes), 1)
        self.assertEqual(ast_deletes[0][1], (100, 50099))
        # Ensure m_file_name was NOT deleted
        fn_deletes = [
            q for q in cnx.queries if "DELETE FROM M_FILE_NAME" in q[0].upper()
        ]
        self.assertEqual(len(fn_deletes), 0)

    def test_unlock_dependent_tasks_base_ref(self):
        """Verify unlock_dependent_tasks updates tasks matching base_ref_name and sets base_commit_id."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)
        cnx.tasks = [
            {"task_id": 1, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 0, "status": distributed.TASK_PENDING},
            {"task_id": 2, "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 0, "status": distributed.TASK_PENDING},
        ]
        # Unlocking v3.0 with base_commit_id=10 and base_ref_name="v3.0"
        count = distributed.unlock_dependent_tasks(db, network_id=1, base_commit_id=10, base_ref_name="v3.0")
        self.assertEqual(count, 1)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(cnx.tasks[0]["base_commit_id"], 10)
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_PENDING)

    def test_mark_task_failed_guards_done(self):
        """Verify mark_task_failed guards against updating TASK_DONE tasks."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)
        cnx.tasks = [{"task_id": 1, "retry_count": 0, "status": distributed.TASK_DONE}]
        result = distributed.mark_task_failed(db, task_id=1, error_msg="Late timeout error")
        self.assertFalse(result)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_DONE)
        update_queries = [
            q for q in cnx.queries if "UPDATE M_PARSE_TASK SET RETRY_COUNT" in q[0].upper()
        ]
        self.assertEqual(len(update_queries), 1)
        self.assertIn("STATUS IN (2, 5)", update_queries[0][0].upper())

    def test_reclaim_expired_leases_lookup_ref(self):
        """Verify reclaim_expired_leases looks up commit_id from m_git_ref when cid is 0."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)
        # Mock an expired leased task where cid=0
        cnx.tasks = [
            {"task_id": 5, "commit_id": 0, "worker_id": "dead-worker", "ref_name": "v3.0.5",
             "status": distributed.TASK_LEASED, "lease_expires_at": 100}
        ]
        # When querying for expired tasks:
        # MockCursor will return task_id 5 with cid 0
        def custom_execute(sql, params=()):
            cnx.queries.append((sql, params))
            sql_upper = sql.upper()
            if "FROM M_PARSE_TASK T" in sql_upper:
                cnx.cursor_obj._last_result = [(5, 0, "dead-worker", "v3.0.5")]
            elif "COMMIT_ID FROM M_GIT_REF" in sql_upper:
                cnx.cursor_obj._last_result = [(42,)]
            elif "SELECT RETRY_COUNT" in sql_upper:
                cnx.cursor_obj._last_result = [(0,)]
            else:
                cnx.cursor_obj._last_result = []
            if "UPDATE" in sql_upper or "DELETE" in sql_upper:
                cnx.cursor_obj.rowcount = 1

        # Intercept cursor execution
        orig_cursor = cnx.cursor
        cur = cnx.cursor()
        cnx.cursor_obj = cur
        cur.execute = custom_execute
        cnx.cursor = lambda: cur

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 1)
            mock_rollback.assert_called_once_with(db, 1, 42, TABLES)

    def test_heartbeat_daemon_set_active_preserves_task_id(self):
        """Verify set_active retains current_task_id if task_id is None."""
        daemon = distributed.NodeHeartbeatDaemon(MagicMock, "worker-1")
        daemon.set_active(10, task_id=42)
        self.assertEqual(daemon.active_commit_id, 10)
        self.assertEqual(daemon.current_task_id, 42)

        # Update commit without passing task_id
        daemon.set_active(11)
        self.assertEqual(daemon.active_commit_id, 11)
        self.assertEqual(daemon.current_task_id, 42)

        # Clear active resets both
        daemon.clear_active()
        self.assertEqual(daemon.active_commit_id, 0)
        self.assertIsNone(daemon.current_task_id)

    def test_ingest_git_dag_distributed_reservations_and_heartbeat(self):
        """Verify ingest_git_dag uses reserve_id_block, updates heartbeat, and links leased task."""
        from main import ingest_git_dag, G, gp
        from core.DBLayout import m_commit, m_git_ref

        mock_db = MagicMock()
        mock_db.select.return_value = None
        mock_db.reserve_id_block.side_effect = lambda table, count: (777, 777) if table == m_commit else (888, 888)
        mock_cursor = MagicMock()
        mock_db.cnx.cursor.return_value = mock_cursor

        mock_daemon = MagicMock()
        orig_distributed = getattr(G, "distributed", False)
        orig_node_id = getattr(G, "node_id", None)
        orig_daemon = getattr(G, "heartbeat_daemon", None)
        orig_active_task = getattr(gp, "_active_task_id", None)
        orig_db_ctx = G.DB

        try:
            G.distributed = True
            G.node_id = "worker-node-1"
            G.heartbeat_daemon = mock_daemon
            gp._active_task_id = 123
            gp.Old_VID = 555
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: mock_db, __exit__=lambda s, e, t, b: None)

            mock_git_parser = MagicMock()
            mock_git_parser.get_commit_hash.return_value = "0" * 40
            mock_git_parser.parse_version_commits.return_value = []

            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("main.ingest_state.begin") as mock_begin, \
                 unittest.mock.patch("main.ingest_state.fetch_repo_version_states", return_value={}):
                cid, is_chk, already = ingest_git_dag("v3.0.1", 1, 1, "repo")

            self.assertEqual(cid, 777)
            # Verify reserve_id_block called for both m_commit and m_git_ref
            mock_db.reserve_id_block.assert_any_call(m_commit, 1)
            mock_db.reserve_id_block.assert_any_call(m_git_ref, 1)

            # Verify ingest_state.begin called with base_commit_id and worker_id
            mock_begin.assert_called_once_with(
                mock_db, 1, 777, TABLES, base_commit_id=555, worker_id="worker-node-1"
            )

            # Verify heartbeat daemon updated
            mock_daemon.set_active.assert_called_once_with(777)

            # Verify m_parse_task updated with head_commit_id
            mock_db.execute.assert_any_call(
                "UPDATE m_parse_task SET commit_id = %s WHERE task_id = %s",
                (777, 123),
            )
        finally:
            G.distributed = orig_distributed
            G.node_id = orig_node_id
            G.heartbeat_daemon = orig_daemon
            gp._active_task_id = orig_active_task
            G.DB = orig_db_ctx


    def test_master_leadership_exclusivity(self):
        """Verify only one master can be actively registered per network."""
        import time
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # Register Master 1
        distributed.register_node(db, "master-1", distributed.ROLE_MASTER, network_id=1)
        self.assertEqual(len(cnx.nodes), 1)

        # Register Master 2 while Master 1 is active -> RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            distributed.register_node(db, "master-2", distributed.ROLE_MASTER, network_id=1)
        self.assertIn("Another master node 'master-1' is already active for network 1", str(ctx.exception))

        # Re-registering Master 1 itself succeeds
        distributed.register_node(db, "master-1", distributed.ROLE_MASTER, network_id=1)

        # If Master 1 died (>120s ago), Master 2 can take over
        cnx.nodes[0]["heartbeat_at"] = int(time.time()) - 200
        distributed.register_node(db, "master-2", distributed.ROLE_MASTER, network_id=1)
        self.assertEqual(len(cnx.nodes), 2)

    def test_reclaim_expired_leases_dead_node_cleanup_and_mutual_exclusion(self):
        """Verify reclaim_expired_leases cleans dead nodes and enforces atomic reclamation."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # Set up an expired leased task
        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "worker_id": "dead-node", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": 50, "network_id": 1}
        ]

        def custom_execute(sql, params=()):
            cnx.queries.append((sql, params))
            sql_upper = sql.upper()
            if "FROM M_PARSE_TASK T" in sql_upper:
                cnx.cursor_obj._last_result = [(1, 10, "dead-node", "v3.0.1")]
            elif "SELECT RETRY_COUNT" in sql_upper:
                cnx.cursor_obj._last_result = [(0,)]
            else:
                cnx.cursor_obj._last_result = []
            if "UPDATE M_PARSE_TASK" in sql_upper and "RECLAIMING EXPIRED LEASE" in sql_upper:
                # Simulate another worker already reclaimed it
                cnx.cursor_obj.rowcount = 0
            elif "UPDATE" in sql_upper or "DELETE" in sql_upper:
                cnx.cursor_obj.rowcount = 1

        cur = cnx.cursor()
        cnx.cursor_obj = cur
        cur.execute = custom_execute
        cnx.cursor = lambda: cur

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 0)
            mock_rollback.assert_not_called()

        # Check dead node cleanup was executed
        cleanup_queries = [
            q for q in cnx.queries if "DELETE FROM M_PARSE_NODE WHERE HEARTBEAT_AT < %S" in q[0].upper()
        ]
        self.assertEqual(len(cleanup_queries), 1)

    def test_mark_task_done_lease_ownership_verification(self):
        """Verify mark_task_done enforces worker lease ownership and handles lost leases."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "worker_id": "worker-1", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "network_id": 1}
        ]

        # Valid worker_id completes task
        res = distributed.mark_task_done(db, task_id=1, commit_id=10, network_id=1, worker_id="worker-1")
        self.assertTrue(res)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_DONE)

        # Another worker tries to complete already-done task -> returns False
        res2 = distributed.mark_task_done(db, task_id=1, commit_id=10, network_id=1, worker_id="worker-2")
        self.assertFalse(res2)

    def test_unlock_dependent_tasks_fallback_to_git_ref(self):
        """Verify unlock_dependent_tasks queries m_git_ref when base_ref_name is None."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 2, "base_ref_name": "v3.0", "base_commit_id": 0, "status": distributed.TASK_PENDING, "network_id": 1}
        ]

        # Call with base_ref_name=None and base_commit_id=10
        count = distributed.unlock_dependent_tasks(db, network_id=1, base_commit_id=10, base_ref_name=None)
        self.assertEqual(count, 1)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(cnx.tasks[0]["base_commit_id"], 10)

    def test_mark_task_failed_cascading(self):
        """Verify cascading task cancellation when task reaches max retries."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "retry_count": 2, "status": distributed.TASK_LEASED, "commit_id": 11,
             "ref_name": "v3.0.1", "network_id": 1},
            {"task_id": 2, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 0, "network_id": 1},
        ]

        # Task 1 fails (3rd failure -> TASK_FAILED)
        distributed.mark_task_failed(db, task_id=1, error_msg="Fatal compilation bug")
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_FAILED)
        # Downstream task 2 should be cancelled
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_FAILED)
        self.assertEqual(cnx.tasks[1]["error_msg"], "Prerequisite base task failed")

    def test_is_parsing_finished_startup_and_dead_chain(self):
        """Verify is_parsing_finished returns False on startup and True when orphaned pending tasks cannot run."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # Empty task table -> False (startup guard)
        cnx.tasks = []
        cnx.nodes = []
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # Pending task with failed base task -> True (cannot be unblocked)
        cnx.tasks = [
            {"task_id": 1, "status": distributed.TASK_FAILED, "ref_name": "v3.0", "commit_id": 10},
            {"task_id": 2, "status": distributed.TASK_PENDING, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 10},
        ]
        cnx.nodes = []
        self.assertTrue(distributed.is_parsing_finished(db, network_id=1))

    def test_tecacheddb_negative_cache_poisoning_avoidance(self):
        """Verify TECachedDB does not cache negative nodup misses in distributed mode."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # In standalone mode (distributed=False), misses are cached as None
        te_standalone = TECachedDB()
        te_standalone.distributed = False
        te_standalone.start(TABLES, lambda: db)
        db.select = mock.MagicMock(return_value=None)
        res1 = te_standalone._committed_nodup_id(m_file_name.table_id, (None, "test/path.c"))
        self.assertIsNone(res1)
        self.assertIn(m_file_name.table_id, te_standalone._committed_nodup_keys)
        self.assertIsNone(te_standalone._committed_nodup_keys[m_file_name.table_id].get(("test/path.c",)))

        # In distributed mode (distributed=True), misses are NOT cached in _committed_nodup_keys
        te_dist = TECachedDB()
        te_dist.distributed = True
        te_dist.start(TABLES, lambda: db)
        db.select = mock.MagicMock(return_value=None)
        res2 = te_dist._committed_nodup_id(m_file_name.table_id, (None, "test/path.c"))
        self.assertIsNone(res2)
        keys = te_dist._committed_nodup_keys.get(m_file_name.table_id, {})
        self.assertNotIn(("test/path.c",), keys)

    def test_interrupted_ingests_active_worker_exclusion(self):
        """Verify interrupted_ingests excludes active heartbeating workers in distributed mode."""
        from core import backfills
        now = int(time.time())

        mock_db = mock.MagicMock()
        mock_db.query_one.return_value = (0,)

        count = backfills.interrupted_ingests(mock_db)
        self.assertEqual(count, 0)
        sql, params = mock_db.query_one.call_args[0]
        self.assertIn("m_parse_node", sql)
        self.assertIn("heartbeat_at < %s", sql)
        self.assertEqual(params[0], 1)
        self.assertAlmostEqual(params[1], now - 180, delta=2)

        # Dead worker / abandoned ingestion -> count is 1
        mock_db.query_one.return_value = (1,)
        count2 = backfills.interrupted_ingests(mock_db)
        self.assertEqual(count2, 1)

        # Table or query failure -> fallback query executed
        def fail_first_query(query, *args, **kwargs):
            if "m_parse_node" in query:
                raise RuntimeError("m_parse_node table does not exist")
            return (2,)
        mock_db.query_one.side_effect = fail_first_query
        count3 = backfills.interrupted_ingests(mock_db)
        self.assertEqual(count3, 2)

    def test_master_target_refs_tag_scoping(self):
        """Verify Master task publication scopes all_tags starting from target_refs[0]."""
        all_tags = ["v2.6.39", "v3.0", "v3.0.1", "v3.1", "v3.2"]
        target_refs = ["v3.0", "v3.1", "v3.2"]

        if target_refs and len(target_refs) > 0 and target_refs[0] in all_tags:
            start_idx = all_tags.index(target_refs[0])
            scoped_tags = all_tags[start_idx:]
        else:
            scoped_tags = all_tags

        self.assertEqual(scoped_tags, ["v3.0", "v3.0.1", "v3.1", "v3.2"])
        self.assertNotIn("v2.6.39", scoped_tags)

    def test_mark_task_failed_cid_zero_does_not_cascade_unrelated(self):
        """Verify mark_task_failed when cid==0 does not match or fail tasks with base_commit_id=0."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "retry_count": 2, "status": distributed.TASK_LEASED, "commit_id": 0,
             "ref_name": "v3.0.1", "network_id": 1},
            {"task_id": 2, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 0, "network_id": 1},
            {"task_id": 3, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.1.1", "base_ref_name": "v3.1", "base_commit_id": 0, "network_id": 1},
        ]

        # Task 1 fails (3rd failure -> TASK_FAILED) with cid=0
        distributed.mark_task_failed(db, task_id=1, error_msg="Compilation failure")
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_FAILED)
        # Downstream task 2 matching base_ref_name "v3.0.1" should fail
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_FAILED)
        # Unrelated task 3 with base_commit_id 0 and different base_ref_name MUST NOT fail!
        self.assertEqual(cnx.tasks[2]["status"], distributed.TASK_PENDING)

        # Check query did NOT include "base_commit_id = %s"
        cascade_queries = [
            q for q in cnx.queries if "PREREQUISITE BASE TASK FAILED" in q[0].upper()
        ]
        self.assertEqual(len(cascade_queries), 1)
        self.assertNotIn("BASE_COMMIT_ID =", cascade_queries[0][0].upper())
        self.assertIn("BASE_REF_NAME = %S", cascade_queries[0][0].upper())

    def test_unlock_dependent_tasks_cid_zero_does_not_unlock_unrelated(self):
        """Verify unlock_dependent_tasks with base_commit_id=0 does not unlock unrelated tasks with base_commit_id=0."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 0, "status": distributed.TASK_PENDING, "network_id": 1},
            {"task_id": 2, "ref_name": "v3.1.1", "base_ref_name": "v3.1", "base_commit_id": 0, "status": distributed.TASK_PENDING, "network_id": 1},
        ]

        count = distributed.unlock_dependent_tasks(db, network_id=1, base_commit_id=0, base_ref_name="v3.0")
        self.assertEqual(count, 1)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_ELIGIBLE)
        # Unrelated task 2 MUST remain TASK_PENDING
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_PENDING)

        # Verify query only matched base_ref_name
        unlock_queries = [
            q for q in cnx.queries if "UPDATE M_PARSE_TASK SET STATUS = %S" in q[0].upper() and "BASE_REF_NAME" in q[0].upper()
        ]
        self.assertEqual(len(unlock_queries), 1)
        self.assertNotIn("BASE_COMMIT_ID = %S", unlock_queries[0][0].upper())

    def test_update_node_heartbeat_upserts_dropped_node(self):
        """Verify update_node_heartbeat re-registers a node if rowcount is 0 on update."""
        mock_cur = mock.MagicMock()
        mock_cur.execute.return_value = 0

        mock_db = mock.MagicMock()
        mock_db.execute = mock_cur.execute

        distributed.update_node_heartbeat(mock_db, node_id="worker-test:123", active_commit_id=42)

        # First call was UPDATE m_parse_node
        self.assertIn("UPDATE m_parse_node", mock_cur.execute.call_args_list[0][0][0])
        # Second call was INSERT INTO m_parse_node upsert
        upsert_call = mock_cur.execute.call_args_list[1]
        self.assertIn("INSERT INTO m_parse_node", upsert_call[0][0])
        self.assertIn("ON DUPLICATE KEY UPDATE", upsert_call[0][0])
        params = upsert_call[0][1]
        self.assertEqual(params[0], "worker-test:123")
        self.assertEqual(params[3], 42)

    def test_reclaim_expired_leases_locks_with_reclaiming_worker(self):
        """Verify reclaim_expired_leases locks task in TASK_LEASED with worker_id='reclaiming'."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "worker_id": "dead-node", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": 50, "network_id": 1, "retry_count": 0}
        ]

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 1)
            mock_rollback.assert_called_once_with(db, 1, 10, TABLES)

        lock_queries = [
            q for q in cnx.queries if "WORKER_ID = 'RECLAIMING'" in q[0].upper()
        ]
        self.assertEqual(len(lock_queries), 1)
        self.assertIn("WHERE TASK_ID = %S AND STATUS = %S", lock_queries[0][0].upper())
        self.assertEqual(lock_queries[0][1][2], distributed.TASK_LEASED)
        # Task retry_count was 0 (< 3), so final status from mark_task_failed is TASK_ELIGIBLE, NOT TASK_FAILED
        t1 = next(t for t in cnx.tasks if t["task_id"] == 1)
        self.assertEqual(t1["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(t1["retry_count"], 1)
        self.assertEqual(t1["worker_id"], "")

    def test_publish_delegable_chains_preserves_task_eligible_query(self):
        """Verify publish_delegable_chains query preserves TASK_ELIGIBLE on duplicate key."""
        mock_cur = mock.MagicMock()
        mock_cur.execute.return_value = 1

        mock_db = mock.MagicMock()
        mock_db.execute = mock_cur.execute

        all_tags = ["v3.0", "v3.0.1"]
        version_states = {"v3.0": (10, "done"), "v3.0.1": (11, "todo")}

        count = distributed.publish_delegable_chains(mock_db, network_id=1, repo_id=1, all_tags=all_tags, version_states=version_states)
        self.assertEqual(count, 1)

        insert_call = mock_cur.execute.call_args_list[0]
        sql = insert_call[0][0]
        self.assertIn("status = IF(", sql)

    def test_reserve_id_block_upsert_initialization(self):
        """Verify reserve_id_block executes sequence upsert before SELECT FOR UPDATE."""
        mock_cnx = mock.MagicMock()
        mock_cur = mock.MagicMock()
        mock_cnx.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (500,)

        class MockBaseEngine:
            def __init__(self, cnx):
                self.cnx = cnx

        from db_engine.DBHandling import MariaDB
        engine = MockBaseEngine(mock_cnx)
        start, end = MariaDB.reserve_id_block(engine, m_ast, block_size=1000, commit_id=12)

        self.assertEqual(start, 500)
        self.assertEqual(end, 1499)

        exec_calls = mock_cur.execute.call_args_list
        self.assertIn("INSERT INTO m_table_sequence", exec_calls[0][0][0])
        # The upsert seeds a missing sequence row from MAX(pk) + 1 and never moves an existing one backwards
        self.assertIn("COALESCE(MAX(ast_id), 0) + 1 FROM m_ast", exec_calls[0][0][0])
        self.assertIn("ON DUPLICATE KEY UPDATE next_id = GREATEST(next_id, VALUES(next_id))", exec_calls[0][0][0])
        self.assertIn("SELECT next_id FROM m_table_sequence WHERE seq_table_id = %s FOR UPDATE", exec_calls[1][0][0])
        self.assertIn("UPDATE m_table_sequence SET next_id = %s WHERE seq_table_id = %s", exec_calls[2][0][0])
        self.assertEqual(exec_calls[2][0][1], (1500, m_ast.table_id))

    def test_worker_old_vid_preserved_through_ingest_git_dag(self):
        """Verify worker gp.Old_VID and gp.Old_Version_Name are preserved through ingest_git_dag."""
        from main import ingest_git_dag, G, gp
        from core.DBLayout import m_commit, m_git_ref

        mock_db = MagicMock()
        mock_db.select.return_value = None
        mock_db.reserve_id_block.side_effect = lambda table, count: (777, 777) if table == m_commit else (888, 888)
        mock_cursor = MagicMock()
        mock_db.cnx.cursor.return_value = mock_cursor

        orig_role = getattr(G, "distributed_role", None)
        orig_dist = getattr(G, "distributed", False)
        orig_node = getattr(G, "node_id", None)
        orig_old_vid = getattr(gp, "Old_VID", None)
        orig_vid = getattr(gp, "VID", None)
        orig_old_vname = getattr(gp, "Old_Version_Name", None)
        orig_vname = getattr(gp, "Version_Name", None)
        orig_repair = getattr(gp, "_repair_base_set", None)
        orig_db_ctx = G.DB

        try:
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: mock_db, __exit__=lambda s, e, t, b: None)
            mock_git_parser = MagicMock()
            mock_git_parser.get_commit_hash.return_value = "0" * 40
            mock_git_parser.parse_version_commits.return_value = []

            # 1. Secondary worker mode: Old_VID and Old_Version_Name are preserved from claimed task's base
            G.distributed = True
            G.distributed_role = distributed.ROLE_WORKER
            G.node_id = "worker-node-1"
            gp._repair_base_set = False
            gp.Old_VID = 300
            gp.Old_Version_Name = "v3.0"
            gp.VID = 100
            gp.Version_Name = "v2.6"

            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("main.ingest_state.begin"), \
                 unittest.mock.patch("main.ingest_state.fetch_repo_version_states", return_value={}):
                cid, _, _ = ingest_git_dag("v3.0.1", 1, 1, "repo")

            self.assertEqual(cid, 777)
            self.assertEqual(gp.Old_VID, 300)
            self.assertEqual(gp.Old_Version_Name, "v3.0")
            self.assertEqual(gp.VID, 777)
            self.assertEqual(gp.Version_Name, "v3.0.1")

            # 2. Preset repair run: Old_VID preserved when _repair_base_set is True
            G.distributed_role = "standalone"
            gp._repair_base_set = True
            gp.Old_VID = 400
            gp.Old_Version_Name = "v3.1"
            gp.VID = 200
            gp.Version_Name = "v3.0"

            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("main.ingest_state.begin"), \
                 unittest.mock.patch("main.ingest_state.fetch_repo_version_states", return_value={}):
                cid2, _, _ = ingest_git_dag("v3.1.1", 1, 1, "repo")

            self.assertEqual(gp.Old_VID, 400)
            self.assertEqual(gp.Old_Version_Name, "v3.1")
            self.assertEqual(gp.VID, 777)
            self.assertEqual(gp.Version_Name, "v3.1.1")

            # 3. Normal standalone run: Old_VID shifts from VID
            gp._repair_base_set = False
            gp.VID = 500
            gp.Version_Name = "v3.2"

            with unittest.mock.patch("parser.git_ast.GitCommitParser", return_value=mock_git_parser), \
                 unittest.mock.patch("main.ingest_state.begin"), \
                 unittest.mock.patch("main.ingest_state.fetch_repo_version_states", return_value={}):
                cid3, _, _ = ingest_git_dag("v3.2.1", 1, 1, "repo")

            self.assertEqual(gp.Old_VID, 500)
            self.assertEqual(gp.Old_Version_Name, "v3.2")
            self.assertEqual(gp.VID, 777)
            self.assertEqual(gp.Version_Name, "v3.2.1")
        finally:
            G.distributed_role = orig_role
            G.distributed = orig_dist
            G.node_id = orig_node
            gp.Old_VID = orig_old_vid
            gp.VID = orig_vid
            gp.Old_Version_Name = orig_old_vname
            gp.Version_Name = orig_vname
            if orig_repair is None:
                if hasattr(gp, "_repair_base_set"):
                    delattr(gp, "_repair_base_set")
            else:
                gp._repair_base_set = orig_repair
            G.DB = orig_db_ctx

    def test_finish_version_ingest_aborts_when_worker_lease_lost(self):
        """Verify finish_version_ingest aborts when worker lease is lost/expired."""
        from main import finish_version_ingest, G, gp

        cnx = MockConnection()
        db = MockDBDriver(cnx)

        orig_role = getattr(G, "distributed_role", None)
        orig_dist = getattr(G, "distributed", False)
        orig_node = getattr(G, "node_id", None)
        orig_active = getattr(gp, "_active_task_id", None)
        orig_db_ctx = G.DB

        try:
            G.distributed = True
            G.distributed_role = distributed.ROLE_WORKER
            G.node_id = "worker-node-1"
            gp._active_task_id = 42
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: db, __exit__=lambda s, e, t, b: None)

            # Lease expired in m_parse_task
            cnx.tasks = [
                {"task_id": 42, "worker_id": "worker-node-1", "status": distributed.TASK_LEASED,
                 "lease_expires_at": int(time.time()) - 10}
            ]

            with unittest.mock.patch("core.ingest_state.set_status") as mock_set_status:
                with self.assertRaises(RuntimeError) as ctx:
                    finish_version_ingest(999)
                self.assertIn("Worker lease for task 42 expired or was revoked", str(ctx.exception))
                mock_set_status.assert_not_called()

            # Now with active valid lease: should succeed
            cnx.tasks[0]["lease_expires_at"] = int(time.time()) + 300
            with unittest.mock.patch("core.ingest_state.set_status") as mock_set_status, \
                 unittest.mock.patch("main.refresh_person_identities"), \
                 unittest.mock.patch("main._compact_checkpoint"):
                finish_version_ingest(999)
                mock_set_status.assert_called_once_with(db, 1, 999, ingest_state.INGEST_DONE)
        finally:
            G.distributed_role = orig_role
            G.distributed = orig_dist
            G.node_id = orig_node
            gp._active_task_id = orig_active
            G.DB = orig_db_ctx

    def test_finish_version_ingest_worker_role_skips_unlock_dependent_tasks(self):
        """Verify finish_version_ingest does not call unlock_dependent_tasks when distributed_role is worker."""
        from main import finish_version_ingest, G, gp
        orig_role = getattr(G, "distributed_role", None)
        orig_dist = getattr(G, "distributed", False)
        orig_node = getattr(G, "node_id", None)
        orig_active = getattr(gp, "_active_task_id", None)
        orig_db_ctx = G.DB

        cnx = MockConnection()
        db = MockDBDriver(cnx)
        cnx.tasks = [
            {"task_id": 42, "worker_id": "worker-1", "status": distributed.TASK_LEASED,
             "lease_expires_at": int(time.time()) + 300}
        ]

        try:
            G.distributed = True
            G.distributed_role = distributed.ROLE_WORKER
            G.node_id = "worker-1"
            gp._active_task_id = 42
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: db, __exit__=lambda s, e, t, b: None)

            with unittest.mock.patch("core.ingest_state.set_status"), \
                 unittest.mock.patch("main.refresh_person_identities"), \
                 unittest.mock.patch("main._compact_checkpoint"), \
                 unittest.mock.patch("core.distributed.unlock_dependent_tasks") as mock_unlock:
                finish_version_ingest(999)
                mock_unlock.assert_not_called()

            # For master role, unlock_dependent_tasks should be called
            G.distributed_role = distributed.ROLE_MASTER
            gp._active_task_id = None
            with unittest.mock.patch("core.ingest_state.set_status"), \
                 unittest.mock.patch("main.refresh_person_identities"), \
                 unittest.mock.patch("main._compact_checkpoint"), \
                 unittest.mock.patch("core.distributed.unlock_dependent_tasks") as mock_unlock:
                finish_version_ingest(999)
                mock_unlock.assert_called_once()
        finally:
            G.distributed_role = orig_role
            G.distributed = orig_dist
            G.node_id = orig_node
            gp._active_task_id = orig_active
            G.DB = orig_db_ctx

    def test_ingest_commit_range_id_block_leasing_in_distributed_mode(self):
        """Verify _ingest_commit_range uses block allocation via reserve_id_block in distributed mode."""
        from main import _ingest_commit_range, G, gp
        orig_dist = getattr(G, "distributed", False)
        orig_vid = getattr(gp, "VID", None)

        try:
            G.distributed = True
            gp.VID = 77

            mock_db = mock.MagicMock()
            mock_cur = mock.MagicMock()
            mock_db.cnx.cursor.return_value = mock_cur
            mock_cur.fetchall.return_value = []
            # No stored person matches, so the author needs a new m_maintainer_person id
            mock_db.query.return_value = []
            mock_db.select.return_value = None

            # Reserve blocks: for m_commit return (1001, 2000), for m_maintainer_person return (501, 756)
            def mock_reserve(table, block_size, commit_id=0):
                if table.table_name == "m_commit":
                    self.assertEqual(block_size, 1000)
                    self.assertEqual(commit_id, 77)
                    return (1001, 2000)
                elif table.table_name == "m_maintainer_person":
                    self.assertEqual(block_size, 256)
                    self.assertEqual(commit_id, 77)
                    return (501, 756)
                return (1, 10)

            mock_db.reserve_id_block.side_effect = mock_reserve

            c1 = mock.MagicMock()
            c1.commit_hash = "1" * 40
            c1.author_name = "Dev One"
            c1.author_email = "one@kernel.org"
            c1.committer_name = "Dev One"
            c1.committer_email = "one@kernel.org"
            c1.author_date = 100
            c1.committer_date = 100
            c1.subject = "Commit 1"
            c1.message = "Msg 1"
            c1.parents = ()
            c1.contributors = []

            with mock.patch("main._lookup_commit_ids", return_value={}):
                res = _ingest_commit_range(mock_db, [c1], network_id=1)

            self.assertEqual(res["1" * 40], 1001)
            self.assertEqual(mock_db.reserve_id_block.call_count, 2)
            mock_db.get_next_id.assert_not_called()

            # In non-distributed mode, fallback to db.get_next_id
            G.distributed = False
            mock_db.reset_mock()
            mock_db.get_next_id.return_value = 42
            with mock.patch("main._lookup_commit_ids", return_value={}):
                res2 = _ingest_commit_range(mock_db, [c1], network_id=1)
            mock_db.reserve_id_block.assert_not_called()
            self.assertTrue(mock_db.get_next_id.called)
        finally:
            G.distributed = orig_dist
            gp.VID = orig_vid

    def test_previous_ingest_active_worker_detects_in_progress_and_avoids_rollback(self):
        """Verify _previous_ingest detects active heartbeating worker and avoids rollback."""
        from main import _previous_ingest, ingest_git_dag, G, gp
        orig_dist = getattr(G, "distributed", False)

        try:
            G.distributed = True
            mock_db = mock.MagicMock()

            # 1. When status is INGEST_PARSING and worker is active
            with mock.patch("core.ingest_state.get_status", return_value=ingest_state.INGEST_PARSING):
                # Worker active query returns a row
                mock_db.query_one.return_value = (1,)
                state = _previous_ingest(mock_db, network_id=1, commit_id=100, version_or_ref="v3.0.1")
                self.assertEqual(state, "in_progress")

                # Verify SQL checked m_parse_node heartbeat
                active_sql = mock_db.query_one.call_args[0][0]
                self.assertIn("m_ingest_state s JOIN m_parse_node n", active_sql)
                self.assertIn("n.heartbeat_at >=", active_sql)

                # Worker inactive (no row returned)
                mock_db.query_one.return_value = None
                state_inactive = _previous_ingest(mock_db, network_id=1, commit_id=100, version_or_ref="v3.0.1")
                self.assertEqual(state_inactive, "interrupted")

            # 2. In ingest_git_dag, verify state == 'in_progress' skips rollback
            orig_db_ctx = G.DB
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: mock_db, __exit__=lambda s, e, t, b: None)
            try:
                with mock.patch("main._previous_ingest", return_value="in_progress"), \
                     mock.patch("core.ingest_state.rollback_version") as mock_rollback, \
                     mock.patch("main._purge_partial_version") as mock_purge, \
                     mock.patch("subprocess.run") as mock_subproc, \
                     mock.patch("parser.git_ast.GitCommitParser") as mock_git_parser:
                    mock_subproc.return_value = mock.MagicMock(stdout="40-hex-hash\n", returncode=0)
                    mock_db.select.side_effect = lambda table, pattern: (100, 1) if table.table_name == "m_commit" else None

                    with mock.patch("main.TABLES", []):
                        ingest_git_dag("v3.0.1", network_id=1, repo_id=1, repo_path="/dummy")

                    mock_rollback.assert_not_called()
                    mock_purge.assert_not_called()
            finally:
                G.DB = orig_db_ctx
        finally:
            G.distributed = orig_dist

    def test_publish_delegable_chains_preserves_nonzero_commit_ids(self):
        """Verify publish_delegable_chains ON DUPLICATE KEY UPDATE preserves non-zero commit_id and base_commit_id."""
        mock_cur = mock.MagicMock()
        mock_cur.execute.return_value = 1
        mock_db = mock.MagicMock(execute=mock_cur.execute)

        all_tags = ["v3.0", "v3.0.1"]
        version_states = {"v3.0": (10, "done"), "v3.0.1": (11, "todo")}

        count = distributed.publish_delegable_chains(mock_db, network_id=1, repo_id=1, all_tags=all_tags, version_states=version_states)
        self.assertEqual(count, 1)

        insert_call = mock_cur.execute.call_args_list[0]
        sql = insert_call[0][0]
        self.assertIn("commit_id = IF(VALUES(commit_id) > 0, VALUES(commit_id), commit_id)", sql)
        self.assertIn("base_commit_id = IF(VALUES(base_commit_id) > 0, VALUES(base_commit_id), base_commit_id)", sql)
        self.assertIn("status = IF(", sql)

    def test_claim_next_task_requeries_fresh_values(self):
        """Verify claim_next_task re-queries commit_id, base_commit_id, and base_ref_name after claiming."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # Initial candidate task in database with older snapshot values
        cnx.tasks = [
            {
                "task_id": 10,
                "chain_name": "stable:v3.0",
                "chain_kind": "stable",
                "seq_order": 1,
                "ref_name": "v3.0.1",
                "commit_id": 0,
                "base_ref_name": "v3.0",
                "base_commit_id": 0,
                "status": distributed.TASK_ELIGIBLE,
                "worker_id": "",
            }
        ]

        class CustomMockCursor(MockCursor):
            def execute(self, sql, params=()):
                sql_upper = sql.upper().strip()
                if "UPDATE M_PARSE_TASK SET STATUS = %S" in sql_upper and "WHERE TASK_ID = %S" in sql_upper:
                    for t in self.db.tasks:
                        if t["task_id"] == 10:
                            t["commit_id"] = 101
                            t["base_commit_id"] = 99
                            t["base_ref_name"] = "v3.0"
                super().execute(sql, params)

        cnx.cursor = lambda: CustomMockCursor(cnx)

        claimed = distributed.claim_next_task(db, network_id=1, worker_id="worker-fresh-test")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], 10)
        self.assertEqual(claimed["commit_id"], 101)
        self.assertEqual(claimed["base_commit_id"], 99)
        self.assertEqual(claimed["base_ref_name"], "v3.0")

    def test_publish_delegable_chains_resets_task_failed(self):
        """Verify publish_delegable_chains ON DUPLICATE KEY UPDATE resets TASK_FAILED."""
        mock_cur = mock.MagicMock()
        mock_cur.execute.return_value = 1
        mock_db = mock.MagicMock(execute=mock_cur.execute)

        all_tags = ["v3.0", "v3.0.1"]
        version_states = {"v3.0": (10, "done"), "v3.0.1": (11, "todo")}

        count = distributed.publish_delegable_chains(mock_db, network_id=1, repo_id=1, all_tags=all_tags, version_states=version_states)
        self.assertEqual(count, 1)

        insert_call = mock_cur.execute.call_args_list[0]
        sql = insert_call[0][0]
        self.assertIn("status = IF(status = 4, VALUES(status)", sql)
        self.assertIn("retry_count = IF(status = 4", sql)

    def test_mark_task_failed_cascades_to_eligible_tasks(self):
        """Verify cascading failure marks both TASK_PENDING and TASK_ELIGIBLE dependent tasks as TASK_FAILED."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "retry_count": 2, "status": distributed.TASK_LEASED, "commit_id": 11,
             "ref_name": "v3.0.1", "network_id": 1},
            {"task_id": 2, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.0.2", "base_ref_name": "v3.0.1", "base_commit_id": 0, "network_id": 1},
            {"task_id": 3, "retry_count": 0, "status": distributed.TASK_ELIGIBLE, "commit_id": 0,
             "ref_name": "v3.0.3", "base_ref_name": "v3.0.1", "base_commit_id": 11, "network_id": 1},
        ]

        distributed.mark_task_failed(db, task_id=1, error_msg="Fatal bug")
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_FAILED)
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_FAILED)
        self.assertEqual(cnx.tasks[1]["error_msg"], "Prerequisite base task failed")
        self.assertEqual(cnx.tasks[2]["status"], distributed.TASK_FAILED)
        self.assertEqual(cnx.tasks[2]["error_msg"], "Prerequisite base task failed")

        cascade_queries = [q for q in cnx.queries if "PREREQUISITE BASE TASK FAILED" in q[0].upper()]
        self.assertEqual(len(cascade_queries), 1)
        self.assertIn("STATUS IN (%S, %S)", cascade_queries[0][0].upper())
        self.assertEqual(cascade_queries[0][1][2:4], (distributed.TASK_PENDING, distributed.TASK_ELIGIBLE))

    def test_unlock_dependent_tasks_scoped_by_network(self):
        """Verify unlock_dependent_tasks scopes m_git_ref lookup by network_id and prefers tags over branches."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.git_refs = [
            {"network_id": 1, "commit_id": 10, "ref_type": 2, "ref_name": "refs/heads/v3.0-branch"},
            {"network_id": 1, "commit_id": 10, "ref_type": 1, "ref_name": "v3.0"},
            {"network_id": 2, "commit_id": 10, "ref_type": 1, "ref_name": "v3.0-net2"},
        ]

        cnx.tasks = [
            {"task_id": 1, "retry_count": 0, "status": distributed.TASK_PENDING, "commit_id": 0,
             "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 0, "network_id": 1},
        ]

        count = distributed.unlock_dependent_tasks(db, network_id=1, base_commit_id=10, base_ref_name=None)
        self.assertEqual(count, 1)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_ELIGIBLE)
        self.assertEqual(cnx.tasks[0]["base_commit_id"], 10)

        lookup_queries = [
            q for q in cnx.queries if "FROM M_GIT_REF" in q[0].upper() and "M_REPOSITORY" in q[0].upper()
        ]
        self.assertEqual(len(lookup_queries), 1)
        sql = lookup_queries[0][0].upper()
        self.assertIn("REPO.NETWORK_ID = %S", sql)
        self.assertIn("ORDER BY R.REF_TYPE ASC LIMIT 1", sql)
        self.assertEqual(lookup_queries[0][1], (1, 10))

    def test_reclaim_expired_leases_scoped_by_network(self):
        """Verify reclaim_expired_leases scopes m_git_ref commit lookup by network_id."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 7, "commit_id": 0, "worker_id": "dead-worker", "ref_name": "v3.0.7",
             "network_id": 1, "status": distributed.TASK_LEASED, "lease_expires_at": 100}
        ]

        cnx.git_refs = [
            {"network_id": 2, "ref_name": "v3.0.7", "commit_id": 999},
            {"network_id": 1, "ref_name": "v3.0.7", "commit_id": 42},
        ]

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 1)
            mock_rollback.assert_called_once_with(db, 1, 42, TABLES)

        ref_queries = [
            q for q in cnx.queries if "FROM M_GIT_REF" in q[0].upper() and "M_REPOSITORY" in q[0].upper()
        ]
        self.assertEqual(len(ref_queries), 1)
        sql = ref_queries[0][0].upper()
        self.assertIn("REPO.NETWORK_ID = %S", sql)
        self.assertEqual(ref_queries[0][1], (1, "v3.0.7"))

    def test_is_checkpoint_pinned_matches_base_ref_name(self):
        """Verify is_checkpoint_pinned pins when base_ref_name matches via m_git_ref."""
        cnx = MockConnection()
        cur = cnx.cursor()

        cnx.tasks = [
            {"task_id": 1, "base_commit_id": 0, "base_ref_name": "v3.0", "status": distributed.TASK_ELIGIBLE}
        ]
        cnx.git_refs = [
            {"commit_id": 100, "ref_name": "v3.0"}
        ]

        self.assertTrue(checkpoints.is_checkpoint_pinned(cnx, 100))
        self.assertFalse(checkpoints.is_checkpoint_pinned(cnx, 999))

        cnx.tasks[0]["status"] = distributed.TASK_DONE
        self.assertFalse(checkpoints.is_checkpoint_pinned(cnx, 100))

    def test_reserve_id_block_disables_autocommit_during_lock(self):
        """Verify reserve_id_block sets autocommit=False during the lock and restores original autocommit."""
        mock_cnx = mock.MagicMock()
        mock_cnx.autocommit = True
        mock_cur = mock.MagicMock()
        mock_cnx.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (100,)

        autocommit_during_execute = []

        def track_execute(sql, params=()):
            autocommit_during_execute.append(mock_cnx.autocommit)

        mock_cur.execute.side_effect = track_execute

        class MockBaseEngine:
            def __init__(self, cnx):
                self.cnx = cnx

        from db_engine.DBHandling import MariaDB
        engine = MockBaseEngine(mock_cnx)
        start, end = MariaDB.reserve_id_block(engine, m_ast, block_size=100, commit_id=5)

        self.assertEqual(start, 100)
        self.assertEqual(end, 199)
        self.assertTrue(len(autocommit_during_execute) >= 3)
        self.assertTrue(all(val is False for val in autocommit_during_execute))
        self.assertTrue(mock_cnx.autocommit)
        mock_cnx.commit.assert_called_once()

    def test_worker_error_handler_rolls_back_before_marking_failed(self):
        """Verify worker error handler rolls back partial rows before marking task failed."""
        from main import main, G, gp
        orig_role = getattr(G, "distributed_role", None)
        orig_dist = getattr(G, "distributed", False)
        orig_node = getattr(G, "node_id", None)
        orig_db_ctx = G.DB

        cnx = MockConnection()
        cnx.tasks = [
            {"task_id": 42, "status": distributed.TASK_LEASED, "worker_id": "test-worker-1", "lease_expires_at": 2000000000}
        ]
        db = MockDBDriver(cnx)

        task = {
            "task_id": 42,
            "ref_name": "v3.0.1",
            "base_ref_name": "v3.0",
            "base_commit_id": 10,
            "commit_id": 101,
            "chain_name": "stable:v3.0",
        }

        call_order = []

        def mock_rollback(cnx, net_id, vid, tables):
            call_order.append(("rollback", vid))
            return True

        def mock_mark_failed(db, task_id, msg, *args, **kwargs):
            call_order.append(("mark_failed", task_id))

        mock_args = mock.MagicMock()
        mock_args.role = "worker"
        mock_args.worker_id = "test-worker-1"
        mock_args.worker_once = True
        mock_args.repo = "linux"
        mock_args.ref = ["v3.0.1"]
        mock_args.start_version = None
        mock_args.end_version = None
        mock_args.single_version = True
        mock_args.include_pre_releases = None
        mock_args.deadline_minutes = 0
        mock_args.mem_max = 16.0
        mock_args.reset = False
        mock_args.Drop = False
        mock_args.cve = False
        mock_args.clean_temp = False
        mock_args.resolve_ml = False
        mock_args.branches = False
        mock_args.config = None
        mock_args.detect_repos = False
        mock_args.chain = None
        mock_args.delegate_stable = False
        mock_args.delegate_branch = None
        mock_args.unit_tests = False

        def mock_update(ref_name, repo_path=None):
            gp.VID = 101
            raise RuntimeError("Synthetic parsing failure in worker")

        with mock.patch("main.arg_handling", return_value=mock_args), \
             mock.patch("main.setup_memory_limit"), \
             mock.patch("main._apply_db_server_tuning"), \
             mock.patch("main.get_mailing_list_config", return_value={"enabled": False}), \
             mock.patch("main.resolve_git_network_and_repo", return_value=(1, 1)), \
             mock.patch("core.ingest_state.fetch_repo_version_states", return_value={}), \
             mock.patch("core.distributed.register_node"), \
             mock.patch("core.distributed.NodeHeartbeatDaemon"), \
             mock.patch("core.distributed.reclaim_expired_leases"), \
             mock.patch("core.distributed.claim_next_task", side_effect=[task, None]), \
             mock.patch("main.update", side_effect=mock_update), \
             mock.patch("core.ingest_state.rollback_version", side_effect=mock_rollback), \
             mock.patch("core.distributed.mark_task_failed", side_effect=mock_mark_failed):
            G.DB = lambda: unittest.mock.MagicMock(__enter__=lambda s: db, __exit__=lambda s, e, t, b: None)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 0)
            finally:
                G.distributed_role = orig_role
                G.distributed = orig_dist
                G.node_id = orig_node
                G.DB = orig_db_ctx

        self.assertEqual(len(call_order), 2)
        self.assertEqual(call_order[0], ("rollback", 101))
        self.assertEqual(call_order[1], ("mark_failed", 42))



class TestDistributedCoreFixes(unittest.TestCase):
    """Targeted unit tests for distributed coordination fixes."""

    def test_reclaim_race_renewed_between_select_and_claim(self):
        """Verify that if a lease is renewed between SELECT and claim CAS update, no rollback occurs."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # Worker renewed lease: lease_expires_at is far in the future
        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "worker_id": "worker-1", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": int(time.time()) + 120, "network_id": 1, "retry_count": 0}
        ]

        with unittest.mock.patch("core.distributed.rollback_version") as mock_rollback:
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 0)
            mock_rollback.assert_not_called()

    def test_stale_mark_task_failed_from_non_owner(self):
        """Verify stale mark_task_failed from a non-owner is rejected and has no effect."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "worker_id": "worker-2", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": int(time.time()) + 120, "network_id": 1, "retry_count": 0}
        ]

        # worker-1 tries to mark worker-2's task as failed
        res = distributed.mark_task_failed(db, task_id=1, error_msg="Stale failure", worker_id="worker-1")
        self.assertFalse(res)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_LEASED)
        self.assertEqual(cnx.tasks[0]["retry_count"], 0)
        self.assertEqual(cnx.tasks[0]["worker_id"], "worker-2")

    def test_mark_task_failed_on_done_task_no_cascade(self):
        """Verify mark_task_failed on a DONE task with retry_count=2 leaves dependents unchanged."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "worker_id": "", "ref_name": "v3.0",
             "status": distributed.TASK_DONE, "lease_expires_at": 0, "network_id": 1, "retry_count": 2},
            {"task_id": 2, "commit_id": 0, "worker_id": "", "ref_name": "v3.0.1",
             "base_ref_name": "v3.0", "base_commit_id": 10, "status": distributed.TASK_PENDING, "network_id": 1, "retry_count": 0},
        ]

        res = distributed.mark_task_failed(db, task_id=1, error_msg="Late stale failure")
        self.assertFalse(res)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_DONE)
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_PENDING)

    def test_three_level_chain_failure_cascade(self):
        """Verify failure cascades through all downstream dependent levels until fixed point."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "commit_id": 11, "worker_id": "worker-1", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "network_id": 1, "retry_count": 2},
            {"task_id": 2, "commit_id": 12, "worker_id": "", "ref_name": "v3.0.2",
             "base_ref_name": "v3.0.1", "base_commit_id": 11, "status": distributed.TASK_PENDING, "network_id": 1, "retry_count": 0},
            {"task_id": 3, "commit_id": 13, "worker_id": "", "ref_name": "v3.0.3",
             "base_ref_name": "v3.0.2", "base_commit_id": 12, "status": distributed.TASK_PENDING, "network_id": 1, "retry_count": 0},
        ]

        res = distributed.mark_task_failed(db, task_id=1, error_msg="Unrecoverable syntax bug", worker_id="worker-1")
        self.assertTrue(res)
        self.assertEqual(cnx.tasks[0]["status"], distributed.TASK_FAILED)
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_FAILED)
        self.assertEqual(cnx.tasks[2]["status"], distributed.TASK_FAILED)

    def test_is_parsing_finished_evaluates_all_pending(self):
        """Verify is_parsing_finished returns False when base is in progress, unlocks when done, and True only when blocked."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        # 1. Base task is in progress (TASK_LEASED) -> False
        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "ref_name": "v3.0", "status": distributed.TASK_LEASED, "network_id": 1},
            {"task_id": 2, "commit_id": 0, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 10,
             "status": distributed.TASK_PENDING, "network_id": 1},
        ]
        cnx.nodes = []
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))

        # 2. Base task is TASK_DONE but pending task was not unlocked -> unblocks and returns False
        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "ref_name": "v3.0", "status": distributed.TASK_DONE, "network_id": 1},
            {"task_id": 2, "commit_id": 0, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 10,
             "status": distributed.TASK_PENDING, "network_id": 1},
        ]
        self.assertFalse(distributed.is_parsing_finished(db, network_id=1))
        # Task 2 should now be unlocked to ELIGIBLE
        self.assertEqual(cnx.tasks[1]["status"], distributed.TASK_ELIGIBLE)

        # 3. Base task is TASK_FAILED -> cannot be unblocked -> returns True
        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "ref_name": "v3.0", "status": distributed.TASK_FAILED, "network_id": 1},
            {"task_id": 2, "commit_id": 0, "ref_name": "v3.0.1", "base_ref_name": "v3.0", "base_commit_id": 10,
             "status": distributed.TASK_PENDING, "network_id": 1},
        ]
        self.assertTrue(distributed.is_parsing_finished(db, network_id=1))

    def test_get_default_node_id_length_and_tokens(self):
        """Verify node_id does not exceed 128 characters and parses tokens by delimiter."""
        nid = distributed.get_default_node_id("worker", "worker-123")
        # Should not falsely match PID 12 inside worker-123
        self.assertIn(str(os.getpid()), nid)

        # Very long custom identifier is hashed to <= 128 chars
        huge_id = "a" * 200
        safe_nid = distributed.get_default_node_id("worker", huge_id)
        self.assertLessEqual(len(safe_nid), 128)

    def test_reclaim_failed_rollback_marks_task_failed_directly(self):
        """Verify that when rollback_version returns False or raises, reclaim marks the task TASK_FAILED directly."""
        cnx = MockConnection()
        db = MockDBDriver(cnx)

        cnx.tasks = [
            {"task_id": 1, "commit_id": 10, "worker_id": "dead-node", "ref_name": "v3.0.1",
             "status": distributed.TASK_LEASED, "lease_expires_at": 50, "network_id": 1, "retry_count": 0}
        ]

        with unittest.mock.patch("core.distributed.rollback_version", return_value=False):
            reclaimed = distributed.reclaim_expired_leases(db, network_id=1, lease_ttl=60)
            self.assertEqual(reclaimed, 1)
            # Failed rollback must not leave task retryable
            t1 = next(t for t in cnx.tasks if t["task_id"] == 1)
            self.assertEqual(t1["status"], distributed.TASK_FAILED)
            self.assertIn("Rollback failed during reclaim", t1["error_msg"])



class TestLeaseSettings(unittest.TestCase):
    """parser.distributed lease/heartbeat config resolution."""

    def test_defaults_when_missing_or_invalid(self) -> None:
        defaults = (distributed.DEFAULT_LEASE_TTL, distributed.DEFAULT_HEARTBEAT_INTERVAL)
        self.assertEqual(distributed.resolve_lease_settings(None), defaults)
        self.assertEqual(distributed.resolve_lease_settings({}), defaults)
        self.assertEqual(
            distributed.resolve_lease_settings({"lease_ttl_seconds": "x", "heartbeat_seconds": 0}), defaults
        )

    def test_configured_values_and_lease_floor(self) -> None:
        self.assertEqual(distributed.resolve_lease_settings({"lease_ttl_seconds": 300, "heartbeat_seconds": 20}), (300, 20))
        # A lease shorter than two heartbeats is raised so live nodes renew before expiry
        self.assertEqual(distributed.resolve_lease_settings({"lease_ttl_seconds": 30, "heartbeat_seconds": 25}), (50, 25))


class TestMemMaxResolution(unittest.TestCase):
    """parser.mem_max resolution for the memory guard."""

    def test_resolve_mem_max(self) -> None:
        import main

        self.assertEqual(main._resolve_mem_max(48), 48.0)
        self.assertEqual(main._resolve_mem_max(0), 0.0)
        self.assertEqual(main._resolve_mem_max(None), 60.0)
        self.assertEqual(main._resolve_mem_max("bad"), 60.0)
        self.assertEqual(main._resolve_mem_max(-5), 60.0)


if __name__ == "__main__":
    unittest.main()
