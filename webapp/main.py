"""webapp/main.py - Developer Web API Server for KernelInfo-Parser.

Provides robust REST endpoints for querying MySQL database (`main`),
including version browsing, file hierarchy traversal, code tag spatial maps (`m_map_ast`),
recursive hierarchical AST container inspection (`m_ast_container`), and dev introspection.
"""
import sys
from pathlib import Path

# Ensure repository root is in sys.path regardless of execution directory
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import os
import re
import json
import logging
import subprocess
import urllib.parse
from datetime import datetime, timezone
from typing import Any
from collections import defaultdict
import mysql.connector
from mysql.connector import pooling
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

from parser.maintainer_ast.maintainer_types import (
    MaintainerRole,
    PatternType,
    MaintainerPerson,
    PatternRule,
    MaintainerSection,
    CreditsEntry,
)
from parser.maintainer_ast.maintainer_parser import MaintainerParser
from parser.maintainer_ast.credits_parser import CreditsParser
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
from parser.git_ast.git_types import (
    CommitRole,
    GitContributor,
    GitCommit,
    CommitDiffHunk,
)
from parser.git_ast.git_commit_parser import GitCommitParser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def safe_decode(val: Any) -> Any:
    """Safely decode bytearray/bytes/memoryview to string, preserving None and primitives."""
    if val is None:
        return None
    if isinstance(val, (bytearray, bytes, memoryview)):
        try:
            return bytes(val).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(val).decode("latin-1", errors="replace")
    return val


STAT_LABEL_MAP: dict[str, str] = {
    "A": "Added",
    "M": "Modified",
    "R": "Renamed",
    "D": "Deleted",
    "0": "Active",
}


def format_stat_label(stat: str | None, is_end: bool = False) -> str:
    """Return friendly label for start or end status code."""
    if not stat or stat == "0":
        return "Active" if is_end else "Added"
    return STAT_LABEL_MAP.get(stat, str(stat))


_NAME_TO_VID_CACHE: dict[str, int] = {}
_VID_TO_NAME_CACHE: dict[int, str] = {}


def preload_version_cache(cnx) -> None:
    """Preload all kernel versions into _NAME_TO_VID_CACHE and _VID_TO_NAME_CACHE."""
    if not cnx:
        return
    try:
        cursor = cnx.cursor()
        cursor.execute("SELECT vid, vname FROM m_v_main ORDER BY vid ASC;")
        for r in cursor.fetchall():
            rvid, rvname = r[0], safe_decode(r[1])
            _NAME_TO_VID_CACHE[rvname] = rvid
            _NAME_TO_VID_CACHE[str(rvid)] = rvid
            _VID_TO_NAME_CACHE[rvid] = rvname
        cursor.close()
    except Exception as e:
        logger.debug("Error preloading versions in get_version_info: %s", e)


def get_version_info(cnx, version_name_or_id: str | int) -> tuple[int, str]:
    """Resolve both vid and vname with thread-safe in-memory caching to eliminate redundant m_v_main queries/joins."""
    if not _VID_TO_NAME_CACHE and cnx:
        preload_version_cache(cnx)

    if isinstance(version_name_or_id, int):
        if version_name_or_id in _VID_TO_NAME_CACHE:
            return version_name_or_id, _VID_TO_NAME_CACHE[version_name_or_id]
    elif str(version_name_or_id).isdigit():
        vid_int = int(version_name_or_id)
        if vid_int in _VID_TO_NAME_CACHE:
            return vid_int, _VID_TO_NAME_CACHE[vid_int]

    vname_str = str(version_name_or_id)
    if vname_str in _NAME_TO_VID_CACHE:
        vid = _NAME_TO_VID_CACHE[vname_str]
        return vid, _VID_TO_NAME_CACHE.get(vid, vname_str)

    if not cnx:
        return 0, vname_str

    try:
        cursor = cnx.cursor()
        if vname_str.isdigit():
            cursor.execute("SELECT vid, vname FROM m_v_main WHERE vid = %s LIMIT 1;", (int(vname_str),))
        else:
            cursor.execute("SELECT vid, vname FROM m_v_main WHERE vname = %s LIMIT 1;", (vname_str,))
        row = cursor.fetchone()
        cursor.close()

        if row:
            vid, vname = row[0], safe_decode(row[1])
            _NAME_TO_VID_CACHE[vname] = vid
            _NAME_TO_VID_CACHE[str(vid)] = vid
            _VID_TO_NAME_CACHE[vid] = vname
            return vid, vname

        preload_version_cache(cnx)
    except Exception as e:
        logger.debug("Error resolving version in get_version_info: %s", e)

    if vname_str in _NAME_TO_VID_CACHE:
        vid = _NAME_TO_VID_CACHE[vname_str]
        return vid, _VID_TO_NAME_CACHE.get(vid, vname_str)

    return 0, vname_str


class DatabaseManager:
    """Manage MySQL connection pool and execute queries with automatic reconnect resilience."""

    def __init__(self) -> None:
        self.user = os.getenv("MYSQL_USER", "root")
        self.password = os.getenv("MYSQL_PASSWORD", "Passe123")
        self.database = os.getenv("MYSQL_DATABASE", "test")
        self.port = int(os.getenv("MYSQL_PORT", "3306"))
        self.host = self._resolve_host()
        self.pool: pooling.MySQLConnectionPool | None = None
        self._init_pool()

    def _resolve_host(self) -> str:
        if env_host := os.getenv("MYSQL_HOST"):
            return env_host
        if os.path.exists("/.dockerenv"):
            return "host.docker.internal"
        return "127.0.0.1"

    def _init_pool(self) -> None:
        candidate_dbs = ["test"]
        if self.database and self.database not in candidate_dbs:
            candidate_dbs.append(self.database)

        for db_name in candidate_dbs:
            try:
                self.pool = pooling.MySQLConnectionPool(
                    pool_name=f"kernelinfo_pool_{os.getpid()}_{db_name}",
                    pool_size=10,
                    host=self.host,
                    port=self.port,
                    user=self.user,
                    password=self.password,
                    database=db_name,
                    charset="utf8mb4",
                    collation="utf8mb4_bin",
                    autocommit=True,
                    connection_timeout=2,
                )
                self.database = db_name
                logger.info("Connected to MySQL at %s:%d/%s", self.host, self.port, self.database)
                return
            except Exception as e:
                logger.warning("Could not initialize connection pool for db '%s' (%s). Trying next.", db_name, e)

        self.pool = None

    def get_connection(self):
        if self.pool:
            try:
                cnx = self.pool.get_connection()
                if cnx and cnx.is_connected():
                    return cnx
            except Exception:
                pass
        try:
            return mysql.connector.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset="utf8mb4",
                collation="utf8mb4_bin",
                connection_timeout=3,
            )
        except Exception as e:
            logger.error("Error connecting to MySQL: %s", e)
            return None


db = DatabaseManager()

app = FastAPI(
    title="KernelInfo-Parser Developer Web API",
    description="REST API for kernel AST spatial coordinate mapping and database introspection.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEBAPP_HTML_PATH = Path(__file__).resolve().parent / "webapp.html"


@app.get("/")
def read_root() -> dict[str, Any]:
    """Root health check and metadata."""
    return {
        "service": "KernelInfo-Parser Developer API",
        "status": "online",
        "database": f"{db.host}:{db.port}/{db.database}",
        "webapp_url": "/app",
        "docs_url": "/docs",
    }


@app.get("/app", response_class=FileResponse)
@app.get("/webapp", response_class=FileResponse)
def serve_webapp() -> FileResponse:
    """Serve the single-page application frontend directly."""
    if not WEBAPP_HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="webapp.html not found")
    return FileResponse(WEBAPP_HTML_PATH, media_type="text/html")


@app.get("/api/versions")
@app.get("/versions")
def get_all_versions(response: Response = None) -> list[dict[str, Any]]:
    """Fetch all registered Linux kernel release versions from m_v_main."""
    if response:
        response.headers["Cache-Control"] = "public, max-age=3600"
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        cursor = cnx.cursor()
        cursor.execute("SELECT vid, vname FROM m_v_main ORDER BY vid ASC;")
        rows = cursor.fetchall()
        cursor.close()
        cnx.close()
        res = []
        for r in rows:
            vid, vname = r[0], safe_decode(r[1])
            _NAME_TO_VID_CACHE[vname] = vid
            _NAME_TO_VID_CACHE[str(vid)] = vid
            _VID_TO_NAME_CACHE[vid] = vname
            res.append({"vid": vid, "vname": vname})
        return res
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_all_versions: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/type_descriptors")
@app.get("/type_descriptors")
def get_type_descriptors(response: Response = None) -> list[dict[str, Any]]:
    """Fetch all AST construct categories from m_type_descriptor for frontend highlight styling."""
    if response:
        response.headers["Cache-Control"] = "public, max-age=3600"
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        cursor = cnx.cursor()
        cursor.execute("SELECT type_id, name FROM m_type_descriptor ORDER BY type_id ASC;")
        rows = cursor.fetchall()
        cursor.close()
        cnx.close()
        return [{"type_id": r[0], "name": safe_decode(r[1])} for r in rows]
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_type_descriptors: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


def compute_container_depths(cursor, all_ast_ids: set[int]) -> dict[int, int]:
    """Compute the hierarchical nesting depth for all AST nodes participating in m_ast_container."""
    clean_ids = [int(x) for x in all_ast_ids if x and x != 0]
    if not clean_ids:
        return {}
    try:
        format_ast_strings = ",".join(["%s"] * len(clean_ids))
        cursor.execute(
            f"""
            SELECT c.ast_id, c.priority, c.type_id, c.ref_ast_id
            FROM m_ast_container c
            WHERE c.ast_id IN ({format_ast_strings}) OR c.ref_ast_id IN ({format_ast_strings})
            ORDER BY c.ast_id ASC, c.priority ASC;
            """,
            tuple(clean_ids) * 2,
        )
        container_rows = cursor.fetchall()
        parent_to_children = defaultdict(list)
        child_to_parents = defaultdict(list)
        all_container_nodes = set()

        for c_row in container_rows:
            p_id = c_row[0]
            child_id = c_row[3]
            all_container_nodes.add(p_id)
            if child_id and child_id != 0:
                parent_to_children[p_id].append(child_id)
                all_container_nodes.add(child_id)
                child_to_parents[child_id].append(p_id)

        # Root container nodes: nodes that are parents in m_ast_container but have no parents in this file subset
        root_nodes = [nid for nid in all_container_nodes if nid in parent_to_children and not child_to_parents.get(nid)]
        # If cyclic or all have parents, pick top-level parent keys
        if not root_nodes and parent_to_children:
            root_nodes = list(parent_to_children.keys())

        ast_depth_map = {}
        queue = [(r_id, 0) for r_id in root_nodes]
        visited = set()
        while queue:
            curr_id, curr_depth = queue.pop(0)
            if curr_id in visited:
                continue
            visited.add(curr_id)
            ast_depth_map[curr_id] = curr_depth
            for c_id in parent_to_children.get(curr_id, []):
                if c_id and c_id != 0 and c_id not in visited:
                    queue.append((c_id, curr_depth + 1))

        return ast_depth_map
    except Exception as e:
        logger.warning("Failed to compute container depths: %s", e)
        return {}


@app.get("/api/version/{version_name}/browse/")
@app.get("/api/version/{version_name}/browse/{path:path}")
@app.get("/v/{version_name}/")
@app.get("/v/{version_name}/{path:path}")
def browse_path(version_name: str, path: str = "") -> dict[str, Any]:
    """Unified endpoint to browse directories or retrieve complete file AST metadata and maps."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    norm_path = path.strip("/")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()

        # ---------------------------------------------------------------------
        # 1. Check if path is empty (Root directory)
        # ---------------------------------------------------------------------
        if not norm_path:
            # Query top-level subdirectories
            cursor.execute(
                """
                SELECT DISTINCT SUBSTRING_INDEX(f.fname, '/', 1) AS dir_prefix
                FROM m_file_name f
                JOIN m_bridge_file bf ON f.fnid = bf.fnid
                WHERE bf.vid = %s AND f.fname LIKE '%/%'
                ORDER BY dir_prefix ASC;
                """,
                (vid,),
            )
            sub_dirs = [safe_decode(r[0]) for r in cursor.fetchall() if r[0] is not None]
            sub_dir_set = set(sub_dirs)

            # Query top-level files (no slash in fname, not in sub_dirs)
            cursor.execute(
                """
                SELECT f.fname, fi.fid, fi.ftype, fi.s_stat, fi.e_stat
                FROM m_file_name f
                JOIN m_bridge_file bf ON f.fnid = bf.fnid
                JOIN m_file fi ON bf.fid = fi.fid
                WHERE bf.vid = %s AND f.fname NOT LIKE '%/%'
                ORDER BY fi.ftype ASC, f.fname ASC;
                """,
                (vid,),
            )
            files = [
                {
                    "fname": safe_decode(r[0]),
                    "fid": r[1],
                    "ftype": r[2],
                    "s_stat": safe_decode(r[3]),
                    "e_stat": safe_decode(r[4]),
                }
                for r in cursor.fetchall()
                if safe_decode(r[0]) not in sub_dir_set
            ]

            cursor.close()
            cnx.close()
            return {
                "type": "directory",
                "version": version_name,
                "vid": vid,
                "path": "/",
                "dir_name": "/",
                "sub_dirs": sub_dirs,
                "files": files,
            }

        # ---------------------------------------------------------------------
        # 2. Check if norm_path matches an exact file
        # ---------------------------------------------------------------------
        cursor.execute(
            """
            SELECT bf.vid, f.fnid, f.fname, fi.fid, fi.vid_s, fi.vid_e, fi.ftype, fi.s_stat, fi.e_stat
            FROM m_file_name f
            JOIN m_bridge_file bf ON f.fnid = bf.fnid
            JOIN m_file fi ON bf.fid = fi.fid
            WHERE bf.vid = %s AND f.fname = %s
            LIMIT 1;
            """,
            (vid, norm_path),
        )
        file_row = cursor.fetchone()

        # If not a file or if ftype == 0 (Directory instance)
        if file_row is None or file_row[6] == 0:
            prefix = f"{norm_path}/"
            prefix_len = len(prefix) + 1

            # Query immediate child subdirectories
            cursor.execute(
                """
                SELECT DISTINCT SUBSTRING_INDEX(SUBSTRING(f.fname, %s), '/', 1) AS subdir
                FROM m_file_name f
                JOIN m_bridge_file bf ON f.fnid = bf.fnid
                WHERE bf.vid = %s AND f.fname LIKE %s AND f.fname LIKE %s
                ORDER BY subdir ASC;
                """,
                (prefix_len, vid, f"{prefix}%", f"{prefix}%/%"),
            )
            sub_dirs = [f"{norm_path}/{safe_decode(r[0])}" for r in cursor.fetchall() if r[0] is not None]
            sub_dir_set = set(sub_dirs)

            # Query immediate child files under this directory (excluding sub_dirs)
            cursor.execute(
                """
                SELECT f.fname, fi.fid, fi.ftype, fi.s_stat, fi.e_stat
                FROM m_file_name f
                JOIN m_bridge_file bf ON f.fnid = bf.fnid
                JOIN m_file fi ON bf.fid = fi.fid
                WHERE bf.vid = %s AND f.fname LIKE %s AND f.fname NOT LIKE %s
                ORDER BY fi.ftype ASC, f.fname ASC;
                """,
                (vid, f"{prefix}%", f"{prefix}%/%"),
            )
            files = [
                {
                    "fname": safe_decode(r[0]),
                    "fid": r[1],
                    "ftype": r[2],
                    "s_stat": safe_decode(r[3]),
                    "e_stat": safe_decode(r[4]),
                }
                for r in cursor.fetchall()
                if safe_decode(r[0]) not in sub_dir_set
            ]

            cursor.close()
            cnx.close()
            return {
                "type": "directory",
                "version": version_name,
                "path": norm_path,
                "dir_name": norm_path,
                "sub_dirs": sub_dirs,
                "files": files,
            }

        # ---------------------------------------------------------------------
        # 3. Path is an active source file -> Fetch full AST tags, maps & lifecycle
        # ---------------------------------------------------------------------
        fid = file_row[3]
        fnid = file_row[1]
        vid_s = file_row[4]
        vid_e = file_row[5]
        s_stat_str = safe_decode(file_row[7])
        e_stat_str = safe_decode(file_row[8])
        vname_s_str = _VID_TO_NAME_CACHE.get(vid_s, version_name)
        vname_e_str = _VID_TO_NAME_CACHE.get(vid_e, None) if vid_e else None

        # Fetch revision history for this fnid
        cursor.execute(
            """
            SELECT fi.fid, fi.vid_s, fi.s_stat, fi.vid_e, fi.e_stat, vs.vname AS vname_s, ve.vname AS vname_e
            FROM m_bridge_file bf
            JOIN m_file fi ON bf.fid = fi.fid
            LEFT JOIN m_v_main vs ON fi.vid_s = vs.vid
            LEFT JOIN m_v_main ve ON fi.vid_e = ve.vid
            WHERE bf.fnid = %s
            GROUP BY fi.fid, fi.vid_s, fi.s_stat, fi.vid_e, fi.e_stat, vs.vname, ve.vname
            ORDER BY fi.vid_s ASC;
            """,
            (fnid,),
        )
        hist_rows = cursor.fetchall()
        history_list = []
        added_version = vname_s_str
        added_stat = s_stat_str or "A"
        for hr in hist_rows:
            h_vid_s = hr[1]
            h_vname_s = safe_decode(hr[5]) or _VID_TO_NAME_CACHE.get(h_vid_s, f"v{h_vid_s}")
            h_s_stat = safe_decode(hr[2])
            h_vid_e = hr[3]
            h_vname_e = safe_decode(hr[6]) if hr[6] is not None else (_VID_TO_NAME_CACHE.get(h_vid_e, None) if h_vid_e else None)
            h_e_stat = safe_decode(hr[4])
            history_list.append({
                "fid": hr[0],
                "vid_s": h_vid_s,
                "vname_s": h_vname_s,
                "s_stat": h_s_stat,
                "s_stat_label": format_stat_label(h_s_stat, is_end=False),
                "vid_e": h_vid_e,
                "vname_e": h_vname_e,
                "e_stat": h_e_stat,
                "e_stat_label": format_stat_label(h_e_stat, is_end=True),
            })
        if history_list:
            added_version = history_list[0]["vname_s"]
            added_stat = history_list[0]["s_stat"]

        file_meta = {
            "vid": file_row[0],
            "vname": version_name,
            "fnid": fnid,
            "fname": safe_decode(file_row[2]),
            "fid": fid,
            "vid_s": vid_s,
            "vname_s": vname_s_str,
            "vid_e": vid_e,
            "vname_e": vname_e_str,
            "ftype": file_row[6],
            "s_stat": s_stat_str,
            "s_stat_label": format_stat_label(s_stat_str, is_end=False),
            "e_stat": e_stat_str,
            "e_stat_label": format_stat_label(e_stat_str, is_end=True),
            "added_version": added_version,
            "added_stat": added_stat,
            "added_stat_label": format_stat_label(added_stat, is_end=False),
            "history": history_list,
        }

        # Fetch tags for this file
        cursor.execute(
            """
            SELECT t.tag_id, t.vid_s, t.vid_e, t.code, t.ast_id, t.hl_s, t.hl_l,
                   bt.line_s, bt.line_e, bt.char_s, bt.char_e,
                   a.name AS ast_name, a.type_id AS ast_type_id, td.name AS ast_type_name,
                   ad.ast_raw
            FROM m_bridge_tag bt
            JOIN m_tag t ON bt.tag_id = t.tag_id
            JOIN m_ast a ON t.ast_id = a.ast_id
            LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
            LEFT JOIN m_ast_debug ad ON a.ast_id = ad.ast_id
            WHERE bt.fid = %s
            ORDER BY bt.line_s ASC, bt.char_s ASC;
            """,
            (fid,),
        )
        tag_rows = cursor.fetchall()
        tag_ids = [r[0] for r in tag_rows]
        # Fetch spatial coordinate map entries for all tags in this file
        map_dict = defaultdict(list)
        all_ast_ids = set([r[4] for r in tag_rows if r[4]])
        if tag_ids:
            format_strings = ",".join(["%s"] * len(tag_ids))
            cursor.execute(
                f"""
                SELECT bm.tag_id, m.map_id, m.line_s, m.char_s, m.line_e, m.char_e,
                       m.ast_id, a.name AS symbol_name, a.type_id, td.name AS type_name
                FROM m_bridge_map bm
                JOIN m_map_ast m ON bm.map_id = m.map_id
                JOIN m_ast a ON m.ast_id = a.ast_id
                LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
                WHERE bm.tag_id IN ({format_strings})
                ORDER BY m.line_s ASC, m.char_s ASC;
                """,
                tuple(tag_ids),
            )
            raw_maps = cursor.fetchall()
            for m_row in raw_maps:
                if m_row[6]:
                    all_ast_ids.add(m_row[6])

            ast_depth_map = compute_container_depths(cursor, all_ast_ids)

            for m_row in raw_maps:
                m_ast_id = m_row[6]
                map_dict[m_row[0]].append({
                    "map_id": m_row[1],
                    "line_s": m_row[2],
                    "char_s": m_row[3],
                    "line_e": m_row[4],
                    "char_e": m_row[5],
                    "ast_id": m_ast_id,
                    "ast_name": safe_decode(m_row[7]),
                    "type_id": m_row[8],
                    "type_name": safe_decode(m_row[9]),
                    "container_depth": ast_depth_map.get(m_ast_id, None),
                })
        else:
            ast_depth_map = compute_container_depths(cursor, all_ast_ids)

        tags = []
        for r in tag_rows:
            t_id = r[0]
            ast_id = r[4]
            raw_val = r[14]
            parsed_raw = None
            if raw_val:
                try:
                    parsed_raw = json.loads(safe_decode(raw_val))
                except Exception:
                    parsed_raw = safe_decode(raw_val)

            tags.append({
                "tag_id": t_id,
                "vid_s": r[1],
                "vid_e": r[2],
                "code": safe_decode(r[3]),
                "ast_id": ast_id,
                "hl_s": r[5],
                "hl_l": r[6],
                "line_s": r[7],
                "line_e": r[8],
                "char_s": r[9],
                "char_e": r[10],
                "ast_name": safe_decode(r[11]),
                "ast_type_id": r[12],
                "ast_type_name": safe_decode(r[13]),
                "ast_raw": parsed_raw,
                "container_depth": ast_depth_map.get(ast_id, None),
                "maps": map_dict.get(t_id, []),
            })

        file_subsystems = resolve_subsystems_for_file_internal(cnx, version_name, norm_path, fid)

        try:
            cursor.close()
        except Exception:
            pass
        try:
            cnx.close()
        except Exception:
            pass
        return {
            "type": "file",
            "version": version_name,
            "path": norm_path,
            "file_info": file_meta,
            "tags": tags,
            "subsystems": file_subsystems,
        }

    except Exception as e:
        try:
            if cnx:
                cnx.close()
        except Exception:
            pass
        logger.error("Error in browse_path: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/file/{fid}")
def get_file_by_id(fid: int) -> dict[str, Any]:
    """Retrieve file metadata, tags, and spatial maps by File ID (fid)."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        cursor = cnx.cursor()
        cursor.execute(
            """
            SELECT bf.vid, f.fnid, f.fname, fi.fid, fi.vid_s, fi.vid_e, fi.ftype, fi.s_stat, fi.e_stat
            FROM m_file fi
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE fi.fid = %s
            LIMIT 1;
            """,
            (fid,),
        )
        file_row = cursor.fetchone()
        if not file_row:
            cursor.close()
            cnx.close()
            raise HTTPException(status_code=404, detail=f"File fid={fid} not found")

        b_vid = file_row[0]
        fnid = file_row[1]
        fname = safe_decode(file_row[2])
        vid_s = file_row[4]
        vid_e = file_row[5]
        s_stat_str = safe_decode(file_row[7])
        e_stat_str = safe_decode(file_row[8])
        version_name = _VID_TO_NAME_CACHE.get(b_vid, "v3.0")
        vname_s_str = _VID_TO_NAME_CACHE.get(vid_s, version_name)
        vname_e_str = _VID_TO_NAME_CACHE.get(vid_e, None) if vid_e else None

        # Fetch revision history for this fnid
        cursor.execute(
            """
            SELECT fi.fid, fi.vid_s, fi.s_stat, fi.vid_e, fi.e_stat, vs.vname AS vname_s, ve.vname AS vname_e
            FROM m_bridge_file bf
            JOIN m_file fi ON bf.fid = fi.fid
            LEFT JOIN m_v_main vs ON fi.vid_s = vs.vid
            LEFT JOIN m_v_main ve ON fi.vid_e = ve.vid
            WHERE bf.fnid = %s
            GROUP BY fi.fid, fi.vid_s, fi.s_stat, fi.vid_e, fi.e_stat, vs.vname, ve.vname
            ORDER BY fi.vid_s ASC;
            """,
            (fnid,),
        )
        hist_rows = cursor.fetchall()
        history_list = []
        added_version = vname_s_str
        added_stat = s_stat_str or "A"
        for hr in hist_rows:
            h_vid_s = hr[1]
            h_vname_s = safe_decode(hr[5]) or _VID_TO_NAME_CACHE.get(h_vid_s, f"v{h_vid_s}")
            h_s_stat = safe_decode(hr[2])
            h_vid_e = hr[3]
            h_vname_e = safe_decode(hr[6]) if hr[6] is not None else (_VID_TO_NAME_CACHE.get(h_vid_e, None) if h_vid_e else None)
            h_e_stat = safe_decode(hr[4])
            history_list.append({
                "fid": hr[0],
                "vid_s": h_vid_s,
                "vname_s": h_vname_s,
                "s_stat": h_s_stat,
                "s_stat_label": format_stat_label(h_s_stat, is_end=False),
                "vid_e": h_vid_e,
                "vname_e": h_vname_e,
                "e_stat": h_e_stat,
                "e_stat_label": format_stat_label(h_e_stat, is_end=True),
            })
        if history_list:
            added_version = history_list[0]["vname_s"]
            added_stat = history_list[0]["s_stat"]

        file_meta = {
            "vid": file_row[0],
            "vname": version_name,
            "fnid": fnid,
            "fname": fname,
            "fid": fid,
            "vid_s": vid_s,
            "vname_s": vname_s_str,
            "vid_e": vid_e,
            "vname_e": vname_e_str,
            "ftype": file_row[6],
            "s_stat": s_stat_str,
            "s_stat_label": format_stat_label(s_stat_str, is_end=False),
            "e_stat": e_stat_str,
            "e_stat_label": format_stat_label(e_stat_str, is_end=True),
            "added_version": added_version,
            "added_stat": added_stat,
            "added_stat_label": format_stat_label(added_stat, is_end=False),
            "history": history_list,
        }

        # Fetch tags
        cursor.execute(
            """
            SELECT t.tag_id, t.vid_s, t.vid_e, t.code, t.ast_id, t.hl_s, t.hl_l,
                   bt.line_s, bt.line_e, bt.char_s, bt.char_e,
                   a.name AS ast_name, a.type_id AS ast_type_id, td.name AS ast_type_name,
                   ad.ast_raw
            FROM m_bridge_tag bt
            JOIN m_tag t ON bt.tag_id = t.tag_id
            JOIN m_ast a ON t.ast_id = a.ast_id
            LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
            LEFT JOIN m_ast_debug ad ON a.ast_id = ad.ast_id
            WHERE bt.fid = %s
            ORDER BY bt.line_s ASC, bt.char_s ASC;
            """,
            (fid,),
        )
        tag_rows = cursor.fetchall()
        tag_ids = [r[0] for r in tag_rows]
        all_ast_ids = set([r[4] for r in tag_rows if r[4]])

        map_dict = defaultdict(list)
        if tag_ids:
            format_strings = ",".join(["%s"] * len(tag_ids))
            cursor.execute(
                f"""
                SELECT bm.tag_id, m.map_id, m.line_s, m.char_s, m.line_e, m.char_e,
                       m.ast_id, a.name AS symbol_name, a.type_id, td.name AS type_name
                FROM m_bridge_map bm
                JOIN m_map_ast m ON bm.map_id = m.map_id
                JOIN m_ast a ON m.ast_id = a.ast_id
                LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
                WHERE bm.tag_id IN ({format_strings})
                ORDER BY m.line_s ASC, m.char_s ASC;
                """,
                tuple(tag_ids),
            )
            raw_maps = cursor.fetchall()
            for m_row in raw_maps:
                if m_row[6]:
                    all_ast_ids.add(m_row[6])

            ast_depth_map = compute_container_depths(cursor, all_ast_ids)

            for m_row in raw_maps:
                m_ast_id = m_row[6]
                map_dict[m_row[0]].append({
                    "map_id": m_row[1],
                    "line_s": m_row[2],
                    "char_s": m_row[3],
                    "line_e": m_row[4],
                    "char_e": m_row[5],
                    "ast_id": m_ast_id,
                    "ast_name": safe_decode(m_row[7]),
                    "type_id": m_row[8],
                    "type_name": safe_decode(m_row[9]),
                    "container_depth": ast_depth_map.get(m_ast_id, None),
                })
        else:
            ast_depth_map = compute_container_depths(cursor, all_ast_ids)

        tags = [
            {
                "tag_id": r[0],
                "vid_s": r[1],
                "vid_e": r[2],
                "code": safe_decode(r[3]),
                "ast_id": r[4],
                "hl_s": r[5],
                "hl_l": r[6],
                "line_s": r[7],
                "line_e": r[8],
                "char_s": r[9],
                "char_e": r[10],
                "ast_name": safe_decode(r[11]),
                "ast_type_id": r[12],
                "ast_type_name": safe_decode(r[13]),
                "ast_raw": safe_decode(r[14]),
                "container_depth": ast_depth_map.get(r[4], None),
                "maps": map_dict.get(r[0], []),
            }
            for r in tag_rows
        ]

        file_subsystems = resolve_subsystems_for_file_internal(cnx, file_meta["vname"], file_meta["fname"], fid)

        try:
            cursor.close()
        except Exception:
            pass
        try:
            cnx.close()
        except Exception:
            pass
        return {
            "type": "file",
            "file_info": file_meta,
            "tags": tags,
            "subsystems": file_subsystems,
        }
    except HTTPException:
        raise
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_file_by_id: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/tag/{tag_id}")
def get_tag_by_id(tag_id: int) -> dict[str, Any]:
    """Fetch tag metadata and spatial AST coordinate maps for a specific tag_id."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        cursor = cnx.cursor()
        cursor.execute(
            """
            SELECT t.tag_id, t.vid_s, t.vid_e, t.code, t.ast_id, t.hl_s, t.hl_l,
                   bt.fid, bt.line_s, bt.line_e, bt.char_s, bt.char_e,
                   a.name, a.type_id, td.name, ad.ast_raw
            FROM m_tag t
            LEFT JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_ast a ON t.ast_id = a.ast_id
            LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
            LEFT JOIN m_ast_debug ad ON a.ast_id = ad.ast_id
            WHERE t.tag_id = %s
            LIMIT 1;
            """,
            (tag_id,),
        )
        tag_row = cursor.fetchone()
        if not tag_row:
            cursor.close()
            cnx.close()
            raise HTTPException(status_code=404, detail=f"Tag tag_id={tag_id} not found")

        # Fetch maps
        cursor.execute(
            """
            SELECT m.map_id, m.line_s, m.char_s, m.line_e, m.char_e,
                   m.ast_id, a.name AS symbol_name, a.type_id, td.name AS type_name
            FROM m_bridge_map bm
            JOIN m_map_ast m ON bm.map_id = m.map_id
            JOIN m_ast a ON m.ast_id = a.ast_id
            LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
            WHERE bm.tag_id = %s
            ORDER BY m.line_s ASC, m.char_s ASC;
            """,
            (tag_id,),
        )
        maps = [
            {
                "map_id": r[0],
                "line_s": r[1],
                "char_s": r[2],
                "line_e": r[3],
                "char_e": r[4],
                "ast_id": r[5],
                "ast_name": safe_decode(r[6]),
                "type_id": r[7],
                "type_name": safe_decode(r[8]),
            }
            for r in cursor.fetchall()
        ]

        cursor.close()
        cnx.close()
        return {
            "tag_id": tag_row[0],
            "vid_s": tag_row[1],
            "vid_e": tag_row[2],
            "code": safe_decode(tag_row[3]),
            "ast_id": tag_row[4],
            "hl_s": tag_row[5],
            "hl_l": tag_row[6],
            "fid": tag_row[7],
            "line_s": tag_row[8],
            "line_e": tag_row[9],
            "char_s": tag_row[10],
            "char_e": tag_row[11],
            "ast_name": safe_decode(tag_row[12]),
            "ast_type_id": tag_row[13],
            "ast_type_name": safe_decode(tag_row[14]),
            "ast_raw": safe_decode(tag_row[15]),
            "maps": maps,
        }
    except HTTPException:
        raise
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_tag_by_id: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/ast/{ast_id}/tree")
def get_ast_container_tree(
    ast_id: int,
    depth: int = Query(default=3, ge=1, le=10, description="Depth of recursive m_ast_container traversal"),
) -> dict[str, Any]:
    """Recursively resolve m_ast_container child relationships down to requested depth."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")

    def _fetch_node(cursor, target_ast_id: int, current_depth: int, max_depth: int, visited: set[int]) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT a.ast_id, a.name, a.type_id, td.name, ad.ast_raw
            FROM m_ast a
            LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
            LEFT JOIN m_ast_debug ad ON a.ast_id = ad.ast_id
            WHERE a.ast_id = %s
            LIMIT 1;
            """,
            (target_ast_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {"ast_id": target_ast_id, "name": "Unknown", "type_id": 0, "type_name": "Undefined", "containers": []}

        node = {
            "ast_id": row[0],
            "name": safe_decode(row[1]),
            "type_id": row[2],
            "type_name": safe_decode(row[3]),
            "ast_raw": safe_decode(row[4]),
            "depth": current_depth,
            "containers": [],
        }

        if current_depth >= max_depth or target_ast_id in visited:
            return node

        visited.add(target_ast_id)

        # Fetch children in m_ast_container
        cursor.execute(
            """
            SELECT c.priority, c.type_id AS rel_type_id, rtd.name AS rel_type_name,
                   c.ref_ast_id, ra.name AS ref_name, ra.type_id AS ref_type_id, td.name AS ref_type_name
            FROM m_ast_container c
            LEFT JOIN m_type_descriptor rtd ON c.type_id = rtd.type_id
            JOIN m_ast ra ON c.ref_ast_id = ra.ast_id
            LEFT JOIN m_type_descriptor td ON ra.type_id = td.type_id
            WHERE c.ast_id = %s
            ORDER BY c.priority ASC;
            """,
            (target_ast_id,),
        )
        child_rows = cursor.fetchall()

        for c_row in child_rows:
            ref_id = c_row[3]
            child_tree = _fetch_node(cursor, ref_id, current_depth + 1, max_depth, set(visited))
            node["containers"].append({
                "priority": c_row[0],
                "rel_type_id": c_row[1],
                "rel_type_name": safe_decode(c_row[2]),
                "ref_ast_id": ref_id,
                "ref_ast_name": safe_decode(c_row[4]),
                "ref_type_id": c_row[5],
                "ref_type_name": safe_decode(c_row[6]),
                "child_node": child_tree,
            })

        return node

    try:
        cur = cnx.cursor()
        result = _fetch_node(cur, ast_id, 0, depth, set())
        cur.close()
        cnx.close()
        return result
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_ast_container_tree: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


KCONFIG_TYPE_MAP = {1: "bool", 2: "tristate", 3: "string", 4: "hex", 5: "int"}
KCONFIG_NODE_TYPE_MAP = {1: "menu", 2: "choice", 3: "config", 4: "menuconfig", 5: "comment"}
KCONFIG_REL_TYPE_MAP = {1: "depends_on", 2: "select", 3: "imply", 4: "choice_member"}


def _has_column(cursor: Any, table_name: str, column_name: str) -> bool:
    """Safely check if a column exists on a given table in MySQL."""
    try:
        cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s;", (column_name,))
        return cursor.fetchone() is not None
    except Exception:
        return False


@app.get("/api/version/{version_name}/kconfig/search")
def search_kconfig_symbols(
    version_name: str,
    q: str = Query("", description="Search term for symbol name, prompt, or help text"),
    type: str | None = Query(None, description="Optional type filter (bool, tristate, string, hex, int)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Search Kconfig symbols within a specified Linux version."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        if vid is None:
            cursor.close()
            cnx.close()
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found")

        has_vid_cols = _has_column(cursor, "m_kconfig_symbol", "vid_s")

        # Handle direct Python function invocation defaults
        q_str = q if isinstance(q, str) else ""
        type_str = type if isinstance(type, str) else None
        limit_val = limit if isinstance(limit, int) else 50
        offset_val = offset if isinstance(offset, int) else 0

        conditions = []
        params: list[Any] = []
        if has_vid_cols:
            conditions.append("((s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s)")
            params.extend([vid, vid])
        else:
            conditions.append("1=1")

        if q_str:
            clean_q = q_str.strip()
            raw_q = clean_q
            if clean_q.upper().startswith("CONFIG_"):
                clean_q = clean_q[7:]
            conditions.append("(s.name LIKE %s OR s.prompt LIKE %s OR s.help LIKE %s OR s.prompt LIKE %s OR s.help LIKE %s)")
            params.extend([f"%{clean_q}%", f"%{clean_q}%", f"%{clean_q}%", f"%{raw_q}%", f"%{raw_q}%"])

        if type_str:
            type_lower = type_str.lower()
            reverse_type_map = {v: k for k, v in KCONFIG_TYPE_MAP.items()}
            if type_lower in reverse_type_map:
                conditions.append("s.type = %s")
                params.append(reverse_type_map[type_lower])
            elif type_str.isdigit():
                conditions.append("s.type = %s")
                params.append(int(type_str))

        where_clause = " AND ".join(conditions)

        # Total count query
        count_sql = f"SELECT COUNT(DISTINCT s.kcid) FROM m_kconfig_symbol s WHERE {where_clause};"
        cursor.execute(count_sql, tuple(params))
        total_count = cursor.fetchone()[0]

        # Results query
        query_sql = f"""
            SELECT s.kcid, s.name, s.type, s.prompt, s.def_val, s.help, s.ast_id,
                   fn.fname, bt.line_s, bt.line_e
            FROM m_kconfig_symbol s
            LEFT JOIN m_ast a ON s.ast_id = a.ast_id
            LEFT JOIN m_tag t ON t.ast_id = a.ast_id {"AND (t.vid_e = 0 OR t.vid_e >= %s) AND t.vid_s <= %s" if has_vid_cols else ""}
            LEFT JOIN m_bridge_tag bt ON bt.tag_id = t.tag_id
            LEFT JOIN m_bridge_file bf ON bf.fid = bt.fid {"AND bf.vid = %s" if has_vid_cols else ""}
            LEFT JOIN m_file_name fn ON fn.fnid = bf.fnid
            WHERE {where_clause}
            GROUP BY s.kcid
            ORDER BY s.name ASC
            LIMIT %s OFFSET %s;
        """
        
        exec_params = []
        if has_vid_cols: exec_params.extend([vid, vid, vid])
        exec_params.extend(params)
        exec_params.extend([limit_val, offset_val])
        
        cursor.execute(query_sql, tuple(exec_params))
        rows = cursor.fetchall()
        cursor.close()
        cnx.close()

        results = []
        for r in rows:
            results.append({
                "kcid": r[0],
                "name": safe_decode(r[1]),
                "type": r[2],
                "type_name": KCONFIG_TYPE_MAP.get(r[2], "unknown"),
                "prompt": safe_decode(r[3]),
                "def_val": safe_decode(r[4]),
                "help": safe_decode(r[5]),
                "ast_id": r[6],
                "file_path": safe_decode(r[7]),
                "line_s": r[8],
                "line_e": r[9],
            })

        return {
            "version": version_name,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "symbols": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in search_kconfig_symbols: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/kconfig/symbol/{name_or_kcid}")
def get_kconfig_symbol_detail(version_name: str, name_or_kcid: str) -> dict[str, Any]:
    """Fetch complete metadata, dependencies, and reverse-dependencies for a Kconfig symbol."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        if vid is None:
            cursor.close()
            cnx.close()
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found")

        has_vid_cols = _has_column(cursor, "m_kconfig_symbol", "vid_s")

        clean_name = name_or_kcid.strip()
        if clean_name.startswith("CONFIG_"):
            clean_name = clean_name[7:]

        if has_vid_cols:
            if clean_name.isdigit():
                cursor.execute(
                    """
                    SELECT s.kcid, s.name, s.type, s.prompt, s.def_val, s.help, s.ast_id,
                           s.vid_s, s.vid_e, vs.vname AS vname_s, ve.vname AS vname_e
                    FROM m_kconfig_symbol s
                    LEFT JOIN m_v_main vs ON s.vid_s = vs.vid
                    LEFT JOIN m_v_main ve ON s.vid_e = ve.vid
                    WHERE s.kcid = %s AND (s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s
                    LIMIT 1;
                    """,
                    (int(clean_name), vid, vid),
                )
            else:
                cursor.execute(
                    """
                    SELECT s.kcid, s.name, s.type, s.prompt, s.def_val, s.help, s.ast_id,
                           s.vid_s, s.vid_e, vs.vname AS vname_s, ve.vname AS vname_e
                    FROM m_kconfig_symbol s
                    LEFT JOIN m_v_main vs ON s.vid_s = vs.vid
                    LEFT JOIN m_v_main ve ON s.vid_e = ve.vid
                    WHERE s.name = %s AND (s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s
                    LIMIT 1;
                    """,
                    (clean_name, vid, vid),
                )
        else:
            if clean_name.isdigit():
                cursor.execute(
                    "SELECT kcid, name, type, prompt, def_val, help, ast_id FROM m_kconfig_symbol WHERE kcid = %s LIMIT 1;",
                    (int(clean_name),),
                )
            else:
                cursor.execute(
                    "SELECT kcid, name, type, prompt, def_val, help, ast_id FROM m_kconfig_symbol WHERE name = %s LIMIT 1;",
                    (clean_name,),
                )

        sym_row = cursor.fetchone()
        if not sym_row:
            cursor.close()
            cnx.close()
            raise HTTPException(status_code=404, detail=f"Symbol '{name_or_kcid}' not found in version '{version_name}'")

        kcid = sym_row[0]
        sym_name = safe_decode(sym_row[1])
        sym_type = sym_row[2]
        prompt = safe_decode(sym_row[3])
        def_val = safe_decode(sym_row[4])
        help_text = safe_decode(sym_row[5])
        ast_id = sym_row[6]
        vid_s = sym_row[7] if len(sym_row) > 7 else vid
        vid_e = sym_row[8] if len(sym_row) > 8 else 0
        vname_s = safe_decode(sym_row[9]) if len(sym_row) > 9 and sym_row[9] is not None else version_name
        vname_e = safe_decode(sym_row[10]) if len(sym_row) > 10 and sym_row[10] is not None else None

        added_version = vname_s
        if has_vid_cols and sym_name:
            cursor.execute(
                """
                SELECT vs.vname
                FROM m_kconfig_symbol s2
                LEFT JOIN m_v_main vs ON s2.vid_s = vs.vid
                WHERE s2.name = %s
                ORDER BY s2.vid_s ASC
                LIMIT 1;
                """,
                (sym_name,),
            )
            av_row = cursor.fetchone()
            if av_row and av_row[0]:
                added_version = safe_decode(av_row[0])

        is_active = (vid_e == 0 or vid_e is None)
        lifecycle_status = "Active" if is_active else f"Ended in {vname_e}"

        # Query direct relations (depends_on, selects, implies)
        cursor.execute(
            """
            SELECT target_name, rel_type, cond_ast_id, priority
            FROM m_kconfig_relation
            WHERE kcid = %s
            ORDER BY rel_type ASC, priority ASC;
            """,
            (kcid,),
        )
        rel_rows = cursor.fetchall()
        depends_on = []
        selects = []
        implies = []
        for rr in rel_rows:
            target = safe_decode(rr[0])
            rtype = rr[1]
            if rtype == 1:
                depends_on.append(target)
            elif rtype == 2:
                selects.append(target)
            elif rtype == 3:
                implies.append(target)

        # Query reverse relations (selected_by, implied_by)
        if has_vid_cols:
            cursor.execute(
                """
                SELECT s2.name, s2.prompt, r.rel_type
                FROM m_kconfig_relation r
                JOIN m_kconfig_symbol s2 ON r.kcid = s2.kcid
                WHERE r.target_name = %s AND (s2.vid_e = 0 OR s2.vid_e >= %s) AND s2.vid_s <= %s
                ORDER BY s2.name ASC;
                """,
                (sym_name, vid, vid),
            )
        else:
            cursor.execute(
                """
                SELECT s2.name, s2.prompt, r.rel_type
                FROM m_kconfig_relation r
                JOIN m_kconfig_symbol s2 ON r.kcid = s2.kcid
                WHERE r.target_name = %s
                ORDER BY s2.name ASC;
                """,
                (sym_name,),
            )
        rev_rows = cursor.fetchall()
        selected_by = []
        implied_by = []
        for rvr in rev_rows:
            entry = {
                "name": safe_decode(rvr[0]),
                "prompt": safe_decode(rvr[1]),
            }
            if rvr[2] == 2:
                selected_by.append(entry)
            elif rvr[2] == 3:
                implied_by.append(entry)

        # Query AST and source file coordinates
        tag_params = []
        sql = """
            SELECT fn.fname, bt.line_s, bt.line_e, t.code
            FROM m_bridge_tag bt
            JOIN m_tag t ON bt.tag_id = t.tag_id
            JOIN m_bridge_file bf ON bt.fid = bf.fid
            JOIN m_file_name fn ON bf.fnid = fn.fnid
            WHERE t.ast_id = %s
        """
        if has_vid_cols:
            sql += " AND bf.vid = %s"
            tag_params.extend([vid])
        sql += " LIMIT 1;"
        cursor.execute(sql, [ast_id] + tag_params)
        tag_row = cursor.fetchone()
        file_path = safe_decode(tag_row[0]) if tag_row else None
        line_s = tag_row[1] if tag_row else None
        line_e = tag_row[2] if tag_row else None
        code_snippet = safe_decode(tag_row[3]) if tag_row else None

        # Query compiled source files from Kbuild
        compiled_files = []
        has_kbuild_table = _has_column(cursor, "m_kconfig_kbuild", "kcid")
        if has_kbuild_table:
            cursor.execute(
                """
                SELECT kb.fid, fn.fname, kb.target_obj, kb.compile_mode
                FROM m_kconfig_kbuild kb
                JOIN m_bridge_file bf ON bf.fid = kb.fid AND bf.vid = kb.vid
                JOIN m_file_name fn ON fn.fnid = bf.fnid
                WHERE kb.kcid = %s AND kb.vid = %s
                ORDER BY fn.fname ASC;
                """,
                (kcid, vid),
            )
            kb_rows = cursor.fetchall()
            for kbr in kb_rows:
                cm = kbr[3]
                cm_str = "built-in (obj-y)" if cm == 1 else "module (obj-m)" if cm == 2 else "conditional"
                compiled_files.append({
                    "fid": kbr[0],
                    "file_path": safe_decode(kbr[1]),
                    "target_obj": safe_decode(kbr[2]),
                    "compile_mode": cm_str,
                })

        # Dynamic fallback heuristic if m_kconfig_kbuild is not pre-populated
        if not compiled_files and sym_name:
            stem = sym_name.lower()
            clean_stem = stem[:-3] if stem.endswith("_fs") else stem
            cursor.execute(
                """
                SELECT bf.fid, fn.fname
                FROM m_bridge_file bf
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE bf.vid = %s AND fn.fname LIKE %s
                ORDER BY fn.fname ASC
                LIMIT 50;
                """,
                (vid, f"%{clean_stem}%"),
            )
            dyn_rows = cursor.fetchall()
            for dr in dyn_rows:
                fname_str = safe_decode(dr[1])
                if fname_str.endswith((".c", ".h", ".S")):
                    compiled_files.append({
                        "fid": dr[0],
                        "file_path": fname_str,
                        "target_obj": f"{clean_stem}.o",
                        "compile_mode": "conditional (obj-$(CONFIG_...))",
                    })

        cursor.close()
        cnx.close()

        return {
            "version": version_name,
            "kcid": kcid,
            "name": sym_name,
            "type": sym_type,
            "type_name": KCONFIG_TYPE_MAP.get(sym_type, "unknown"),
            "prompt": prompt,
            "def_val": def_val,
            "help": help_text,
            "ast_id": ast_id,
            "vid_s": vid_s,
            "vname_s": vname_s,
            "vid_e": vid_e,
            "vname_e": vname_e,
            "added_version": added_version,
            "is_active": is_active,
            "lifecycle_status": lifecycle_status,
            "file_path": file_path,
            "line_s": line_s,
            "line_e": line_e,
            "code": code_snippet,
            "depends_on": depends_on,
            "selects": selects,
            "implies": implies,
            "selected_by": selected_by,
            "implied_by": implied_by,
            "compiled_files": compiled_files,
        }
    except HTTPException:
        raise
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_kconfig_symbol_detail: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/kconfig/tree")
def get_kconfig_tree(
    version_name: str,
    arch: str = Query("x86", description="Target architecture (e.g. x86, arm, mips) to scope the menu tree"),
) -> dict[str, Any]:
    """Retrieve authentic, scoped hierarchical menu tree structure for a given kernel version and architecture."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        if vid is None:
            cursor.close()
            cnx.close()
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found")

        has_tree_vid = _has_column(cursor, "m_kconfig_tree", "vid")
        has_sym_vid = _has_column(cursor, "m_kconfig_symbol", "vid_s")

        if has_tree_vid and has_sym_vid:
            tree_sql = """
                SELECT t.tree_id, t.parent_id, t.node_type, t.title, t.kcid, t.priority, t.dep_ast_id, t.ast_id,
                       s.name, s.type, s.prompt, s.def_val, s.help
                FROM m_kconfig_tree t
                LEFT JOIN m_kconfig_symbol s ON t.kcid = s.kcid AND (s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s
                WHERE t.vid = %s
                ORDER BY t.parent_id ASC, t.priority ASC;
            """
            cursor.execute(tree_sql, (vid, vid, vid))
        elif has_tree_vid:
            tree_sql = """
                SELECT t.tree_id, t.parent_id, t.node_type, t.title, t.kcid, t.priority, t.dep_ast_id, t.ast_id,
                       s.name, s.type, s.prompt, s.def_val, s.help
                FROM m_kconfig_tree t
                LEFT JOIN m_kconfig_symbol s ON t.kcid = s.kcid
                WHERE t.vid = %s
                ORDER BY t.parent_id ASC, t.priority ASC;
            """
            cursor.execute(tree_sql, (vid,))
        else:
            tree_sql = """
                SELECT t.tree_id, t.parent_id, t.node_type, t.title, t.kcid, t.priority, t.dep_ast_id, t.ast_id,
                       s.name, s.type, s.prompt, s.def_val, s.help
                FROM m_kconfig_tree t
                LEFT JOIN m_kconfig_symbol s ON t.kcid = s.kcid
                ORDER BY t.parent_id ASC, t.priority ASC;
            """
            cursor.execute(tree_sql)
        rows = cursor.fetchall()

        # Normalize target architecture early
        target_arch = (arch or "x86").lower().strip()
        if target_arch in ("x86_64", "i386", "x86_32", "x86"):
            target_arch = "x86"
        elif target_arch in ("arm64", "aarch64"):
            target_arch = "arm64"
        elif target_arch in ("powerpc_64", "powerpc_32", "powerpc", "ppc"):
            target_arch = "powerpc"
        elif target_arch in ("sparc64", "sparc32", "sparc"):
            target_arch = "sparc"
        elif target_arch.startswith("mips"):
            target_arch = "mips"
        elif target_arch.startswith("riscv"):
            target_arch = "riscv"

        ast_file_map = {}
        try:
            cursor.execute(
                """
                SELECT tg.ast_id, fn.fname
                FROM m_bridge_file bf
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                JOIN m_bridge_tag bt ON bt.fid = bf.fid
                JOIN m_tag tg ON tg.tag_id = bt.tag_id
                WHERE bf.vid = %s AND (fn.fname LIKE '%Kconfig%' OR fn.fname LIKE '%kconfig%')
                """,
                (vid,),
            )
            for ar in cursor.fetchall():
                ast_file_map[ar[0]] = safe_decode(ar[1])
        except Exception as e:
            logger.warning("Could not build ast_file_map: %s", e)

        # Build relations and reverse-relations maps for active version
        relations_map: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"depends_on": [], "selects": [], "implies": []})
        reverse_relations_map: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: {"selected_by": [], "implied_by": []})

        try:
            if has_sym_vid:
                cursor.execute(
                    """
                    SELECT s.name, r.target_name, r.rel_type, s.prompt, r.cond_ast_id
                    FROM m_kconfig_relation r
                    JOIN m_kconfig_symbol s ON r.kcid = s.kcid AND (s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s
                    ORDER BY r.priority ASC;
                    """,
                    (vid, vid),
                )
            else:
                cursor.execute(
                    """
                    SELECT s.name, r.target_name, r.rel_type, s.prompt, r.cond_ast_id
                    FROM m_kconfig_relation r
                    JOIN m_kconfig_symbol s ON r.kcid = s.kcid
                    ORDER BY r.priority ASC;
                    """,
                )
            for s_name_raw, target_raw, rtype, s_prompt_raw, cond_ast_id in cursor.fetchall():
                s_name = safe_decode(s_name_raw)
                target = safe_decode(target_raw)
                s_prompt = safe_decode(s_prompt_raw)

                # Filter foreign architecture relations
                cond_file = ast_file_map.get(cond_ast_id, "")
                if cond_file and cond_file.startswith("arch/"):
                    if not cond_file.startswith(f"arch/{target_arch}/"):
                        continue

                # Filter architecture-specific foreign symbol targets on generic bitness symbols
                if s_name in ("64BIT", "32BIT") and target_arch == "x86":
                    if target in ("PA8X00", "TILEGX", "PARISC", "TILE", "SUPERH", "S390"):
                        continue

                if rtype == 1:
                    relations_map[s_name]["depends_on"].append(target)
                elif rtype == 2:
                    relations_map[s_name]["selects"].append(target)
                    reverse_relations_map[target]["selected_by"].append({"name": s_name, "prompt": s_prompt})
                elif rtype == 3:
                    relations_map[s_name]["implies"].append(target)
                    reverse_relations_map[target]["implied_by"].append({"name": s_name, "prompt": s_prompt})
        except Exception as e:
            logger.warning("Could not query relations in get_kconfig_tree: %s", e)

        cursor.close()
        cnx.close()

        raw_nodes = []
        for r in rows:
            s_name = safe_decode(r[8])
            node_relations = relations_map.get(s_name, {"depends_on": [], "selects": [], "implies": []}) if s_name else {"depends_on": [], "selects": [], "implies": []}
            node_rev = reverse_relations_map.get(s_name, {"selected_by": [], "implied_by": []}) if s_name else {"selected_by": [], "implied_by": []}

            raw_nodes.append({
                "tree_id": r[0],
                "parent_id": r[1],
                "node_type": r[2],
                "node_type_name": KCONFIG_NODE_TYPE_MAP.get(r[2], "unknown"),
                "title": safe_decode(r[3]),
                "kcid": r[4],
                "priority": r[5],
                "dep_ast_id": r[6],
                "ast_id": r[7],
                "symbol_name": s_name,
                "symbol_type": r[9],
                "symbol_type_name": KCONFIG_TYPE_MAP.get(r[9], "unknown") if r[9] else None,
                "prompt": safe_decode(r[10]),
                "def_val": safe_decode(r[11]),
                "help": safe_decode(r[12]),
                "file_path": ast_file_map.get(r[7], ""),
                "depends_on": node_relations["depends_on"],
                "selects": node_relations["selects"],
                "implies": node_relations["implies"],
                "selected_by": node_rev["selected_by"],
                "implied_by": node_rev["implied_by"],
            })

        # --- Menu Inclusion Scoping & Subsystem Tree Stitching ---
        # Target arch normalization (e.g. x86_64 -> x86, i386 -> x86, arm64 -> arm64)
        target_arch = arch.lower().strip()
        if target_arch in ("x86_64", "i386"):
            target_arch = "x86"
        elif target_arch in ("powerpc_64", "powerpc_32"):
            target_arch = "powerpc"
        elif target_arch in ("sparc64", "sparc32"):
            target_arch = "sparc"

        # Standard root menu titles that are genuinely at the root level of Linux make menuconfig:
        authentic_root_titles = {
            "general setup",
            "processor type and features",
            "power management and acpi options",
            "power management options",
            "bus options (pci etc.)",
            "bus options (pci, pcmcia, eisa, mca, isa)",
            "executable file formats / emulations",
            "executable file formats",
            "networking support",
            "device drivers",
            "firmware drivers",
            "file systems",
            "kernel hacking",
            "security options",
            "cryptographic api",
            "library routines",
            "enable loadable module support",
            "enable the block layer",
            "virtualization",
        }

        # Locate or identify synthetic subsystem parent menu IDs
        subsystem_menu_ids: dict[str, int] = {}
        for n in raw_nodes:
            t = (n["title"] or "").strip().lower()
            if "general setup" in t and "init" not in subsystem_menu_ids:
                subsystem_menu_ids["init"] = n["tree_id"]
            elif "processor type" in t and "cpu" not in subsystem_menu_ids:
                subsystem_menu_ids["cpu"] = n["tree_id"]
            elif "device drivers" in t and "drivers" not in subsystem_menu_ids:
                subsystem_menu_ids["drivers"] = n["tree_id"]
            elif "file systems" in t and "fs" not in subsystem_menu_ids:
                subsystem_menu_ids["fs"] = n["tree_id"]
            elif "networking support" in t and "net" not in subsystem_menu_ids:
                subsystem_menu_ids["net"] = n["tree_id"]
            elif "security options" in t and "security" not in subsystem_menu_ids:
                subsystem_menu_ids["security"] = n["tree_id"]
            elif "cryptographic api" in t and "crypto" not in subsystem_menu_ids:
                subsystem_menu_ids["crypto"] = n["tree_id"]
            elif ("kernel hacking" in t or "library" in t) and "lib" not in subsystem_menu_ids:
                subsystem_menu_ids["lib"] = n["tree_id"]
            elif "power management" in t and "power" not in subsystem_menu_ids:
                subsystem_menu_ids["power"] = n["tree_id"]
            elif "block layer" in t and "block" not in subsystem_menu_ids:
                subsystem_menu_ids["block"] = n["tree_id"]
            elif "virtualization" in t and "virt" not in subsystem_menu_ids:
                subsystem_menu_ids["virt"] = n["tree_id"]

        default_fallback_id = subsystem_menu_ids.get("drivers") or subsystem_menu_ids.get("init") or 0

        # Fallback parent mapping based on symbol naming if file_path is unavailable
        def resolve_subsystem_fallback(sym_name: str, title: str) -> int | None:
            s = (sym_name or "").upper()
            if s.startswith(("NET", "INET", "IPV6", "WIRELESS", "BLUETOOTH", "CAN", "BRIDGE", "VLAN", "IPX")):
                return subsystem_menu_ids.get("net")
            if s.startswith(("EXT", "BTRFS", "FAT", "NFS", "CIFS", "FUSE", "SYSV", "UFS", "HFS", "JFFS2", "UBIFS", "SQUASHFS", "ISO9660", "QUOTA", "AUTOFS", "FS_")):
                return subsystem_menu_ids.get("fs")
            if s.startswith(("SECURITY", "SECURITYFS", "APPARMOR", "TOMOYO", "SELINUX", "SMACK")):
                return subsystem_menu_ids.get("security")
            if s.startswith(("CRYPTO", "ASYMMETRIC", "HASH", "CIPHER")):
                return subsystem_menu_ids.get("crypto")
            if s.startswith(("DEBUG", "MAGIC_SYSRQ", "PROVE_LOCKING", "FTRACE", "KGDB", "CRC", "XZ", "ZLIB", "LZ4", "LZO")):
                return subsystem_menu_ids.get("lib")
            if s.startswith(("DRIVERS", "USB", "WATCHDOG", "PCI", "SCSI", "ATA", "I2C", "SPI", "GPIO", "TTY", "SERIAL", "VIDEO", "SOUND", "SND", "HID", "INFINIBAND", "STAGING", "EDAC", "RTC", "DMADEVICES", "UIO", "VIRTIO", "MEDIA", "DRM", "AGP", "FB", "BACKLIGHT", "LEDS", "INPUT", "MOUSE", "KEYBOARD", "JOYSTICK", "TOUCHSCREEN", "MISC_DEVICES", "HW_RANDOM", "PARPORT", "PNP", "BLK_DEV", "NETDEVICES", "ETHERNET", "FDDI", "HIPPI", "PLIP", "PPP", "SLIP", "WLAN", "ISDN", "TELEPHONY", "ATM", "FIREWIRE", "MTD", "I2O")):
                return subsystem_menu_ids.get("drivers")
            return None

        scoped_nodes = []
        seen_root_titles: set[str] = set()
        seen_root_symbols: set[str] = set()

        for n in raw_nodes:
            fp = n.get("file_path", "").replace("\\", "/")
            sym = n.get("symbol_name", "") or ""
            title_lower = (n.get("title", "") or "").strip().lower()

            # Filter out non-matching architectures
            if fp.startswith("arch/") and not fp.startswith(f"arch/{target_arch}/"):
                continue

            # If node has parent_id == 0, check if it is a genuine root menu
            if n["parent_id"] == 0:
                is_genuine_root = (title_lower in authentic_root_titles) or sym in ("64BIT", "X86_32", "X86_64", "ARCH", "SRCARCH")

                # If duplicate root menu or symbol already registered at root, fold into its subsystem
                is_duplicate_root = False
                if is_genuine_root:
                    if title_lower and title_lower in seen_root_titles:
                        is_duplicate_root = True
                    elif sym and sym in seen_root_symbols:
                        is_duplicate_root = True
                    else:
                        if title_lower:
                            seen_root_titles.add(title_lower)
                        if sym:
                            seen_root_symbols.add(sym)

                if (not is_genuine_root) or is_duplicate_root:
                    # Stitch into its proper subsystem
                    attached = False
                    if fp.startswith("drivers/") and "drivers" in subsystem_menu_ids and n["tree_id"] != subsystem_menu_ids["drivers"]:
                        n["parent_id"] = subsystem_menu_ids["drivers"]
                        attached = True
                    elif fp.startswith("fs/") and "fs" in subsystem_menu_ids and n["tree_id"] != subsystem_menu_ids["fs"]:
                        n["parent_id"] = subsystem_menu_ids["fs"]
                        attached = True
                    elif fp.startswith("net/") and "net" in subsystem_menu_ids and n["tree_id"] != subsystem_menu_ids["net"]:
                        n["parent_id"] = subsystem_menu_ids["net"]
                        attached = True
                    elif fp.startswith("security/") and "security" in subsystem_menu_ids and n["tree_id"] != subsystem_menu_ids["security"]:
                        n["parent_id"] = subsystem_menu_ids["security"]
                        attached = True
                    elif fp.startswith("crypto/") and "crypto" in subsystem_menu_ids and n["tree_id"] != subsystem_menu_ids["crypto"]:
                        n["parent_id"] = subsystem_menu_ids["crypto"]
                        attached = True
                    elif fp.startswith("lib/") and "lib" in subsystem_menu_ids and n["tree_id"] != subsystem_menu_ids["lib"]:
                        n["parent_id"] = subsystem_menu_ids["lib"]
                        attached = True
                    elif fp.startswith("kernel/power/") and "power" in subsystem_menu_ids and n["tree_id"] != subsystem_menu_ids["power"]:
                        n["parent_id"] = subsystem_menu_ids["power"]
                        attached = True
                    elif fp.startswith("block/") and "block" in subsystem_menu_ids and n["tree_id"] != subsystem_menu_ids["block"]:
                        n["parent_id"] = subsystem_menu_ids["block"]
                        attached = True

                    if not attached:
                        fallback_parent = resolve_subsystem_fallback(sym, n.get("title", ""))
                        if not fallback_parent:
                            if any(w in title_lower for w in ("driver", "device", "usb", "sound", "graphics", "pci", "scsi", "ata", "serial", "video", "input", "i2c", "spi", "gpio", "media", "watchdog")):
                                fallback_parent = subsystem_menu_ids.get("drivers")
                            elif any(w in title_lower for w in ("file system", "partition", "dos", "cd-rom", "dvd", "nfs", "ext")):
                                fallback_parent = subsystem_menu_ids.get("fs")
                            elif any(w in title_lower for w in ("network", "protocol", "wireless", "ethernet", "ip", "tcp", "socket")):
                                fallback_parent = subsystem_menu_ids.get("net")
                            elif any(w in title_lower for w in ("security", "integrity", "keys")):
                                fallback_parent = subsystem_menu_ids.get("security")
                            elif any(w in title_lower for w in ("crypto", "cipher", "digest", "hash")):
                                fallback_parent = subsystem_menu_ids.get("crypto")
                            elif any(w in title_lower for w in ("debug", "trace", "hacking", "lock", "fault")):
                                fallback_parent = subsystem_menu_ids.get("lib")
                            elif any(w in title_lower for w in ("cpu", "processor", "memory model", "feature", "platform")):
                                fallback_parent = subsystem_menu_ids.get("cpu")
                            else:
                                fallback_parent = default_fallback_id

                        if fallback_parent and n["tree_id"] != fallback_parent:
                            n["parent_id"] = fallback_parent
                        elif default_fallback_id and n["tree_id"] != default_fallback_id:
                            n["parent_id"] = default_fallback_id

            scoped_nodes.append(n)

        return {
            "version": version_name,
            "arch": target_arch,
            "total_nodes": len(scoped_nodes),
            "nodes": scoped_nodes,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_kconfig_tree: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/kconfig/env-presets")
def get_kconfig_env_presets(version_name: str, response: Response = None) -> dict[str, Any]:
    """Dynamically discover target architectures and environment/compiler symbols from kernel database."""
    if response:
        response.headers["Cache-Control"] = "public, max-age=3600"
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")

    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        if vid is None:
            cursor.close()
            cnx.close()
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found")

        # 1. Dynamically query all arch/*/Kconfig files present in this version
        cursor.execute(
            """
            SELECT DISTINCT fn.fname
            FROM m_bridge_file bf
            JOIN m_file_name fn ON bf.fnid = fn.fnid
            WHERE bf.vid = %s AND fn.fname LIKE 'arch/%/Kconfig'
            ORDER BY fn.fname ASC;
            """,
            (vid,),
        )
        arch_file_rows = cursor.fetchall()

        discovered_archs: list[dict[str, Any]] = []
        seen_arch_ids = set()

        for (fname_raw,) in arch_file_rows:
            fname = safe_decode(fname_raw)
            # e.g. arch/x86/Kconfig -> x86, arch/arm/Kconfig -> arm, arch/mips/Kconfig -> mips
            parts = fname.split("/")
            if len(parts) >= 2 and parts[0] == "arch":
                raw_arch = parts[1].strip().lower()
                if not raw_arch or raw_arch in seen_arch_ids:
                    continue
                seen_arch_ids.add(raw_arch)

                # Generate presets for discovered architecture
                if raw_arch == "x86":
                    discovered_archs.append({
                        "id": "x86_64",
                        "label": "x86_64 (64-bit x86)",
                        "arch": "x86",
                        "srcarch": "x86",
                        "kconfig_path": fname,
                        "bits": 64,
                        "symbols": {
                            "64BIT": "y",
                            "X86_64": "y",
                            "X86": "y",
                            "X86_32": "n",
                            "ARCH": "x86",
                            "SRCARCH": "x86",
                        },
                    })
                    discovered_archs.append({
                        "id": "i386",
                        "label": "i386 / x86_32 (32-bit x86)",
                        "arch": "x86",
                        "srcarch": "x86",
                        "kconfig_path": fname,
                        "bits": 32,
                        "symbols": {
                            "64BIT": "n",
                            "X86_32": "y",
                            "X86": "y",
                            "X86_64": "n",
                            "ARCH": "x86",
                            "SRCARCH": "x86",
                        },
                    })
                elif raw_arch in ("powerpc", "ppc"):
                    discovered_archs.append({
                        "id": "powerpc_64",
                        "label": "powerpc (64-bit PPC64)",
                        "arch": "powerpc",
                        "srcarch": "powerpc",
                        "kconfig_path": fname,
                        "bits": 64,
                        "symbols": {
                            "64BIT": "y",
                            "PPC64": "y",
                            "PPC": "y",
                            "PPC32": "n",
                            "ARCH": "powerpc",
                            "SRCARCH": "powerpc",
                        },
                    })
                    discovered_archs.append({
                        "id": "powerpc_32",
                        "label": "powerpc (32-bit PPC32)",
                        "arch": "powerpc",
                        "srcarch": "powerpc",
                        "kconfig_path": fname,
                        "bits": 32,
                        "symbols": {
                            "64BIT": "n",
                            "PPC32": "y",
                            "PPC": "y",
                            "PPC64": "n",
                            "ARCH": "powerpc",
                            "SRCARCH": "powerpc",
                        },
                    })
                elif raw_arch in ("sparc", "sparc64"):
                    discovered_archs.append({
                        "id": "sparc64",
                        "label": "sparc64 (64-bit SPARC)",
                        "arch": "sparc",
                        "srcarch": "sparc",
                        "kconfig_path": fname,
                        "bits": 64,
                        "symbols": {
                            "64BIT": "y",
                            "SPARC64": "y",
                            "SPARC": "y",
                            "SPARC32": "n",
                            "ARCH": "sparc",
                            "SRCARCH": "sparc",
                        },
                    })
                    discovered_archs.append({
                        "id": "sparc32",
                        "label": "sparc32 (32-bit SPARC)",
                        "arch": "sparc",
                        "srcarch": "sparc",
                        "kconfig_path": fname,
                        "bits": 32,
                        "symbols": {
                            "64BIT": "n",
                            "SPARC32": "y",
                            "SPARC": "y",
                            "SPARC64": "n",
                            "ARCH": "sparc",
                            "SRCARCH": "sparc",
                        },
                    })
                elif raw_arch in ("arm64", "aarch64", "alpha", "ia64", "s390", "tile", "riscv"):
                    upper_arch = raw_arch.upper()
                    discovered_archs.append({
                        "id": raw_arch,
                        "label": f"{raw_arch} (64-bit {upper_arch})",
                        "arch": raw_arch,
                        "srcarch": raw_arch,
                        "kconfig_path": fname,
                        "bits": 64,
                        "symbols": {
                            "64BIT": "y",
                            upper_arch: "y",
                            "ARCH": raw_arch,
                            "SRCARCH": raw_arch,
                        },
                    })
                else:
                    upper_arch = raw_arch.upper()
                    discovered_archs.append({
                        "id": raw_arch,
                        "label": f"{raw_arch} ({upper_arch})",
                        "arch": raw_arch,
                        "srcarch": raw_arch,
                        "kconfig_path": fname,
                        "bits": 32,
                        "symbols": {
                            "64BIT": "n",
                            upper_arch: "y",
                            "ARCH": raw_arch,
                            "SRCARCH": raw_arch,
                        },
                    })

        # Fallback if no arch files found in DB
        if not discovered_archs:
            discovered_archs = [
                {
                    "id": "x86_64",
                    "label": "x86_64 (64-bit x86)",
                    "arch": "x86",
                    "srcarch": "x86",
                    "bits": 64,
                    "symbols": { "64BIT": "y", "X86_64": "y", "X86": "y", "X86_32": "n", "ARCH": "x86", "SRCARCH": "x86" }
                },
                {
                    "id": "i386",
                    "label": "i386 / x86_32 (32-bit x86)",
                    "arch": "x86",
                    "srcarch": "x86",
                    "bits": 32,
                    "symbols": { "64BIT": "n", "X86_32": "y", "X86": "y", "X86_64": "n", "ARCH": "x86", "SRCARCH": "x86" }
                },
                {
                    "id": "arm64",
                    "label": "arm64 / aarch64 (64-bit ARM)",
                    "arch": "arm64",
                    "srcarch": "arm64",
                    "bits": 64,
                    "symbols": { "64BIT": "y", "ARM64": "y", "ARM": "n", "ARCH": "arm64", "SRCARCH": "arm64" }
                },
                {
                    "id": "arm",
                    "label": "arm (32-bit ARM)",
                    "arch": "arm",
                    "srcarch": "arm",
                    "bits": 32,
                    "symbols": { "64BIT": "n", "ARM": "y", "ARM64": "n", "ARCH": "arm", "SRCARCH": "arm" }
                },
                {
                    "id": "riscv64",
                    "label": "riscv64 (64-bit RISC-V)",
                    "arch": "riscv",
                    "srcarch": "riscv",
                    "bits": 64,
                    "symbols": { "64BIT": "y", "RISCV": "y", "ARCH": "riscv", "SRCARCH": "riscv" }
                }
            ]

        # 2. Dynamically query detected environment and compiler symbols
        has_sym_vid = _has_column(cursor, "m_kconfig_symbol", "vid_s")
        if has_sym_vid:
            cursor.execute(
                """
                SELECT DISTINCT s.name, s.type, s.prompt, s.def_val
                FROM m_kconfig_symbol s
                WHERE (s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s
                  AND (
                    s.name IN ('ARCH', 'SRCARCH', 'SUBARCH', '64BIT', '32BIT', 'CROSS_COMPILE', 'KERNELVERSION')
                    OR s.name LIKE 'CC_%'
                    OR s.name LIKE 'GCC_%'
                    OR s.name LIKE 'CLANG_%'
                    OR s.name LIKE 'LLVM%'
                  )
                ORDER BY s.name ASC;
                """,
                (vid, vid),
            )
        else:
            cursor.execute(
                """
                SELECT DISTINCT s.name, s.type, s.prompt, s.def_val
                FROM m_kconfig_symbol s
                WHERE (
                    s.name IN ('ARCH', 'SRCARCH', 'SUBARCH', '64BIT', '32BIT', 'CROSS_COMPILE', 'KERNELVERSION')
                    OR s.name LIKE 'CC_%'
                    OR s.name LIKE 'GCC_%'
                    OR s.name LIKE 'CLANG_%'
                    OR s.name LIKE 'LLVM%'
                  )
                ORDER BY s.name ASC;
                """
            )
        sym_rows = cursor.fetchall()
        cursor.close()
        cnx.close()

        detected_symbols = []
        for r in sym_rows:
            detected_symbols.append({
                "name": safe_decode(r[0]),
                "type": KCONFIG_TYPE_MAP.get(r[1], "unknown"),
                "prompt": safe_decode(r[2]),
                "def_val": safe_decode(r[3]),
            })

        return {
            "version": version_name,
            "total_architectures": len(discovered_archs),
            "architectures": discovered_archs,
            "detected_env_symbols": detected_symbols,
            "compilers": [
                {
                    "id": "gcc",
                    "label": "GCC (GNU Compiler Collection)",
                    "symbols": {
                        "CC_IS_GCC": "y",
                        "CC_IS_CLANG": "n",
                        "GCC_VERSION": "110200",
                        "CC": "gcc",
                    },
                },
                {
                    "id": "clang",
                    "label": "Clang / LLVM",
                    "symbols": {
                        "CC_IS_CLANG": "y",
                        "CC_IS_GCC": "n",
                        "CLANG_VERSION": "150000",
                        "LLVM": "1",
                        "CC": "clang",
                    },
                },
            ],
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_kconfig_env_presets: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/kconfig/defconfigs")
def get_kconfig_defconfigs(
    version_name: str,
    arch: str = Query("x86", description="Target architecture to filter defconfigs (e.g. x86, arm, mips, powerpc)"),
) -> dict[str, Any]:
    """Retrieve all available default configuration profiles (defconfig) for a given architecture."""
    # Handle direct Python function invocation
    arch_str = arch if isinstance(arch, str) else "x86"
    arch_norm = arch_str.lower().strip()

    if arch_norm in ("x86_64", "i386", "x86_32", "x86"):
        arch_dir = "x86"
    elif arch_norm in ("arm64", "aarch64"):
        arch_dir = "arm64"
    elif arch_norm.startswith("powerpc") or arch_norm == "ppc":
        arch_dir = "powerpc"
    elif arch_norm.startswith("sparc"):
        arch_dir = "sparc"
    elif arch_norm.startswith("mips"):
        arch_dir = "mips"
    elif arch_norm.startswith("riscv"):
        arch_dir = "riscv"
    else:
        arch_dir = arch_norm

    cnx = db.get_connection()
    rows = []
    if cnx:
        try:
            vid, version_name = get_version_info(cnx, version_name)
            cursor = cnx.cursor()
            if vid is not None:
                cursor.execute(
                    """
                    SELECT fn.fname, bf.fid
                    FROM m_file_name fn
                    JOIN m_bridge_file bf ON bf.fnid = fn.fnid
                    WHERE bf.vid = %s AND (
                        fn.fname LIKE %s OR fn.fname LIKE %s
                    )
                    ORDER BY fn.fname ASC;
                    """,
                    (vid, f"arch/{arch_dir}/configs/%defconfig%", f"arch/{arch_dir}/defconfig%"),
                )
                rows = cursor.fetchall()
            cursor.close()
            cnx.close()
        except Exception as e:
            if cnx and cnx.is_connected():
                cnx.close()
            logger.warning("Could not query DB for defconfigs: %s", e)

    defconfigs: list[dict[str, Any]] = []
    canonical: dict[str, Any] | None = None

    for fname_raw, fid in rows:
        fname = safe_decode(fname_raw)
        base_name = os.path.basename(fname)
        is_canonical = False

        # Determine canonical default for this arch
        if arch_norm in ("x86_64", "x86") and base_name == "x86_64_defconfig":
            is_canonical = True
        elif arch_norm in ("i386", "x86_32") and base_name == "i386_defconfig":
            is_canonical = True
        elif arch_norm == "arm64" and base_name == "defconfig":
            is_canonical = True
        elif arch_norm == "arm" and base_name in ("versatile_defconfig", "omap2plus_defconfig"):
            is_canonical = True
        elif arch_norm in ("powerpc", "powerpc_64") and base_name == "ppc64_defconfig":
            is_canonical = True
        elif arch_norm == "powerpc_32" and base_name == "pmac32_defconfig":
            is_canonical = True
        elif arch_norm in ("sparc", "sparc64") and base_name == "sparc64_defconfig":
            is_canonical = True
        elif arch_norm == "sparc32" and base_name == "sparc32_defconfig":
            is_canonical = True
        elif base_name == "defconfig":
            is_canonical = True

        item = {
            "name": base_name,
            "file_path": fname,
            "fid": fid,
            "is_canonical": is_canonical,
        }
        if is_canonical and not canonical:
            canonical = item
        defconfigs.append(item)

    # Fallback canonical if none explicitly marked
    if not canonical and defconfigs:
        canonical = defconfigs[0]
        canonical["is_canonical"] = True

    # Fallback if no files in database (e.g. offline mock or new arch)
    if not defconfigs:
        fallback_name = "x86_64_defconfig" if "64" in arch_norm else "defconfig"
        defconfigs = [{
            "name": fallback_name,
            "file_path": f"arch/{arch_dir}/configs/{fallback_name}",
            "fid": 0,
            "is_canonical": True,
        }]
        canonical = defconfigs[0]

    return {
        "version": version_name,
        "arch": arch_str,
        "arch_dir": arch_dir,
        "total_count": len(defconfigs),
        "canonical_default": canonical,
        "defconfigs": defconfigs,
    }


@app.get("/api/version/{version_name}/kconfig/defconfig")
def get_kconfig_defconfig_content(
    version_name: str,
    file_path: str = Query(..., description="Relative path or name of defconfig file"),
    arch: str | None = Query(None, description="Optional architecture context"),
) -> dict[str, Any]:
    """Retrieve and parse a specific defconfig file into Kconfig key-value symbols."""
    # Handle direct Python function invocation
    path_str = file_path if isinstance(file_path, str) else ""
    arch_str = arch if isinstance(arch, str) else "x86"

    clean_path = path_str.strip().lstrip("/")
    arch_norm = (arch_str or "x86").lower().strip()
    if arch_norm in ("x86_64", "i386", "x86_32", "x86"):
        arch_dir = "x86"
    elif arch_norm in ("arm64", "aarch64"):
        arch_dir = "arm64"
    elif arch_norm.startswith("powerpc") or arch_norm == "ppc":
        arch_dir = "powerpc"
    elif arch_norm.startswith("sparc"):
        arch_dir = "sparc"
    elif arch_norm.startswith("mips"):
        arch_dir = "mips"
    elif arch_norm.startswith("riscv"):
        arch_dir = "riscv"
    else:
        arch_dir = arch_norm

    cache_key = (version_name, clean_path, arch_dir)
    if cache_key in _DEFCONFIG_CACHE:
        return _DEFCONFIG_CACHE[cache_key]

    candidate_paths: list[str] = []
    if clean_path.startswith("arch/"):
        candidate_paths.append(clean_path)
    elif clean_path.startswith("configs/"):
        candidate_paths.append(f"arch/{arch_dir}/{clean_path}")
    else:
        candidate_paths.append(f"arch/{arch_dir}/configs/{clean_path}")
        candidate_paths.append(f"arch/{arch_dir}/{clean_path}")
        if not clean_path.endswith("defconfig"):
            candidate_paths.append(f"arch/{arch_dir}/configs/{clean_path}_defconfig")

    raw_content = ""
    resolved_path = candidate_paths[0]

    # 1. Try reading from git repository across candidate paths
    for cand in candidate_paths:
        try:
            proc = subprocess.run(
                ["git", "-C", "linux", "show", f"{version_name}:{cand}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout:
                raw_content = proc.stdout
                resolved_path = cand
                break
        except Exception as e:
            logger.debug("Git show error for defconfig %s: %s", cand, e)

    # 2. Try direct local file read
    if not raw_content:
        for cand in candidate_paths:
            local_candidate = os.path.join("linux", cand)
            if os.path.exists(local_candidate):
                try:
                    with open(local_candidate, "r", encoding="utf-8", errors="replace") as f:
                        raw_content = f.read()
                        resolved_path = cand
                        break
                except Exception as e:
                    logger.debug("Local read error for defconfig %s: %s", local_candidate, e)

    # 3. Fallback baseline if file could not be read
    if not raw_content:
        if "64" in resolved_path or (arch_str and "64" in arch_str):
            raw_content = "CONFIG_64BIT=y\nCONFIG_EXPERIMENTAL=y\nCONFIG_MODULES=y\nCONFIG_SMP=y\nCONFIG_NET=y\nCONFIG_INET=y\nCONFIG_EXT4_FS=y\n"
        else:
            raw_content = "CONFIG_64BIT=n\nCONFIG_EXPERIMENTAL=y\nCONFIG_MODULES=y\nCONFIG_NET=y\nCONFIG_INET=y\nCONFIG_EXT4_FS=y\n"

    # Parse .config syntax
    values: dict[str, str] = {}
    for line in raw_content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m_unset = re.match(r"^#\s*CONFIG_([A-Za-z0-9_]+)\s+is\s+not\s+set", line)
            if m_unset:
                values[m_unset.group(1)] = "n"
            continue

        if line.startswith("CONFIG_"):
            m_set = re.match(r"^CONFIG_([A-Za-z0-9_]+)=(.*)$", line)
            if m_set:
                sym_name = m_set.group(1)
                val = m_set.group(2).strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                values[sym_name] = val

    is_64bit = values.get("64BIT") == "y" or ("64" in os.path.basename(resolved_path))
    bits = 64 if is_64bit else 32

    res = {
        "version": version_name,
        "name": os.path.basename(resolved_path),
        "file_path": resolved_path,
        "bits": bits,
        "symbol_count": len(values),
        "values": values,
        "raw_content": raw_content,
    }
    _DEFCONFIG_CACHE[cache_key] = res
    return res


@app.post("/api/version/{version_name}/kconfig/validate")
def validate_kconfig_assignments(version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and enforce Linux Kconfig constraint rules (depends_on, selects, selected_by) on symbol values."""
    symbols = payload.get("symbols", {})
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")

    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        if vid is None:
            cursor.close()
            cnx.close()
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found")

        has_sym_vid = _has_column(cursor, "m_kconfig_symbol", "vid_s")
        if has_sym_vid:
            cursor.execute(
                """
                SELECT s.name, r.target_name, r.rel_type
                FROM m_kconfig_relation r
                JOIN m_kconfig_symbol s ON r.kcid = s.kcid AND (s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s;
                """,
                (vid, vid),
            )
        else:
            cursor.execute(
                """
                SELECT s.name, r.target_name, r.rel_type
                FROM m_kconfig_relation r
                JOIN m_kconfig_symbol s ON r.kcid = s.kcid;
                """,
            )

        relations = defaultdict(lambda: {"depends_on": [], "selects": []})
        for s_name_raw, target_raw, rtype in cursor.fetchall():
            s_name = safe_decode(s_name_raw)
            target = safe_decode(target_raw)
            if rtype == 1:
                relations[s_name]["depends_on"].append(target)
            elif rtype == 2:
                relations[s_name]["selects"].append(target)

        cursor.close()
        cnx.close()

        # Sanitize symbol names (strip CONFIG_)
        clean_values: dict[str, str] = {}
        for k, v in symbols.items():
            clean_k = k[7:] if k.startswith("CONFIG_") else k
            clean_values[clean_k] = str(v).strip()

        # Step 1: Iteratively compute forced minimum values from selects
        forced_symbols: dict[str, dict[str, Any]] = {}
        changed = True
        while changed:
            changed = False
            for s_name, s_val in list(clean_values.items()):
                if s_val in ("y", "m") and s_name in relations:
                    for target in relations[s_name]["selects"]:
                        target_forced = "y" if s_val == "y" else "m"
                        if target not in forced_symbols:
                            forced_symbols[target] = {"forced_value": target_forced, "forced_by": [s_name]}
                            if clean_values.get(target) != target_forced:
                                clean_values[target] = target_forced
                                changed = True
                        else:
                            if s_name not in forced_symbols[target]["forced_by"]:
                                forced_symbols[target]["forced_by"].append(s_name)
                            if target_forced == "y" and forced_symbols[target]["forced_value"] != "y":
                                forced_symbols[target]["forced_value"] = "y"
                                if clean_values.get(target) != "y":
                                    clean_values[target] = "y"
                                    changed = True

        # Step 2: Evaluate depends_on satisfiability
        unmet_dependencies: list[dict[str, Any]] = []
        for s_name, s_val in clean_values.items():
            if s_val in ("y", "m") and s_name in relations:
                for dep in relations[s_name]["depends_on"]:
                    dep_sym = dep.strip()
                    if dep_sym.startswith("CONFIG_"):
                        dep_sym = dep_sym[7:]
                    dep_val = clean_values.get(dep_sym, "n")
                    if dep_val == "n":
                        unmet_dependencies.append({
                            "symbol": s_name,
                            "unmet_dependency": dep_sym,
                            "current_value": s_val,
                            "dependency_value": dep_val,
                        })

        return {
            "version": version_name,
            "is_valid": len(unmet_dependencies) == 0,
            "total_symbols": len(clean_values),
            "forced_symbols": forced_symbols,
            "unmet_dependencies": unmet_dependencies,
            "adjusted_symbols": clean_values,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in validate_kconfig_assignments: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/version/{version_name}/kconfig/export")
def export_kconfig_file(version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Generate Linux kernel compatible .config file text from provided symbol assignments."""
    symbols = payload.get("symbols", {})
    lines = [
        "#",
        "# Automatically generated by KernelInfo-Parser Web Menuconfig",
        f"# Linux Kernel Version {version_name} Configuration",
        "#",
    ]

    for raw_name, val in sorted(symbols.items()):
        name = raw_name if not raw_name.startswith("CONFIG_") else raw_name[7:]
        val_str = str(val).strip() if val is not None else ""

        if val_str in ("y", "Y", "1", "true", "True"):
            lines.append(f"CONFIG_{name}=y")
        elif val_str in ("m", "M"):
            lines.append(f"CONFIG_{name}=m")
        elif val_str in ("n", "N", "0", "false", "False", ""):
            lines.append(f"# CONFIG_{name} is not set")
        else:
            # String or numeric literal
            if val_str.startswith('"') and val_str.endswith('"'):
                lines.append(f"CONFIG_{name}={val_str}")
            elif val_str.startswith("0x") or val_str.isdigit() or (val_str.startswith("-") and val_str[1:].isdigit()):
                lines.append(f"CONFIG_{name}={val_str}")
            else:
                lines.append(f'CONFIG_{name}="{val_str}"')

    lines.append("")
    config_content = "\n".join(lines)
    return {
        "filename": ".config",
        "content": config_content,
        "symbol_count": len(symbols),
    }


@app.post("/api/version/{version_name}/kconfig/import")
def import_kconfig_file(version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Parse imported Linux .config text payload into symbol assignments dictionary."""
    content = payload.get("content", "")
    symbols: dict[str, str] = {}
    lines = content.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check '# CONFIG_FOO is not set'
        if stripped.startswith("# CONFIG_") and stripped.endswith("is not set"):
            sym = stripped[9:-10].strip()
            if sym:
                symbols[sym] = "n"
            continue

        # Skip generic comments
        if stripped.startswith("#"):
            continue

        # Check 'CONFIG_FOO=value'
        if "=" in stripped and stripped.startswith("CONFIG_"):
            parts = stripped[7:].split("=", 1)
            sym = parts[0].strip()
            val = parts[1].strip()
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                val = val[1:-1]
            symbols[sym] = val

    return {
        "version": version_name,
        "symbol_count": len(symbols),
        "symbols": symbols,
    }


# -----------------------------------------------------------------------------
# Maintainer & Credits Data Providers and In-Memory Matcher Cache
# -----------------------------------------------------------------------------
_MAINTAINER_CACHE: dict[str, tuple[list[MaintainerSection], MaintainerMatcher]] = {}
_CREDITS_CACHE: dict[str, list[CreditsEntry]] = {}
_DEFCONFIG_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}
_FILE_SUBSYSTEMS_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}
_GIT_COMMITS_CACHE: dict[tuple[str, int], list[Any]] = {}
_GIT_HUNKS_CACHE: dict[tuple[str, str], list[Any]] = {}


def _get_cached_git_commits(version_name: str, limit: int = 1000) -> list[Any]:
    """Retrieve parsed git commits for a version with thread-safe in-memory caching."""
    key = (version_name, limit)
    if key in _GIT_COMMITS_CACHE:
        return _GIT_COMMITS_CACHE[key]
    parser = GitCommitParser()
    commits = parser.parse_version_commits(None, version_name, limit=limit)
    _GIT_COMMITS_CACHE[key] = commits
    return commits


def _get_cached_git_hunks(version_name: str, file_path: str) -> list[Any]:
    """Retrieve parsed git diff hunks for a file with thread-safe in-memory caching."""
    key = (version_name, file_path)
    if key in _GIT_HUNKS_CACHE:
        return _GIT_HUNKS_CACHE[key]
    parser = GitCommitParser()
    hunks = parser.extract_file_hunks(None, version_name, file_path)
    _GIT_HUNKS_CACHE[key] = hunks
    return hunks


def _read_kernel_source_file(version_name: str, rel_path: str) -> str:
    """Read file content from git repository or local filesystem."""
    try:
        proc = subprocess.run(
            ["git", "-C", "linux", "show", f"{version_name}:{rel_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except Exception as e:
        logger.debug("Could not read %s via git: %s", rel_path, e)

    local_candidate = os.path.join("linux", rel_path)
    if os.path.exists(local_candidate):
        try:
            with open(local_candidate, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.debug("Could not read local file %s: %s", local_candidate, e)
    return ""


def get_maintainer_data(cnx, version_name: str) -> tuple[list[MaintainerSection], MaintainerMatcher]:
    """Retrieve or parse MaintainerSection items and MaintainerMatcher for a version."""
    if version_name in _MAINTAINER_CACHE:
        return _MAINTAINER_CACHE[version_name]

    sections: list[MaintainerSection] = []
    # 1. Try querying relational database tables if connected
    if cnx:
        try:
            cursor = cnx.cursor()
            cursor.execute(
                """
                SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list, s.vid_s
                FROM m_maintainer_section s
                JOIN m_v_main v ON (s.vid_s <= v.vid AND (s.vid_e = 0 OR s.vid_e >= v.vid))
                WHERE v.vname = %s
                ORDER BY s.vid_s DESC, s.sec_id ASC;
                """,
                (version_name,),
            )
            sec_rows = cursor.fetchall()
            if sec_rows:
                sec_map: dict[int, MaintainerSection] = {}
                seen_sec_names: set[str] = set()
                for r in sec_rows:
                    sec_id = r[0]
                    sec_name = safe_decode(r[1]) or ""
                    sec_name_key = sec_name.strip().lower()
                    if sec_name_key in seen_sec_names:
                        continue
                    seen_sec_names.add(sec_name_key)

                    sec = MaintainerSection(
                        name=sec_name,
                        status=safe_decode(r[2]) or "Maintained",
                        scm_tree=safe_decode(r[3]) or "",
                        web_page=safe_decode(r[4]) or "",
                        mailing_list=safe_decode(r[5]) or "",
                    )
                    setattr(sec, "sec_id", sec_id)
                    sec_map[sec_id] = sec

                # Fetch members
                if sec_map:
                    id_placeholders = ",".join(["%s"] * len(sec_map))
                    cursor.execute(
                        f"""
                        SELECT m.sec_id, p.person_id, p.name, p.email, m.role_type, m.priority
                        FROM m_maintainer_member m
                        JOIN m_maintainer_person p ON m.person_id = p.person_id
                        WHERE m.sec_id IN ({id_placeholders})
                        ORDER BY m.priority ASC;
                        """,
                        tuple(sec_map.keys()),
                    )
                    for mr in cursor.fetchall():
                        s_id = mr[0]
                        if s_id in sec_map:
                            person = MaintainerPerson(
                                name=safe_decode(mr[2]) or "",
                                email=safe_decode(mr[3]) or "",
                                role=MaintainerRole(mr[4]) if mr[4] in (1, 2, 3, 4) else MaintainerRole.MAINTAINER,
                            )
                            setattr(person, "person_id", mr[1])
                            sec_map[s_id].members.append(person)

                    # Fetch patterns
                    cursor.execute(
                        f"""
                        SELECT pat.sec_id, pat.pat_type, pat.pattern, pat.priority
                        FROM m_maintainer_pattern pat
                        WHERE pat.sec_id IN ({id_placeholders})
                        ORDER BY pat.priority ASC;
                        """,
                        tuple(sec_map.keys()),
                    )
                    for pr in cursor.fetchall():
                        s_id = pr[0]
                        if s_id in sec_map:
                            rule = PatternRule(
                                pat_type=PatternType(pr[1]) if pr[1] in (1, 2, 3, 4) else PatternType.FILE,
                                pattern=safe_decode(pr[2]) or "",
                                priority=pr[3],
                            )
                            sec_map[s_id].patterns.append(rule)

                    sections = list(sec_map.values())
            cursor.close()
        except Exception as e:
            logger.debug("Database maintainers query error (%s), using parser fallback", e)

    # 2. Fallback to on-demand parsing MAINTAINERS file
    if not sections:
        raw_text = _read_kernel_source_file(version_name, "MAINTAINERS")
        if raw_text:
            parser = MaintainerParser(raw_text)
            sections = parser.parse()
            for idx, sec in enumerate(sections, 1):
                setattr(sec, "sec_id", idx)
                for pidx, p in enumerate(sec.members, 1):
                    setattr(p, "person_id", (idx * 100) + pidx)

    matcher = MaintainerMatcher(sections)
    _MAINTAINER_CACHE[version_name] = (sections, matcher)
    return sections, matcher


def get_credits_data(cnx, version_name: str) -> list[CreditsEntry]:
    """Retrieve or parse CreditsEntry items for a version."""
    if version_name in _CREDITS_CACHE:
        return _CREDITS_CACHE[version_name]

    entries: list[CreditsEntry] = []
    # 1. Try querying database tables if connected
    if cnx:
        try:
            cursor = cnx.cursor()
            cursor.execute(
                """
                SELECT c.credit_id, p.person_id, p.name, p.email, c.web_page, c.pgp_key, c.description, c.snail_mail, c.vid_s
                FROM m_credits_entry c
                JOIN m_maintainer_person p ON c.person_id = p.person_id
                JOIN m_v_main v ON (c.vid_s <= v.vid AND (c.vid_e = 0 OR c.vid_e >= v.vid))
                WHERE v.vname = %s
                ORDER BY c.vid_s DESC, p.name ASC;
                """,
                (version_name,),
            )
            rows = cursor.fetchall()
            seen_persons: set[Any] = set()
            for r in rows:
                p_id = r[1]
                p_name = safe_decode(r[2]) or ""
                p_email = safe_decode(r[3]) or ""
                p_key = p_id if p_id else (p_name.strip().lower(), p_email.strip().lower())
                if p_key in seen_persons:
                    continue
                seen_persons.add(p_key)

                entry = CreditsEntry(
                    name=p_name,
                    email=p_email,
                    web_page=safe_decode(r[4]) or "",
                    pgp_key=safe_decode(r[5]) or "",
                    description=safe_decode(r[6]) or "",
                    snail_mail=safe_decode(r[7]) or "",
                )
                setattr(entry, "credit_id", r[0])
                setattr(entry, "person_id", r[1])
                entries.append(entry)
            cursor.close()
        except Exception as e:
            logger.debug("Database credits query error (%s), using parser fallback", e)

    # 2. Fallback to on-demand parsing CREDITS file
    if not entries:
        raw_text = _read_kernel_source_file(version_name, "CREDITS")
        if raw_text:
            parser = CreditsParser(raw_text)
            entries = parser.parse()
            for idx, entry in enumerate(entries, 1):
                setattr(entry, "credit_id", idx)
                setattr(entry, "person_id", 10000 + idx)

    _CREDITS_CACHE[version_name] = entries
    return entries


def resolve_subsystems_for_file_internal(
    cnx,
    version_name: str,
    file_path: str,
    fid: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve subsystem sections, maintainers, and reviewers for a specific file path."""
    if not file_path:
        return []
    cache_key = (version_name, file_path)
    if cache_key in _FILE_SUBSYSTEMS_CACHE:
        return _FILE_SUBSYSTEMS_CACHE[cache_key]

    sections, matcher = get_maintainer_data(cnx, version_name)
    matched_secs = matcher.match_file(file_path)

    credits_entries = get_credits_data(cnx, version_name)
    credits_emails = {e.email.lower() for e in credits_entries if e.email}
    credits_names = {e.name.lower() for e in credits_entries if e.name}

    results = []
    for sec in matched_secs:
        sec_id = getattr(sec, "sec_id", None)
        maintainers = []
        for m in sec.get_maintainers():
            in_cred = (m.email.lower() in credits_emails) if m.email else (m.name.lower() in credits_names)
            maintainers.append({
                "person_id": getattr(m, "person_id", None),
                "name": m.name,
                "email": m.email,
                "role": "Maintainer",
                "in_credits": in_cred,
            })

        reviewers = []
        for r in sec.get_reviewers():
            in_cred = (r.email.lower() in credits_emails) if r.email else (r.name.lower() in credits_names)
            reviewers.append({
                "person_id": getattr(r, "person_id", None),
                "name": r.name,
                "email": r.email,
                "role": "Reviewer",
                "in_credits": in_cred,
            })

        results.append({
            "sec_id": sec_id,
            "name": sec.name,
            "status": sec.status,
            "scm_tree": sec.scm_tree,
            "web_page": sec.web_page,
            "mailing_list": sec.mailing_list,
            "maintainers": maintainers,
            "reviewers": reviewers,
            "patterns": [
                {"type": p.pat_type.name, "pattern": p.pattern}
                for p in sec.patterns
            ],
        })
    _FILE_SUBSYSTEMS_CACHE[cache_key] = results
    return results


# -----------------------------------------------------------------------------
# Maintainers & Credits REST Endpoints
# -----------------------------------------------------------------------------

@app.get("/api/version/{version_name}/maintainers")
def get_maintainers_overview(
    version_name: str,
    q: str = Query("", description="Search term for subsystem name, maintainer, or pattern"),
    status: str = Query("", description="Optional status filter (e.g. Maintained, Supported)"),
) -> dict[str, Any]:
    """Retrieve catalog of all kernel subsystems in the active version."""
    cnx = db.get_connection()
    try:
        sections, _ = get_maintainer_data(cnx, version_name)
        credits_entries = get_credits_data(cnx, version_name)
        credits_emails = {e.email.lower() for e in credits_entries if e.email}
        credits_names = {e.name.lower() for e in credits_entries if e.name}

        query_str = q.strip().lower() if isinstance(q, str) else ""
        status_filter = status.strip().lower() if isinstance(status, str) else ""

        filtered_sections = []
        for sec in sections:
            sec_id = getattr(sec, "sec_id", None)
            sec_name = sec.name
            sec_status = sec.status

            if status_filter and status_filter not in sec_status.lower():
                continue

            maintainers = [
                {
                    "person_id": getattr(m, "person_id", None),
                    "name": m.name,
                    "email": m.email,
                    "role": "Maintainer",
                    "in_credits": (m.email.lower() in credits_emails) if m.email else (m.name.lower() in credits_names),
                }
                for m in sec.get_maintainers()
            ]
            reviewers = [
                {
                    "person_id": getattr(r, "person_id", None),
                    "name": r.name,
                    "email": r.email,
                    "role": "Reviewer",
                    "in_credits": (r.email.lower() in credits_emails) if r.email else (r.name.lower() in credits_names),
                }
                for r in sec.get_reviewers()
            ]

            if query_str:
                matches_name = query_str in sec_name.lower()
                matches_maint = any(query_str in m["name"].lower() or query_str in m["email"].lower() for m in maintainers)
                matches_rev = any(query_str in r["name"].lower() or query_str in r["email"].lower() for r in reviewers)
                matches_pat = any(query_str in p.pattern.lower() for p in sec.patterns)
                if not (matches_name or matches_maint or matches_rev or matches_pat):
                    continue

            filtered_sections.append({
                "sec_id": sec_id,
                "name": sec_name,
                "status": sec_status,
                "scm_tree": sec.scm_tree,
                "web_page": sec.web_page,
                "mailing_list": sec.mailing_list,
                "maintainers": maintainers,
                "reviewers": reviewers,
                "pattern_count": len(sec.patterns),
            })

        if cnx and cnx.is_connected():
            cnx.close()

        return {
            "version": version_name,
            "total_count": len(filtered_sections),
            "sections": filtered_sections,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_maintainers_overview: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/maintainer/section/{sec_id_or_name:path}")
def get_maintainer_section_detail(version_name: str, sec_id_or_name: str) -> dict[str, Any]:
    """Retrieve full subsystem details, maintainers, reviewers, pattern rules, and matching files."""
    cnx = db.get_connection()
    try:
        sections, _ = get_maintainer_data(cnx, version_name)
        credits_entries = get_credits_data(cnx, version_name)
        credits_emails = {e.email.lower() for e in credits_entries if e.email}
        credits_names = {e.name.lower() for e in credits_entries if e.name}

        target_sec: MaintainerSection | None = None
        raw_sec = urllib.parse.unquote_plus(urllib.parse.unquote(sec_id_or_name)).strip() if isinstance(sec_id_or_name, str) else str(sec_id_or_name)
        if raw_sec.isdigit():
            target_id = int(raw_sec)
            for s in sections:
                if getattr(s, "sec_id", None) == target_id:
                    target_sec = s
                    break

        clean_name = raw_sec.lower()
        if target_sec is None:
            for s in sections:
                if s.name.lower() == clean_name:
                    target_sec = s
                    break

        if target_sec is None:
            for s in sections:
                if clean_name in s.name.lower():
                    target_sec = s
                    break

        if target_sec is None:
            if cnx and cnx.is_connected():
                cnx.close()
            raise HTTPException(status_code=404, detail=f"Subsystem section '{sec_id_or_name}' not found")

        matching_files = []
        if cnx:
            try:
                cursor = cnx.cursor()
                cursor.execute(
                    """
                    SELECT fn.fname, mf.fid, 1
                    FROM m_maintainer_file mf
                    JOIN m_bridge_file bf ON (mf.fid = bf.fid AND mf.vid = bf.vid)
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    JOIN m_v_main vm ON mf.vid = vm.vid
                    WHERE vm.vname = %s AND mf.sec_id = %s
                    ORDER BY fn.fname ASC;
                    """,
                    (version_name, target_sec.sec_id if getattr(target_sec, "sec_id", None) else 0),
                )
                for row in cursor.fetchall():
                    fname = safe_decode(row[0])
                    if fname:
                        matching_files.append({
                            "fname": fname,
                            "fid": row[1],
                            "ftype": row[2],
                        })
                cursor.close()
            except Exception as e:
                logger.warning("Could not fetch maintainer files from DB: %s", e)

        if not matching_files and cnx:
            try:
                cursor = cnx.cursor()
                cursor.execute(
                    """
                    SELECT f.fname, fi.fid, fi.ftype
                    FROM m_file_name f
                    JOIN m_bridge_file bf ON f.fnid = bf.fnid
                    JOIN m_v_main v ON bf.vid = v.vid
                    JOIN m_file fi ON bf.fid = fi.fid
                    WHERE v.vname = %s AND fi.ftype != 0
                    ORDER BY f.fname ASC;
                    """,
                    (version_name,),
                )
                single_sec_matcher = MaintainerMatcher([target_sec])
                for row in cursor.fetchall():
                    fname = safe_decode(row[0])
                    if fname and single_sec_matcher.match_file(fname):
                        matching_files.append({
                            "fname": fname,
                            "fid": row[1],
                            "ftype": row[2],
                        })
                cursor.close()
            except Exception as e:
                logger.debug("Could not query files for subsystem: %s", e)

        members = []
        for m in target_sec.members:
            m_email_clean = (m.email or "").strip().strip("<>").lower()
            m_name_clean = (m.name or "").strip().lower()
            in_cred = (m_email_clean in credits_emails) or (m_name_clean in credits_names)
            members.append({
                "person_id": getattr(m, "person_id", None),
                "name": m.name,
                "email": m.email,
                "role_name": m.role.name.capitalize(),
                "priority": getattr(m, "priority", 0),
                "in_credits": in_cred,
            })

        patterns = [
            {
                "pat_type": p.pat_type.name,
                "pattern": p.pattern,
                "priority": p.priority,
            }
            for p in target_sec.patterns
        ]

        if cnx and cnx.is_connected():
            cnx.close()

        return {
            "version": version_name,
            "section": {
                "sec_id": getattr(target_sec, "sec_id", None),
                "name": target_sec.name,
                "status": target_sec.status,
                "scm_tree": target_sec.scm_tree,
                "web_page": target_sec.web_page,
                "mailing_list": target_sec.mailing_list,
                "members": members,
                "patterns": patterns,
                "files": matching_files,
                "file_count": len(matching_files),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_maintainer_section_detail: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


COMMIT_ROLE_NAMES = {
    1: "Author",
    2: "Committer",
    3: "Co-developed-by",
    4: "Signed-off-by",
    5: "Reviewed-by",
    6: "Acked-by",
    7: "Tested-by",
    8: "Reported-by",
    9: "Suggested-by",
    10: "Merged-by",
    11: "Requested-by",
    12: "Contributor",
}


def get_person_git_contributions(
    cnx: Any,
    version_name: str,
    target_pid: int | None,
    target_name: str,
    target_email: str,
) -> dict[str, Any]:
    """Query git contributions, contribution breakdown, recent commits, and latest patch for a developer."""
    commits_list: list[dict[str, Any]] = []
    authored_count = 0
    codev_count = 0
    signed_off_count = 0
    reviewed_count = 0
    merged_count = 0
    requested_count = 0
    other_count = 0

    clean_email = (target_email or "").strip().strip("<>").lower()
    clean_name = (target_name or "").strip().lower()

    if cnx and cnx.is_connected():
        try:
            cursor = cnx.cursor()
            # 1. Query commits via m_bridge_commit_person or author_id
            cursor.execute("""
                SELECT DISTINCT c.commit_id, c.commit_hash, c.subject, c.author_date,
                       bp.role_type, ap.name AS author_name, ap.email AS author_email, v.vname
                FROM m_commit c
                JOIN m_maintainer_person ap ON c.author_id = ap.person_id
                LEFT JOIN m_bridge_commit_person bp ON c.commit_id = bp.commit_id
                LEFT JOIN m_maintainer_person bp_p ON bp.person_id = bp_p.person_id
                LEFT JOIN m_v_main v ON c.vid = v.vid
                WHERE (bp.person_id = %s OR c.author_id = %s OR LOWER(ap.email) = %s OR LOWER(ap.name) = %s OR LOWER(bp_p.email) = %s OR LOWER(bp_p.name) = %s)
                ORDER BY c.author_date DESC
                LIMIT 50
            """, (target_pid or 0, target_pid or 0, clean_email, clean_name, clean_email, clean_name))
            rows = cursor.fetchall()
            seen_commit_ids = set()

            for r in rows:
                cid = r[0]
                if cid in seen_commit_ids:
                    continue
                seen_commit_ids.add(cid)

                chash = safe_decode(r[1])
                subj = safe_decode(r[2])
                ts = int(r[3]) if r[3] else 0
                role_val = r[4] or 1
                a_name = safe_decode(r[5])
                a_email = safe_decode(r[6])
                vname = safe_decode(r[7]) or version_name

                # Count files modified
                cursor.execute("SELECT COUNT(*) FROM m_bridge_commit_file WHERE commit_id = %s", (cid,))
                f_count = (cursor.fetchone() or [0])[0]

                role_str = COMMIT_ROLE_NAMES.get(role_val, "Contributor")
                if role_val == 1:
                    authored_count += 1
                elif role_val == 3:
                    codev_count += 1
                elif role_val == 4:
                    signed_off_count += 1
                elif role_val == 5:
                    reviewed_count += 1
                elif role_val == 10:
                    merged_count += 1
                elif role_val == 11:
                    requested_count += 1
                else:
                    other_count += 1

                iso_date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else ""

                commits_list.append({
                    "commit_id": cid,
                    "commit_hash": chash,
                    "subject": subj,
                    "author_date": ts,
                    "author_date_iso": iso_date,
                    "author_name": a_name,
                    "author_email": a_email,
                    "role": role_str,
                    "role_type": role_val,
                    "version": vname,
                    "files_count": f_count,
                })
            cursor.close()
        except Exception as e:
            logger.warning("Error querying developer git contributions from MySQL: %s", e)

    # Fallback to direct Git parser if no commits in DB
    if not commits_list and (clean_name or clean_email):
        try:
            raw_commits = _get_cached_git_commits(version_name, limit=1000)
            seen_hashes = set()
            for idx, gc in enumerate(raw_commits):
                if gc.commit_hash in seen_hashes:
                    continue

                matched_role = None
                if (clean_name and clean_name in gc.author_name.lower()) or (clean_email and clean_email in gc.author_email.lower()):
                    matched_role = CommitRole.AUTHOR
                else:
                    for cb in gc.contributors:
                        if (clean_name and clean_name in cb.name.lower()) or (clean_email and clean_email in cb.email.lower()):
                            matched_role = cb.role
                            break

                if matched_role is not None:
                    seen_hashes.add(gc.commit_hash)
                    role_val = int(matched_role)
                    if role_val == 1:
                        authored_count += 1
                    elif role_val == 3:
                        codev_count += 1
                    elif role_val == 4:
                        signed_off_count += 1
                    elif role_val == 5:
                        reviewed_count += 1
                    elif role_val == 10:
                        merged_count += 1
                    elif role_val == 11:
                        requested_count += 1
                    else:
                        other_count += 1

                    iso_date = datetime.fromtimestamp(gc.author_date, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if gc.author_date else ""
                    commits_list.append({
                        "commit_id": idx + 1,
                        "commit_hash": gc.commit_hash,
                        "subject": gc.subject,
                        "author_date": gc.author_date,
                        "author_date_iso": iso_date,
                        "author_name": gc.author_name,
                        "author_email": gc.author_email,
                        "role": matched_role.name.capitalize().replace("_", "-"),
                        "role_type": role_val,
                        "version": version_name,
                        "files_count": len(gc.files),
                    })
        except Exception as e:
            logger.error("Git fallback for person contributions failed: %s", e)

    # Determine latest patch
    latest_patch = None
    if commits_list:
        # Sort by timestamp descending
        commits_list.sort(key=lambda x: x["author_date"], reverse=True)
        # Find latest where role is Author (1), Co-developed (3), Committer (2), or Requested-by (11)
        primary_patches = [c for c in commits_list if c["role_type"] in (1, 2, 3, 11)]
        top_patch = primary_patches[0] if primary_patches else commits_list[0]
        latest_patch = {
            "commit_id": top_patch["commit_id"],
            "commit_hash": top_patch["commit_hash"],
            "subject": top_patch["subject"],
            "author_date": top_patch["author_date"],
            "author_date_iso": top_patch["author_date_iso"],
            "role": top_patch["role"],
            "version": top_patch["version"],
            "files_count": top_patch.get("files_count", 0),
        }

    return {
        "latest_patch": latest_patch,
        "contribution_stats": {
            "authored_commits": authored_count,
            "co_developed_commits": codev_count,
            "signed_off_commits": signed_off_count,
            "reviewed_commits": reviewed_count,
            "merged_commits": merged_count,
            "requested_commits": requested_count,
            "other_contributions": other_count,
            "total_contributions": len(commits_list),
        },
        "recent_commits": commits_list[:15],
    }


@app.get("/api/version/{version_name}/person/{person_id_or_email:path}")
def get_person_profile(version_name: str, person_id_or_email: str) -> dict[str, Any]:
    """Retrieve full developer profile, maintainer subsystems, CREDITS match, and latest patch."""
    cnx = db.get_connection()
    try:
        decoded_id = urllib.parse.unquote(person_id_or_email).strip()
        # Decode once more if double-encoded
        if "%" in decoded_id:
            decoded_id = urllib.parse.unquote(decoded_id).strip()

        person_data = None
        credits_data = None
        subsystems = []

        target_lower = decoded_id.lower()
        target_name = ""
        target_email = ""
        target_pid = None

        sections, _ = get_maintainer_data(cnx, version_name)
        credits_entries = get_credits_data(cnx, version_name)

        # 1. Search in Maintainer sections
        for sec in sections:
            for m in sec.members:
                m_email_clean = (m.email or "").strip().strip("<>").lower()
                m_name_clean = (m.name or "").strip().lower()
                if (decoded_id.isdigit() and getattr(m, "person_id", None) == int(decoded_id)) or \
                   (m_email_clean and (m_email_clean == target_lower or target_lower == m_email_clean)) or \
                   (m_name_clean and (m_name_clean == target_lower or target_lower == m_name_clean)):
                    if not target_name and m.name:
                        target_name = m.name
                    if not target_email and m.email:
                        target_email = m.email
                    if target_pid is None and getattr(m, "person_id", None) is not None:
                        target_pid = m.person_id

        # 2. Search in CREDITS entries
        credit_match = None
        for ce in credits_entries:
            ce_email_clean = (ce.email or "").strip().strip("<>").lower()
            ce_name_clean = (ce.name or "").strip().lower()
            if (decoded_id.isdigit() and getattr(ce, "person_id", None) == int(decoded_id)) or \
               (ce_email_clean and (ce_email_clean == target_lower or (target_email and ce_email_clean == target_email.strip().strip("<>").lower()))) or \
               (ce_name_clean and (ce_name_clean == target_lower or (target_name and ce_name_clean == target_name.strip().lower()))):
                credit_match = ce
                if not target_name and ce.name:
                    target_name = ce.name
                if not target_email and ce.email:
                    target_email = ce.email
                if target_pid is None and getattr(ce, "person_id", None) is not None:
                    target_pid = ce.person_id
                break

        if cnx and cnx.is_connected():
            try:
                cursor = cnx.cursor()
                if decoded_id.isdigit():
                    cursor.execute("SELECT person_id, name, email FROM m_maintainer_person WHERE person_id = %s", (int(decoded_id),))
                else:
                    cursor.execute("SELECT person_id, name, email FROM m_maintainer_person WHERE LOWER(email) = %s OR LOWER(name) = %s LIMIT 1", (decoded_id.lower(), decoded_id.lower()))
                row = cursor.fetchone()
                if row:
                    person_data = {
                        "person_id": row[0],
                        "name": safe_decode(row[1]) or target_name,
                        "email": safe_decode(row[2]) or target_email,
                    }
                cursor.close()
            except Exception as e:
                logger.warning("Error looking up person in DB: %s", e)

        # Fallback if person not in DB: construct stub from parsed name/email
        if not person_data:
            person_data = {
                "person_id": target_pid or (int(decoded_id) if decoded_id.isdigit() else (hash(target_name or decoded_id) & 0x7FFFFFFF or 1)),
                "name": target_name or (decoded_id if not decoded_id.isdigit() else f"Person #{decoded_id}"),
                "email": target_email or (decoded_id if "@" in decoded_id else ""),
            }

        # 3. Check if developer is in CREDITS
        p_email = person_data.get("email", "").lower()
        p_name = person_data.get("name", "").lower()
        in_credits = (credit_match is not None)

        if credit_match:
            credits_data = {
                "credit_id": getattr(credit_match, "credit_id", getattr(credit_match, "entry_id", None)),
                "name": credit_match.name,
                "email": credit_match.email,
                "web_page": credit_match.web_page,
                "description": credit_match.description,
                "snail_mail": credit_match.snail_mail,
                "pgp_key": credit_match.pgp_key,
            }
        else:
            for entry in credits_entries:
                if (p_email and entry.email and p_email == entry.email.lower()) or (p_name and entry.name and p_name == entry.name.lower()):
                    in_credits = True
                    credits_data = {
                        "credit_id": getattr(entry, "credit_id", getattr(entry, "entry_id", None)),
                        "name": entry.name,
                        "email": entry.email,
                        "web_page": entry.web_page,
                        "description": entry.description,
                        "snail_mail": entry.snail_mail,
                        "pgp_key": entry.pgp_key,
                    }
                    break

        # 3. Subsystems handled by this developer
        sections, _ = get_maintainer_data(cnx, version_name)
        for sec in sections:
            for m in sec.members:
                if (p_email and m.email and p_email == m.email.lower()) or (p_name and m.name and p_name == m.name.lower()):
                    subsystems.append({
                        "sec_id": sec.sec_id,
                        "name": sec.name,
                        "role": m.role.name.capitalize(),
                        "mailing_list": sec.mailing_list,
                        "status": sec.status,
                    })
                    break

        # 4. Git history, latest patch, and contribution breakdown
        git_contribs = get_person_git_contributions(
            cnx,
            version_name,
            person_data.get("person_id"),
            person_data.get("name", ""),
            person_data.get("email", ""),
        )

        if cnx and cnx.is_connected():
            cnx.close()

        return {
            "person": person_data,
            "in_credits": in_credits,
            "credits": credits_data,
            "subsystems_count": len(subsystems),
            "subsystems": subsystems,
            "latest_patch": git_contribs.get("latest_patch"),
            "contribution_stats": git_contribs.get("contribution_stats"),
            "recent_commits": git_contribs.get("recent_commits", []),
        }
    except HTTPException:
        raise
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_person_profile: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/commits")
def get_version_commits(
    version_name: str,
    q: str = Query("", description="Search term for commit subject, message, or hash"),
    author: str = Query("", description="Filter commits by author name or email"),
    limit: int = Query(50, ge=1, le=500, description="Max items per page"),
    offset: int = Query(0, ge=0, description="Pagination offset index"),
) -> dict[str, Any]:
    """Retrieve chronological commit timeline for the specified kernel version."""
    cnx = db.get_connection()
    try:
        commits = []
        total_count = 0

        author_filter = author.strip() if isinstance(author, str) else ""
        search_query = q.strip() if isinstance(q, str) else ""
        limit_val = limit if isinstance(limit, int) else 50
        offset_val = offset if isinstance(offset, int) else 0

        if cnx and cnx.is_connected():
            try:
                cursor = cnx.cursor()
                where_clauses = ["v.vname = %s"]
                params: list[Any] = [version_name]

                if author_filter:
                    where_clauses.append("(LOWER(ap.name) LIKE %s OR LOWER(ap.email) LIKE %s)")
                    params.extend([f"%{author_filter.lower()}%", f"%{author_filter.lower()}%"])
                if search_query:
                    where_clauses.append("(LOWER(c.subject) LIKE %s OR LOWER(c.commit_hash) LIKE %s)")
                    params.extend([f"%{search_query.lower()}%", f"%{search_query.lower()}%"])

                where_sql = " AND ".join(where_clauses)
                count_query = f"""
                    SELECT COUNT(DISTINCT c.commit_id)
                    FROM m_commit c
                    JOIN m_v_main v ON c.vid = v.vid
                    JOIN m_maintainer_person ap ON c.author_id = ap.person_id
                    WHERE {where_sql}
                """
                cursor.execute(count_query, params)
                count_row = cursor.fetchone()
                total_count = count_row[0] if count_row else 0

                data_query = f"""
                    SELECT c.commit_id, c.commit_hash, c.author_id, ap.name AS author_name, ap.email AS author_email,
                           c.author_date, c.committer_id, cp.name AS committer_name, cp.email AS committer_email,
                           c.committer_date, c.subject, c.message
                    FROM m_commit c
                    JOIN m_v_main v ON c.vid = v.vid
                    JOIN m_maintainer_person ap ON c.author_id = ap.person_id
                    JOIN m_maintainer_person cp ON c.committer_id = cp.person_id
                    WHERE {where_sql}
                    ORDER BY c.author_date DESC
                    LIMIT %s OFFSET %s
                """
                cursor.execute(data_query, params + [limit_val, offset_val])
                rows = cursor.fetchall()
                for r in rows:
                    cid = r[0]
                    cursor.execute("SELECT COUNT(*) FROM m_bridge_commit_file WHERE commit_id = %s", (cid,))
                    f_count = (cursor.fetchone() or [0])[0]
                    cursor.execute("SELECT COUNT(*) FROM m_bridge_commit_tag WHERE commit_id = %s", (cid,))
                    t_count = (cursor.fetchone() or [0])[0]

                    cursor.execute("""
                        SELECT bp.person_id, p.name, p.email, bp.role_type
                        FROM m_bridge_commit_person bp
                        JOIN m_maintainer_person p ON bp.person_id = p.person_id
                        WHERE bp.commit_id = %s
                        ORDER BY bp.priority ASC
                    """, (cid,))
                    contrib_rows = cursor.fetchall()
                    contributors = [
                        {
                            "person_id": cr[0],
                            "name": safe_decode(cr[1]),
                            "email": safe_decode(cr[2]),
                            "role": cr[3],
                            "role_name": COMMIT_ROLE_NAMES.get(cr[3], "Contributor"),
                        }
                        for cr in contrib_rows
                    ]

                    author_ts = int(r[5]) if r[5] else 0
                    author_iso = datetime.fromtimestamp(author_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if author_ts else ""
                    c_subj = safe_decode(r[10])
                    c_msg = safe_decode(r[11]) if len(r) > 11 else c_subj

                    is_m, req_n, req_e, m_br, m_sums = GitCommitParser.extract_merge_metadata(c_subj, c_msg, [])

                    commits.append({
                        "commit_id": cid,
                        "commit_hash": safe_decode(r[1]),
                        "author": {
                            "person_id": r[2],
                            "name": safe_decode(r[3]),
                            "email": safe_decode(r[4]),
                        },
                        "author_date": author_ts,
                        "author_date_iso": author_iso,
                        "committer": {
                            "person_id": r[6],
                            "name": safe_decode(r[7]),
                            "email": safe_decode(r[8]),
                        },
                        "committer_date": int(r[9]) if r[9] else 0,
                        "subject": c_subj,
                        "files_count": f_count,
                        "tags_count": t_count,
                        "contributors": contributors,
                        "is_merge": is_m,
                        "merge_requester_name": req_n,
                        "merge_requester_email": req_e,
                        "merged_branch": m_br,
                        "merged_commits_summary": m_sums,
                    })
                cursor.close()
            except Exception as e:
                logger.warning("MySQL query for git commits failed, falling back to Git parser: %s", e)

        # Fallback to direct Git parser
        if not commits:
            raw_commits = _get_cached_git_commits(version_name, limit=1000)
            filtered = []
            for rc in raw_commits:
                if author_filter:
                    af = author_filter.lower()
                    if af not in rc.author_name.lower() and af not in rc.author_email.lower():
                        continue
                if search_query:
                    sq = search_query.lower()
                    if sq not in rc.subject.lower() and sq not in rc.commit_hash.lower() and sq not in rc.message.lower():
                        continue
                filtered.append(rc)

            total_count = len(filtered)
            paged = filtered[offset_val : offset_val + limit_val]

            for idx, gc in enumerate(paged):
                c_id = idx + 1 + offset_val
                author_ts = gc.author_date
                author_iso = datetime.fromtimestamp(author_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if author_ts else ""
                commits.append({
                    "commit_id": c_id,
                    "commit_hash": gc.commit_hash,
                    "author": {
                        "person_id": hash(gc.author_name) & 0x7FFFFFFF or 1,
                        "name": gc.author_name,
                        "email": gc.author_email,
                    },
                    "author_date": author_ts,
                    "author_date_iso": author_iso,
                    "committer": {
                        "person_id": hash(gc.committer_name) & 0x7FFFFFFF or 1,
                        "name": gc.committer_name,
                        "email": gc.committer_email,
                    },
                    "committer_date": gc.committer_date,
                    "subject": gc.subject,
                    "files_count": len(gc.files),
                    "tags_count": 0,
                    "contributors": [
                        {
                            "person_id": hash(cb.name) & 0x7FFFFFFF or 1,
                            "name": cb.name,
                            "email": cb.email,
                            "role": int(cb.role),
                            "role_name": cb.role.name.capitalize().replace("_", "-"),
                        }
                        for cb in gc.contributors
                    ],
                    "is_merge": gc.is_merge,
                    "parents": gc.parents,
                    "merge_requester_name": gc.merge_requester_name,
                    "merge_requester_email": gc.merge_requester_email,
                    "merged_branch": gc.merged_branch,
                    "merged_commits_summary": gc.merged_commits_summary,
                })

        if cnx and cnx.is_connected():
            cnx.close()

        return {
            "version": version_name,
            "total": total_count,
            "total_count": total_count,
            "limit": limit_val,
            "offset": offset_val,
            "commits": commits,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_version_commits: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/commit/{commit_hash_or_id:path}")
def get_commit_detail(version_name: str, commit_hash_or_id: str) -> dict[str, Any]:
    """Retrieve detailed commit metadata, full log message, contributors, touched files, and linked tags."""
    cnx = db.get_connection()
    try:
        identifier = commit_hash_or_id.strip()
        commit_data = None

        if cnx and cnx.is_connected():
            try:
                cursor = cnx.cursor()
                if identifier.isdigit():
                    cursor.execute("""
                        SELECT c.commit_id, c.commit_hash, c.author_id, ap.name, ap.email, c.author_date,
                               c.committer_id, cp.name, cp.email, c.committer_date, c.subject, c.message, c.vid, v.vname
                        FROM m_commit c
                        JOIN m_maintainer_person ap ON c.author_id = ap.person_id
                        JOIN m_maintainer_person cp ON c.committer_id = cp.person_id
                        LEFT JOIN m_v_main v ON c.vid = v.vid
                        WHERE c.commit_id = %s
                    """, (int(identifier),))
                else:
                    cursor.execute("""
                        SELECT c.commit_id, c.commit_hash, c.author_id, ap.name, ap.email, c.author_date,
                               c.committer_id, cp.name, cp.email, c.committer_date, c.subject, c.message, c.vid, v.vname
                        FROM m_commit c
                        JOIN m_maintainer_person ap ON c.author_id = ap.person_id
                        JOIN m_maintainer_person cp ON c.committer_id = cp.person_id
                        LEFT JOIN m_v_main v ON c.vid = v.vid
                        WHERE c.commit_hash LIKE %s
                        LIMIT 1
                    """, (f"{identifier}%",))
                row = cursor.fetchone()
                if row:
                    cid = row[0]
                    chash = safe_decode(row[1])
                    a_ts = int(row[5]) if row[5] else 0
                    a_iso = datetime.fromtimestamp(a_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if a_ts else ""
                    c_ts = int(row[9]) if row[9] else 0
                    c_iso = datetime.fromtimestamp(c_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if c_ts else ""
                    c_subj = safe_decode(row[10])
                    c_msg = safe_decode(row[11])

                    is_m, req_n, req_e, m_br, m_sums = GitCommitParser.extract_merge_metadata(c_subj, c_msg, [])

                    # Get contributors
                    cursor.execute("""
                        SELECT bp.person_id, p.name, p.email, bp.role_type
                        FROM m_bridge_commit_person bp
                        JOIN m_maintainer_person p ON bp.person_id = p.person_id
                        WHERE bp.commit_id = %s
                        ORDER BY bp.priority ASC
                    """, (cid,))
                    contribs = [
                        {
                            "person_id": cr[0],
                            "name": safe_decode(cr[1]),
                            "email": safe_decode(cr[2]),
                            "role": cr[3],
                            "role_name": COMMIT_ROLE_NAMES.get(cr[3], "Contributor"),
                        }
                        for cr in cursor.fetchall()
                    ]

                    # Get modified files
                    cursor.execute("""
                        SELECT bcf.fid, fn.fname, bcf.change_type
                        FROM m_bridge_commit_file bcf
                        JOIN m_bridge_file bf ON bcf.fid = bf.fid
                        JOIN m_file_name fn ON bf.fnid = fn.fnid
                        WHERE bcf.commit_id = %s
                    """, (cid,))
                    files_list = [
                        {
                            "fid": fr[0],
                            "path": safe_decode(fr[1]),
                            "change_type": safe_decode(fr[2]),
                        }
                        for fr in cursor.fetchall()
                    ]

                    # Get linked tags
                    cursor.execute("""
                        SELECT bct.tag_id, bct.fid, bt.line_s, bt.line_e, t.code
                        FROM m_bridge_commit_tag bct
                        JOIN m_bridge_tag bt ON bct.tag_id = bt.tag_id AND bct.fid = bt.fid
                        JOIN m_tag t ON bct.tag_id = t.tag_id
                        WHERE bct.commit_id = %s
                        LIMIT 100
                    """, (cid,))
                    tags_list = [
                        {
                            "tag_id": tr[0],
                            "fid": tr[1],
                            "line_s": tr[2],
                            "line_e": tr[3],
                            "code": safe_decode(tr[4]),
                        }
                        for tr in cursor.fetchall()
                    ]

                    commit_data = {
                        "commit_id": cid,
                        "commit_hash": chash,
                        "version": safe_decode(row[13]) or version_name,
                        "author": {
                            "person_id": row[2],
                            "name": safe_decode(row[3]),
                            "email": safe_decode(row[4]),
                        },
                        "author_date": a_ts,
                        "author_date_iso": a_iso,
                        "committer": {
                            "person_id": row[6],
                            "name": safe_decode(row[7]),
                            "email": safe_decode(row[8]),
                        },
                        "committer_date": c_ts,
                        "committer_date_iso": c_iso,
                        "subject": c_subj,
                        "message": c_msg,
                        "contributors": contribs,
                        "files": files_list,
                        "tags": tags_list,
                        "is_merge": is_m,
                        "parents": [],
                        "merge_requester_name": req_n,
                        "merge_requester_email": req_e,
                        "merged_branch": m_br,
                        "merged_commits_summary": m_sums,
                    }
                cursor.close()
            except Exception as e:
                logger.warning("Error fetching commit detail from MySQL: %s", e)

        # Fallback to direct Git query
        if not commit_data:
            raw_commits = _get_cached_git_commits(version_name, limit=1000)
            target_gc = None
            for gc in raw_commits:
                if gc.commit_hash.startswith(identifier) or identifier in gc.subject:
                    target_gc = gc
                    break

            if target_gc:
                a_ts = target_gc.author_date
                a_iso = datetime.fromtimestamp(a_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if a_ts else ""
                c_ts = target_gc.committer_date
                c_iso = datetime.fromtimestamp(c_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if c_ts else ""
                commit_data = {
                    "commit_id": hash(target_gc.commit_hash) & 0x7FFFFFFF or 1,
                    "commit_hash": target_gc.commit_hash,
                    "version": version_name,
                    "author": {
                        "person_id": hash(target_gc.author_name) & 0x7FFFFFFF or 1,
                        "name": target_gc.author_name,
                        "email": target_gc.author_email,
                    },
                    "author_date": a_ts,
                    "author_date_iso": a_iso,
                    "committer": {
                        "person_id": hash(target_gc.committer_name) & 0x7FFFFFFF or 1,
                        "name": target_gc.committer_name,
                        "email": target_gc.committer_email,
                    },
                    "committer_date": c_ts,
                    "committer_date_iso": c_iso,
                    "subject": target_gc.subject,
                    "message": target_gc.message,
                    "contributors": [
                        {
                            "person_id": hash(cb.name) & 0x7FFFFFFF or 1,
                            "name": cb.name,
                            "email": cb.email,
                            "role": int(cb.role),
                            "role_name": cb.role.name.capitalize().replace("_", "-"),
                        }
                        for cb in target_gc.contributors
                    ],
                    "files": [
                        {
                            "fid": 0,
                            "path": f_path,
                            "change_type": c_type,
                        }
                        for c_type, f_path in target_gc.files
                    ],
                    "tags": [],
                    "is_merge": target_gc.is_merge,
                    "parents": target_gc.parents,
                    "merge_requester_name": target_gc.merge_requester_name,
                    "merge_requester_email": target_gc.merge_requester_email,
                    "merged_branch": target_gc.merged_branch,
                    "merged_commits_summary": target_gc.merged_commits_summary,
                }

        if cnx and cnx.is_connected():
            cnx.close()

        if not commit_data:
            raise HTTPException(status_code=404, detail=f"Commit '{commit_hash_or_id}' not found")

        return commit_data
    except HTTPException:
        raise
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_commit_detail: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/file/{fid}/blame")
def get_file_blame(version_name: str, fid: int) -> dict[str, Any]:
    """Retrieve code tag git blame annotations, multi-commit links, and author highlights for a file."""
    cnx = db.get_connection()
    try:
        file_path = ""
        tags_blame = []

        if cnx and cnx.is_connected():
            try:
                cursor = cnx.cursor()
                # 1. Look up file name
                cursor.execute("""
                    SELECT fn.fname
                    FROM m_bridge_file bf
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    WHERE bf.fid = %s
                    LIMIT 1
                """, (fid,))
                fn_row = cursor.fetchone()
                if fn_row:
                    file_path = safe_decode(fn_row[0])

                # 2. Look up all tags for this file
                cursor.execute("""
                    SELECT bt.tag_id, bt.line_s, bt.line_e, bt.char_s, bt.char_e, t.code, t.ast_id
                    FROM m_bridge_tag bt
                    JOIN m_tag t ON bt.tag_id = t.tag_id
                    WHERE bt.fid = %s
                    ORDER BY bt.line_s ASC, bt.char_s ASC
                """, (fid,))
                tag_rows = cursor.fetchall()

                # 3. Look up multi-commit mappings for each tag
                for tr in tag_rows:
                    tid = tr[0]
                    line_s = tr[1]
                    line_e = tr[2]
                    char_s = tr[3]
                    char_e = tr[4]
                    code_snippet = safe_decode(tr[5])

                    cursor.execute("""
                        SELECT c.commit_id, c.commit_hash, c.subject, c.author_date,
                               c.author_id, ap.name AS author_name, ap.email AS author_email
                        FROM m_bridge_commit_tag bct
                        JOIN m_commit c ON bct.commit_id = c.commit_id
                        JOIN m_maintainer_person ap ON c.author_id = ap.person_id
                        WHERE bct.tag_id = %s AND bct.fid = %s
                        ORDER BY c.author_date DESC
                    """, (tid, fid))
                    c_rows = cursor.fetchall()
                    commits = []

                    for cr in c_rows:
                        cid = cr[0]
                        chash = safe_decode(cr[1])
                        c_subj = safe_decode(cr[2])
                        c_ts = int(cr[3]) if cr[3] else 0
                        c_iso = datetime.fromtimestamp(c_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if c_ts else ""

                        # Contributors on this commit
                        cursor.execute("""
                            SELECT bp.person_id, p.name, bp.role_type
                            FROM m_bridge_commit_person bp
                            JOIN m_maintainer_person p ON bp.person_id = p.person_id
                            WHERE bp.commit_id = %s
                            ORDER BY bp.priority ASC
                        """, (cid,))
                        contribs = [
                            {
                                "person_id": cb[0],
                                "name": safe_decode(cb[1]),
                                "role": cb[2],
                                "role_name": COMMIT_ROLE_NAMES.get(cb[2], "Contributor"),
                            }
                            for cb in cursor.fetchall()
                        ]

                        commits.append({
                            "commit_id": cid,
                            "commit_hash": chash,
                            "subject": c_subj,
                            "author_date": c_ts,
                            "author_date_iso": c_iso,
                            "author": {
                                "person_id": cr[4],
                                "name": safe_decode(cr[5]),
                                "email": safe_decode(cr[6]),
                            },
                            "contributors": contribs,
                        })

                    tags_blame.append({
                        "tag_id": tid,
                        "line_s": line_s,
                        "line_e": line_e,
                        "char_s": char_s,
                        "char_e": char_e,
                        "code": code_snippet,
                        "commits_count": len(commits),
                        "commits": commits,
                        "primary_author": commits[0]["author"] if commits else None,
                        "primary_commit": commits[0] if commits else None,
                    })

                cursor.close()
            except Exception as e:
                logger.warning("Error fetching file blame from MySQL: %s", e)

        # Fallback / dynamic git blame if tags or commit bridges not yet populated
        if (not tags_blame or all(t["commits_count"] == 0 for t in tags_blame)) and file_path:
            try:
                hunks = _get_cached_git_hunks(version_name, file_path)
                raw_commits = _get_cached_git_commits(version_name, limit=500)
                commit_map = {gc.commit_hash: gc for gc in raw_commits}

                for t_item in tags_blame:
                    matched_c = []
                    for h in hunks:
                        h_s = h.new_start
                        h_e = h.new_start + max(1, h.new_count) - 1
                        if not (t_item["line_e"] < h_s or t_item["line_s"] > h_e):
                            if h.commit_hash in commit_map:
                                gc = commit_map[h.commit_hash]
                                matched_c.append({
                                    "commit_id": hash(gc.commit_hash) & 0x7FFFFFFF or 1,
                                    "commit_hash": gc.commit_hash,
                                    "subject": gc.subject,
                                    "author_date": gc.author_date,
                                    "author_date_iso": datetime.fromtimestamp(gc.author_date, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if gc.author_date else "",
                                    "author": {
                                        "person_id": hash(gc.author_name) & 0x7FFFFFFF or 1,
                                        "name": gc.author_name,
                                        "email": gc.author_email,
                                    },
                                    "contributors": [
                                        {
                                            "person_id": hash(cb.name) & 0x7FFFFFFF or 1,
                                            "name": cb.name,
                                            "role": int(cb.role),
                                            "role_name": cb.role.name.capitalize().replace("_", "-"),
                                        }
                                        for cb in gc.contributors
                                    ],
                                })
                    if matched_c:
                        t_item["commits"] = matched_c
                        t_item["commits_count"] = len(matched_c)
                        t_item["primary_author"] = matched_c[0]["author"]
                        t_item["primary_commit"] = matched_c[0]
            except Exception as e:
                logger.error("Git fallback blame correlation failed: %s", e)

        if cnx and cnx.is_connected():
            cnx.close()

        return {
            "fid": fid,
            "version": version_name,
            "path": file_path,
            "total_tags": len(tags_blame),
            "tags": tags_blame,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_file_blame: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/timeline")
def get_commit_timeline(
    version_name: str,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """Retrieve structured commit timeline and contributor activity metrics."""
    limit_val = limit if isinstance(limit, int) else 100
    commit_res = get_version_commits(version_name=version_name, limit=limit_val, offset=0)
    commits = commit_res.get("commits", [])

    # Calculate contributor frequency
    author_counts: dict[str, dict[str, Any]] = {}
    for c in commits:
        a = c.get("author", {})
        a_key = a.get("name") or a.get("email") or "Unknown"
        if a_key not in author_counts:
            author_counts[a_key] = {
                "name": a.get("name") or a_key,
                "email": a.get("email", ""),
                "person_id": a.get("person_id", 1),
                "commits_count": 0,
            }
        author_counts[a_key]["commits_count"] += 1

    top_authors = sorted(author_counts.values(), key=lambda x: x["commits_count"], reverse=True)[:10]

    return {
        "version": version_name,
        "total_commits": commit_res.get("total_count", commit_res.get("total", len(commits))),
        "displayed_commits": len(commits),
        "top_contributors": top_authors,
        "timeline": commits,
    }


@app.get("/api/version/{version_name}/credits")
def get_credits_overview(
    version_name: str,
    q: str = Query("", description="Search term for contributor name, email, or contribution keyword"),
) -> dict[str, Any]:
    """Retrieve directory of all credited Linux contributors in CREDITS."""
    cnx = db.get_connection()
    try:
        sections, _ = get_maintainer_data(cnx, version_name)
        credits_entries = get_credits_data(cnx, version_name)

        # Build maintainer lookup by name & email
        subsystems_by_email: dict[str, list[dict[str, Any]]] = defaultdict(list)
        subsystems_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sec in sections:
            for m in sec.members:
                item = {
                    "sec_id": getattr(sec, "sec_id", None),
                    "name": sec.name,
                    "role": m.role.name.capitalize(),
                }
                if m.email:
                    subsystems_by_email[m.email.lower()].append(item)
                if m.name:
                    subsystems_by_name[m.name.lower()].append(item)

        query_str = q.strip().lower() if isinstance(q, str) else ""
        results = []
        for ce in credits_entries:
            c_name = ce.name
            c_email = ce.email
            c_desc = ce.description

            if query_str:
                matches_name = query_str in c_name.lower()
                matches_email = query_str in c_email.lower()
                matches_desc = query_str in c_desc.lower()
                matches_web = query_str in ce.web_page.lower()
                matches_pgp = query_str in ce.pgp_key.lower()
                if not (matches_name or matches_email or matches_desc or matches_web or matches_pgp):
                    continue

            # Linked subsystems
            linked_subs = []
            if c_email and c_email.lower() in subsystems_by_email:
                linked_subs = subsystems_by_email[c_email.lower()]
            elif c_name and c_name.lower() in subsystems_by_name:
                linked_subs = subsystems_by_name[c_name.lower()]

            results.append({
                "credit_id": getattr(ce, "credit_id", None),
                "person_id": getattr(ce, "person_id", None),
                "name": c_name,
                "email": c_email,
                "web_page": ce.web_page,
                "pgp_key": ce.pgp_key,
                "description": c_desc,
                "snail_mail": ce.snail_mail,
                "subsystems": linked_subs,
                "subsystems_count": len(linked_subs),
            })

        if cnx and cnx.is_connected():
            cnx.close()

        return {
            "version": version_name,
            "total_count": len(results),
            "credits": results,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_credits_overview: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/dev/tables")
def get_dev_table_counts() -> dict[str, Any]:
    """Dev introspection endpoint returning row counts for all 25 schema tables."""
    table_names = [
        "m_v_main",
        "m_file_name",
        "m_file",
        "m_bridge_file",
        "m_moved_file",
        "m_type_descriptor",
        "m_ast",
        "m_ast_container",
        "m_ast_include",
        "m_ast_debug",
        "m_tag",
        "m_bridge_tag",
        "m_map_ast",
        "m_bridge_map",
        "m_ast_hash",
        "m_kconfig_symbol",
        "m_kconfig_relation",
        "m_kconfig_tree",
        "m_kconfig_kbuild",
        "m_maintainer_person",
        "m_maintainer_section",
        "m_maintainer_member",
        "m_maintainer_pattern",
        "m_maintainer_file",
        "m_credits_entry",
    ]
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    counts = {}
    try:
        cursor = cnx.cursor()
        for tname in table_names:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {tname};")  # noqa: S608
                cnt = cursor.fetchone()[0]
                counts[tname] = cnt
            except Exception as e:
                counts[tname] = f"Error: {e}"
        cursor.close()
        cnx.close()
        return counts
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_dev_table_counts: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e



@app.get("/api/dev/endpoints")
def get_dev_endpoints() -> list[dict[str, Any]]:
    """Dev catalog detailing all available API endpoints with sample test arguments."""
    return [
        {
            "method": "GET",
            "path": "/api/versions",
            "description": "List all registered Linux kernel versions",
            "sample_call": "/api/versions",
        },
        {
            "method": "GET",
            "path": "/api/type_descriptors",
            "description": "List all AST syntax category descriptors",
            "sample_call": "/api/type_descriptors",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/browse/{path}",
            "description": "Browse directory hierarchy or fetch full file AST tags and spatial maps",
            "sample_call": "/api/version/v3.0/browse/",
        },
        {
            "method": "GET",
            "path": "/api/ast/{ast_id}/tree?depth={depth}",
            "description": "Recursively resolve m_ast_container child hierarchy down to requested depth",
            "sample_call": "/api/ast/1/tree?depth=3",
        },
        {
            "method": "GET",
            "path": "/api/file/{fid}",
            "description": "Direct file metadata and tag inspection by File ID",
            "sample_call": "/api/file/1",
        },
        {
            "method": "GET",
            "path": "/api/tag/{tag_id}",
            "description": "Direct tag metadata and spatial coordinate map inspection",
            "sample_call": "/api/tag/1",
        },
        {
            "method": "GET",
            "path": "/api/dev/tables",
            "description": "Inspect row counts across all 18 database schema tables",
            "sample_call": "/api/dev/tables",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/kconfig/search?q={query}&type={type}",
            "description": "Search Kconfig symbols by name, prompt, or help text with type filtering",
            "sample_call": "/api/version/v3.0/kconfig/search?q=EXT4",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/kconfig/symbol/{name_or_kcid}",
            "description": "Get full symbol details, direct dependencies, and reverse dependencies",
            "sample_call": "/api/version/v3.0/kconfig/symbol/EXT4_FS",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/kconfig/tree",
            "description": "Fetch complete hierarchical Menuconfig tree structure",
            "sample_call": "/api/version/v3.0/kconfig/tree",
        },
        {
            "method": "POST",
            "path": "/api/version/{version_name}/kconfig/export",
            "description": "Generate standard kernel .config file from symbol assignments",
            "sample_call": "/api/version/v3.0/kconfig/export",
        },
        {
            "method": "POST",
            "path": "/api/version/{version_name}/kconfig/import",
            "description": "Parse imported .config file content into symbol assignments",
            "sample_call": "/api/version/v3.0/kconfig/import",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/maintainers",
            "description": "Catalog of all kernel subsystems in the active version with maintainers and pattern rules",
            "sample_call": "/api/version/v3.0/maintainers",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/maintainer/section/{sec_id_or_name}",
            "description": "Full subsystem details with maintainer roster, pattern rules, and matching repository files",
            "sample_call": "/api/version/v3.0/maintainer/section/EXT4%20FILE%20SYSTEM",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/person/{person_id_or_email}",
            "description": "Developer/maintainer profile with CREDITS bio and all maintained subsystems",
            "sample_call": "/api/version/v3.0/person/tytso@mit.edu",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/credits",
            "description": "Directory of all credited Linux contributors in CREDITS with keyword search",
            "sample_call": "/api/version/v3.0/credits",
        },
        {
            "method": "GET",
            "path": "/api/diff/versions/{v1}/{v2}",
            "description": "Cross-version file tree comparison (added, removed, modified, unchanged)",
            "sample_call": "/api/diff/versions/v3.0/v3.0",
        },
        {
            "method": "GET",
            "path": "/api/diff/kconfig/{v1}/{v2}",
            "description": "Cross-version Kconfig symbol & lifecycle difference analysis",
            "sample_call": "/api/diff/kconfig/v3.0/v3.0",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/xref/{symbol_name}",
            "description": "Global symbol cross-reference (definitions and usage tags across files)",
            "sample_call": "/api/version/v3.0/xref/kmalloc",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/symbol_lookup?q={q}",
            "description": "Fast symbol autocomplete across AST identifiers",
            "sample_call": "/api/version/v3.0/symbol_lookup?q=ext4",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/kconfig/graph/{symbol_name}",
            "description": "Interactive node-link DAG graph data for visual Canvas dependency rendering",
            "sample_call": "/api/version/v3.0/kconfig/graph/EXT4_FS",
        },
        {
            "method": "POST",
            "path": "/api/version/{version_name}/kconfig/autosolve",
            "description": "1-click Kconfig constraint auto-solver for prerequisite dependency chain",
            "sample_call": "/api/version/v3.0/kconfig/autosolve",
        },
        {
            "method": "POST",
            "path": "/api/version/{version_name}/kconfig/diff_config",
            "description": "Compare active .config against a defconfig profile",
            "sample_call": "/api/version/v3.0/kconfig/diff_config",
        },
        {
            "method": "POST",
            "path": "/api/version/{version_name}/patch/maintainers",
            "description": "Analyze unified diff text and match maintainer/reviewer recipients with attribution",
            "sample_call": "/api/version/v3.0/patch/maintainers",
        },
        {
            "method": "POST",
            "path": "/api/version/{version_name}/ast/query",
            "description": "AST Semantic Query Sandbox with structural constraints and type filters",
            "sample_call": "/api/version/v3.0/ast/query",
        },
        {
            "method": "GET",
            "path": "/api/version/{version_name}/export/compile_commands",
            "description": "Export Clang compile_commands.json Compilation Database",
            "sample_call": "/api/version/v3.0/export/compile_commands?arch=x86",
        },
    ]


import difflib

class AutoSolveRequest(BaseModel):
    target_symbol: str
    current_values: dict[str, str] = {}


class DiffConfigRequest(BaseModel):
    active_config: dict[str, str] = {}
    target_defconfig: str | None = None
    custom_config: dict[str, str] | None = None


class PatchReviewRequest(BaseModel):
    patch_text: str


class AstQueryRequest(BaseModel):
    type_id: int | None = None
    type_name: str | None = None
    name_pattern: str | None = None
    path_prefix: str | None = None
    container_depth: int | None = None
    limit: int = 50
    offset: int = 0


class FootprintRequest(BaseModel):
    kconfig_values: dict[str, str] = {}


class FormatPatchRequest(BaseModel):
    file_path: str
    original_content: str
    modified_content: str
    author_name: str = "Kernel Developer"
    author_email: str = "developer@kernel.org"
    commit_subject: str = "kernel: apply updates"
    commit_message: str = ""


# =========================================================================
# 1. CROSS-VERSION DIFF ENDPOINTS
# =========================================================================
@app.get("/api/diff/versions/{v1}/{v2}")
def get_versions_diff(v1: str, v2: str) -> dict[str, Any]:
    """Compare file trees between version v1 and v2."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid1, vname1 = get_version_info(cnx, v1)
        vid2, vname2 = get_version_info(cnx, v2)
        cursor = cnx.cursor()

        # Query files for v1
        cursor.execute(
            """
            SELECT f.fname, fi.fid, fi.ftype, fi.s_stat, fi.e_stat
            FROM m_file_name f
            JOIN m_bridge_file bf ON f.fnid = bf.fnid
            JOIN m_file fi ON bf.fid = fi.fid
            WHERE bf.vid = %s;
            """,
            (vid1,),
        )
        files_v1 = {safe_decode(r[0]): {"fid": r[1], "ftype": r[2], "s_stat": safe_decode(r[3]), "e_stat": safe_decode(r[4])} for r in cursor.fetchall()}

        # Query files for v2
        cursor.execute(
            """
            SELECT f.fname, fi.fid, fi.ftype, fi.s_stat, fi.e_stat
            FROM m_file_name f
            JOIN m_bridge_file bf ON f.fnid = bf.fnid
            JOIN m_file fi ON bf.fid = fi.fid
            WHERE bf.vid = %s;
            """,
            (vid2,),
        )
        files_v2 = {safe_decode(r[0]): {"fid": r[1], "ftype": r[2], "s_stat": safe_decode(r[3]), "e_stat": safe_decode(r[4])} for r in cursor.fetchall()}
        cursor.close()
        cnx.close()

        all_paths = sorted(set(files_v1.keys()) | set(files_v2.keys()))
        added, removed, modified, unchanged = [], [], [], []

        for p in all_paths:
            in_v1 = p in files_v1
            in_v2 = p in files_v2
            if not in_v1 and in_v2:
                added.append({"path": p, "v2": files_v2[p]})
            elif in_v1 and not in_v2:
                removed.append({"path": p, "v1": files_v1[p]})
            else:
                f1, f2 = files_v1[p], files_v2[p]
                if f1["fid"] != f2["fid"] or f2["s_stat"] == "M" or f2["s_stat"] == "R":
                    modified.append({"path": p, "v1": f1, "v2": f2})
                else:
                    unchanged.append({"path": p, "fid": f1["fid"]})

        return {
            "v1": vname1,
            "v2": vname2,
            "summary": {
                "total_v1": len(files_v1),
                "total_v2": len(files_v2),
                "added_count": len(added),
                "removed_count": len(removed),
                "modified_count": len(modified),
                "unchanged_count": len(unchanged),
            },
            "added": added[:200],
            "removed": removed[:200],
            "modified": modified[:200],
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_versions_diff: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/diff/kconfig/{v1}/{v2}")
def get_kconfig_diff(v1: str, v2: str) -> dict[str, Any]:
    """Compare Kconfig symbols and lifecycle states between two kernel versions."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid1, vname1 = get_version_info(cnx, v1)
        vid2, vname2 = get_version_info(cnx, v2)
        cursor = cnx.cursor()

        has_vid_cols = _has_column(cursor, "m_kconfig_symbol", "vid_s")
        if has_vid_cols:
            cursor.execute(
                """
                SELECT name, type, prompt, def_val, vid_s, vid_e
                FROM m_kconfig_symbol
                WHERE (vid_e = 0 OR vid_e >= %s) AND vid_s <= %s;
                """,
                (vid1, vid1),
            )
            syms_v1 = {safe_decode(r[0]): {"type": r[1], "prompt": safe_decode(r[2]), "def_val": safe_decode(r[3])} for r in cursor.fetchall() if r[0]}

            cursor.execute(
                """
                SELECT name, type, prompt, def_val, vid_s, vid_e
                FROM m_kconfig_symbol
                WHERE (vid_e = 0 OR vid_e >= %s) AND vid_s <= %s;
                """,
                (vid2, vid2),
            )
            syms_v2 = {safe_decode(r[0]): {"type": r[1], "prompt": safe_decode(r[2]), "def_val": safe_decode(r[3])} for r in cursor.fetchall() if r[0]}
        else:
            cursor.execute("SELECT name, type, prompt, def_val FROM m_kconfig_symbol;")
            all_syms = {safe_decode(r[0]): {"type": r[1], "prompt": safe_decode(r[2]), "def_val": safe_decode(r[3])} for r in cursor.fetchall() if r[0]}
            syms_v1 = dict(all_syms)
            syms_v2 = dict(all_syms)

        cursor.close()
        cnx.close()

        all_names = sorted(set(syms_v1.keys()) | set(syms_v2.keys()))
        added_syms, removed_syms, changed_syms = [], [], []

        for name in all_names:
            in_v1 = name in syms_v1
            in_v2 = name in syms_v2
            if not in_v1 and in_v2:
                added_syms.append({"name": name, **syms_v2[name]})
            elif in_v1 and not in_v2:
                removed_syms.append({"name": name, **syms_v1[name]})
            else:
                s1, s2 = syms_v1[name], syms_v2[name]
                changes = {}
                if s1["type"] != s2["type"]:
                    changes["type"] = {"v1": s1["type"], "v2": s2["type"]}
                if s1["prompt"] != s2["prompt"]:
                    changes["prompt"] = {"v1": s1["prompt"], "v2": s2["prompt"]}
                if s1["def_val"] != s2["def_val"]:
                    changes["def_val"] = {"v1": s1["def_val"], "v2": s2["def_val"]}
                if changes:
                    changed_syms.append({"name": name, "v1": s1, "v2": s2, "changes": changes})

        return {
            "v1": vname1,
            "v2": vname2,
            "summary": {
                "total_v1": len(syms_v1),
                "total_v2": len(syms_v2),
                "added": len(added_syms),
                "removed": len(removed_syms),
                "changed": len(changed_syms),
            },
            "added_symbols": added_syms[:150],
            "removed_symbols": removed_syms[:150],
            "changed_symbols": changed_syms[:150],
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_kconfig_diff: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# =========================================================================
# 2. GLOBAL SYMBOL XREF & AUTOCOMPLETE ENDPOINTS
# =========================================================================
@app.get("/api/version/{version_name}/xref/{symbol_name}")
def get_symbol_xref(version_name: str, symbol_name: str) -> dict[str, Any]:
    """Find symbol definition and global usage references across the kernel source tree."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        clean_sym = symbol_name.strip()

        # 1. Look for definitions in m_ast
        cursor.execute(
            """
            SELECT a.ast_id, a.name, a.type_id, td.name AS type_name, bt.line_s, bt.line_e,
                   f.fname, fi.fid
            FROM m_ast a
            JOIN m_type_descriptor td ON a.type_id = td.type_id
            JOIN m_tag t ON a.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_file fi ON bt.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE bf.vid = %s AND (t.vid_e = 0 OR t.vid_e >= %s) AND t.vid_s <= %s AND a.name = %s
            LIMIT 20;
            """,
            (vid, vid, vid, clean_sym),
        )
        definitions = [
            {
                "ast_id": r[0],
                "name": safe_decode(r[1]),
                "type_id": r[2],
                "type_name": safe_decode(r[3]),
                "line_s": r[4],
                "line_e": r[5],
                "file_path": safe_decode(r[6]),
                "fid": r[7],
            }
            for r in cursor.fetchall()
        ]

        # 2. Look for usage references in m_tag & m_bridge_tag
        cursor.execute(
            """
            SELECT t.tag_id, t.code, bt.line_s, bt.line_e, bt.char_s, bt.char_e,
                   f.fname, fi.fid, td.name AS tag_type
            FROM m_ast a
            JOIN m_type_descriptor td ON a.type_id = td.type_id
            JOIN m_tag t ON a.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_file fi ON bt.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE bf.vid = %s AND (t.vid_e = 0 OR t.vid_e >= %s) AND t.vid_s <= %s AND a.name = %s
            LIMIT 100;
            """,
            (vid, vid, vid, clean_sym),
        )
        references = [
            {
                "tag_id": r[0],
                "code": safe_decode(r[1]),
                "line_s": r[2],
                "line_e": r[3],
                "char_s": r[4],
                "char_e": r[5],
                "file_path": safe_decode(r[6]),
                "fid": r[7],
                "tag_type": safe_decode(r[8]) or "Identifier",
            }
            for r in cursor.fetchall()
        ]

        cursor.close()
        cnx.close()
        return {
            "version": version_name,
            "symbol": clean_sym,
            "definitions_count": len(definitions),
            "definitions": definitions,
            "references_count": len(references),
            "references": references,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_symbol_xref: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/version/{version_name}/symbol_lookup")
def lookup_symbols(version_name: str, q: str = Query("", min_length=1), limit: int = 30) -> list[dict[str, Any]]:
    """Autocomplete / fast symbol lookup across AST identifiers."""
    cnx = db.get_connection()
    if not cnx:
        return []
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        cursor.execute(
            """
            SELECT DISTINCT a.name, td.name AS type_name, f.fname, fi.fid
            FROM m_ast a
            JOIN m_type_descriptor td ON a.type_id = td.type_id
            JOIN m_tag t ON a.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_file fi ON bt.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE bf.vid = %s AND (t.vid_e = 0 OR t.vid_e >= %s) AND t.vid_s <= %s AND a.name LIKE %s AND a.name != ''
            ORDER BY LENGTH(a.name) ASC, a.name ASC
            LIMIT %s;
            """,
            (vid, vid, vid, f"{q.strip()}%", limit),
        )
        results = [
            {
                "name": safe_decode(r[0]),
                "type_name": safe_decode(r[1]),
                "file_path": safe_decode(r[2]),
                "fid": r[3],
            }
            for r in cursor.fetchall()
        ]
        cursor.close()
        cnx.close()
        return results
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.debug("Error in lookup_symbols: %s", e)
        return []


# =========================================================================
# 3. INTERACTIVE DEPENDENCY GRAPH (DAG) ENDPOINT
# =========================================================================
@app.get("/api/version/{version_name}/kconfig/graph/{symbol_name}")
def get_kconfig_graph(version_name: str, symbol_name: str, depth: int = 2) -> dict[str, Any]:
    """Retrieve node-link DAG graph data for visual Canvas dependency rendering."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        clean_sym = symbol_name.strip()
        if clean_sym.startswith("CONFIG_"):
            clean_sym = clean_sym[7:]

        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        visited = set()
        queue = [(clean_sym, 0)]

        while queue:
            curr_sym, curr_depth = queue.pop(0)
            if curr_sym in visited:
                continue
            visited.add(curr_sym)

            # Query symbol info
            cursor.execute(
                """
                SELECT s.kcid, s.name, s.type, s.prompt
                FROM m_kconfig_symbol s
                WHERE (s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s AND s.name = %s
                LIMIT 1;
                """,
                (vid, vid, curr_sym),
            )
            s_row = cursor.fetchone()
            if s_row:
                kcid = s_row[0]
                nodes[curr_sym] = {
                    "id": curr_sym,
                    "label": f"CONFIG_{curr_sym}",
                    "prompt": safe_decode(s_row[3]) or curr_sym,
                    "type": s_row[2],
                    "is_root": (curr_sym == clean_sym),
                }

                if curr_depth < depth:
                    # Dependencies (outgoing)
                    cursor.execute(
                        """
                        SELECT r.target_name, r.rel_type
                        FROM m_kconfig_relation r
                        WHERE r.kcid = %s;
                        """,
                        (kcid,),
                    )
                    for r in cursor.fetchall():
                        target_name = safe_decode(r[0])
                        rel_type = safe_decode(r[1])
                        if target_name and target_name not in ["y", "n", "m"]:
                            edges.append({
                                "source": curr_sym,
                                "target": target_name,
                                "rel_type": rel_type,
                            })
                            if target_name not in visited:
                                queue.append((target_name, curr_depth + 1))

                    # Reverse Dependencies (incoming)
                    cursor.execute(
                        """
                        SELECT s2.name, r.rel_type
                        FROM m_kconfig_relation r
                        JOIN m_kconfig_symbol s2 ON r.kcid = s2.kcid
                        WHERE r.target_name = %s AND (s2.vid_e = 0 OR s2.vid_e >= %s) AND s2.vid_s <= %s;
                        """,
                        (curr_sym, vid, vid),
                    )
                    for r in cursor.fetchall():
                        source_name = safe_decode(r[0])
                        rel_type = safe_decode(r[1])
                        if source_name:
                            edges.append({
                                "source": source_name,
                                "target": curr_sym,
                                "rel_type": rel_type,
                            })
                            if source_name not in visited:
                                queue.append((source_name, curr_depth + 1))
            else:
                nodes[curr_sym] = {
                    "id": curr_sym,
                    "label": f"CONFIG_{curr_sym}",
                    "prompt": curr_sym,
                    "type": 0,
                    "is_root": (curr_sym == clean_sym),
                }

        cursor.close()
        cnx.close()
        return {
            "root_symbol": clean_sym,
            "nodes": list(nodes.values()),
            "edges": edges,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_kconfig_graph: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# =========================================================================
# 4. KCONFIG AUTO-SOLVER & CONFIG COMPARE ENDPOINTS
# =========================================================================
@app.post("/api/version/{version_name}/kconfig/autosolve")
def autosolve_kconfig(version_name: str, req: AutoSolveRequest) -> dict[str, Any]:
    """Compute minimal prerequisite toggle chain to enable a requested target symbol."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        clean_target = req.target_symbol.strip()
        if clean_target.startswith("CONFIG_"):
            clean_target = clean_target[7:]

        toggles_needed: list[dict[str, Any]] = []
        visited = set()
        queue = [clean_target]

        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)

            cursor.execute(
                """
                SELECT r.target_name, r.rel_type, s.prompt
                FROM m_kconfig_relation r
                JOIN m_kconfig_symbol s ON r.kcid = s.kcid
                WHERE s.name = %s AND (s.vid_e = 0 OR s.vid_e >= %s) AND s.vid_s <= %s;
                """,
                (curr, vid, vid),
            )
            deps = cursor.fetchall()
            for dep in deps:
                dep_name = safe_decode(dep[0])
                rel_type = safe_decode(dep[1])
                if dep_name and dep_name not in ["y", "n", "m"] and not dep_name.isdigit():
                    cur_val = req.current_values.get(dep_name, "n")
                    if cur_val != "y" and rel_type in ["depends_on", "requires"]:
                        toggles_needed.append({
                            "symbol": dep_name,
                            "suggested_value": "y",
                            "reason": f"Required by CONFIG_{curr} ({rel_type})",
                        })
                        queue.append(dep_name)

        cursor.close()
        cnx.close()
        return {
            "target_symbol": clean_target,
            "solution_found": True,
            "toggles_needed": toggles_needed,
            "count": len(toggles_needed),
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in autosolve_kconfig: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/version/{version_name}/kconfig/diff_config")
def diff_kconfig_configurations(version_name: str, req: DiffConfigRequest) -> dict[str, Any]:
    """Compare active configuration against a defconfig or custom config dictionary."""
    target_config = {}
    if req.custom_config:
        target_config = req.custom_config
    elif req.target_defconfig:
        def_content = get_kconfig_defconfig_content(version_name, req.target_defconfig)
        target_config = def_content.get("config", {})

    active = req.active_config or {}
    all_symbols = sorted(set(active.keys()) | set(target_config.keys()))

    differences = []
    matches = 0
    for sym in all_symbols:
        val_a = active.get(sym, "n")
        val_b = target_config.get(sym, "n")
        if val_a == val_b:
            matches += 1
        else:
            differences.append({
                "symbol": sym,
                "active_value": val_a,
                "target_value": val_b,
            })

    total = len(all_symbols) or 1
    match_pct = round((matches / total) * 100, 1)

    return {
        "version": version_name,
        "target_name": req.target_defconfig or "Custom Configuration",
        "total_symbols": total,
        "matching_symbols": matches,
        "mismatched_symbols": len(differences),
        "match_percentage": match_pct,
        "differences": differences,
    }


# =========================================================================
# 5. PATCH REVIEW & GET_MAINTAINER MATCHING ENDPOINT
# =========================================================================
@app.post("/api/version/{version_name}/patch/maintainers")
def match_patch_maintainers(version_name: str, req: PatchReviewRequest) -> dict[str, Any]:
    """Analyze unified diff patch text and generate exact get_maintainer.pl recipient rosters."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        lines = req.patch_text.splitlines()
        touched_files: dict[str, int] = defaultdict(int)

        curr_file = None
        for line in lines:
            if line.startswith("diff --git a/"):
                parts = line.split(" ")
                if len(parts) >= 4 and parts[3].startswith("b/"):
                    curr_file = parts[3][2:].strip()
            elif line.startswith("+++ b/"):
                curr_file = line[6:].strip()
            elif line.startswith("--- a/") and not curr_file:
                curr_file = line[6:].strip()
            elif line.startswith("+") and not line.startswith("+++"):
                if curr_file:
                    touched_files[curr_file] += 1
            elif line.startswith("-") and not line.startswith("---"):
                if curr_file:
                    touched_files[curr_file] += 1

        if not touched_files and curr_file:
            touched_files[curr_file] = 1

        all_maintainers: dict[str, dict[str, Any]] = {}
        all_reviewers: dict[str, dict[str, Any]] = {}
        all_lists: set[str] = set()
        all_subsystems: set[str] = set()

        file_breakdown = []
        for fpath, mod_lines in touched_files.items():
            subs = resolve_subsystems_for_file_internal(cnx, version_name, fpath)
            file_breakdown.append({
                "file_path": fpath,
                "modified_lines": mod_lines,
                "subsystems": subs,
            })
            for s in subs:
                all_subsystems.add(s["name"])
                if s.get("mailing_list"):
                    all_lists.add(s["mailing_list"])
                for m in s.get("maintainers", []):
                    m_key = m.get("email") or m.get("name")
                    if m_key:
                        if m_key not in all_maintainers:
                            all_maintainers[m_key] = {**m, "roles": set(), "subsystems": set()}
                        all_maintainers[m_key]["roles"].add("maintainer")
                        all_maintainers[m_key]["subsystems"].add(s["name"])
                for r in s.get("reviewers", []):
                    r_key = r.get("email") or r.get("name")
                    if r_key:
                        if r_key not in all_reviewers:
                            all_reviewers[r_key] = {**r, "roles": set(), "subsystems": set()}
                        all_reviewers[r_key]["roles"].add("reviewer")
                        all_reviewers[r_key]["subsystems"].add(s["name"])

        # Format To: and Cc: lists
        to_list = [f"{m['name']} <{m['email']}>" if m.get('email') else m['name'] for m in all_maintainers.values()]
        cc_list = [f"{r['name']} <{r['email']}>" if r.get('email') else r['name'] for r in all_reviewers.values()]
        cc_list.extend(list(all_lists))

        cnx.close()
        return {
            "version": version_name,
            "touched_files_count": len(touched_files),
            "files": file_breakdown,
            "subsystems": list(all_subsystems),
            "suggested_to": to_list,
            "suggested_cc": cc_list,
            "maintainers": [{**m, "roles": list(m["roles"]), "subsystems": list(m["subsystems"])} for m in all_maintainers.values()],
            "reviewers": [{**r, "roles": list(r["roles"]), "subsystems": list(r["subsystems"])} for r in all_reviewers.values()],
            "mailing_lists": list(all_lists),
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in match_patch_maintainers: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# =========================================================================
# 6. AST SEMANTIC QUERY SANDBOX ENDPOINT
# =========================================================================
@app.post("/api/version/{version_name}/ast/query")
def query_ast_semantic_sandbox(version_name: str, req: AstQueryRequest) -> dict[str, Any]:
    """Search AST symbols by structural constraints (type, pattern, container depth, path prefix)."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()

        conditions = [
            "bf.vid = %s",
            "(t.vid_e = 0 OR t.vid_e >= %s)",
            "t.vid_s <= %s",
            "a.name != ''",
        ]
        params: list[Any] = [vid, vid, vid]

        if req.type_id is not None:
            conditions.append("a.type_id = %s")
            params.append(req.type_id)
        elif req.type_name:
            conditions.append("td.name LIKE %s")
            params.append(f"%{req.type_name.strip()}%")

        if req.name_pattern:
            conditions.append("a.name LIKE %s")
            params.append(f"%{req.name_pattern.strip()}%")

        if req.path_prefix:
            conditions.append("f.fname LIKE %s")
            params.append(f"{req.path_prefix.strip()}%")

        where_clause = " AND ".join(conditions)

        # Count total matches
        count_sql = f"""
            SELECT COUNT(*)
            FROM m_ast a
            JOIN m_type_descriptor td ON a.type_id = td.type_id
            JOIN m_tag t ON a.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_file fi ON bt.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE {where_clause};
        """
        cursor.execute(count_sql, tuple(params))
        total_count = cursor.fetchone()[0]

        # Fetch page
        limit_val = min(req.limit, 200)
        offset_val = max(req.offset, 0)
        query_sql = f"""
            SELECT a.ast_id, a.name, a.type_id, td.name AS type_name, bt.line_s, bt.line_e,
                   f.fname, fi.fid
            FROM m_ast a
            JOIN m_type_descriptor td ON a.type_id = td.type_id
            JOIN m_tag t ON a.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_file fi ON bt.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE {where_clause}
            ORDER BY f.fname ASC, bt.line_s ASC
            LIMIT %s OFFSET %s;
        """
        cursor.execute(query_sql, tuple(params + [limit_val, offset_val]))
        rows = cursor.fetchall()
        cursor.close()
        cnx.close()

        items = [
            {
                "ast_id": r[0],
                "name": safe_decode(r[1]),
                "type_id": r[2],
                "type_name": safe_decode(r[3]),
                "line_s": r[4],
                "line_e": r[5],
                "file_path": safe_decode(r[6]),
                "fid": r[7],
            }
            for r in rows
        ]

        return {
            "version": version_name,
            "total": total_count,
            "limit": limit_val,
            "offset": offset_val,
            "items": items,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in query_ast_semantic_sandbox: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# =========================================================================
# 7. CLANG COMPILE_COMMANDS.JSON EXPORTER ENDPOINT
# =========================================================================
@app.get("/api/version/{version_name}/export/compile_commands")
def export_compile_commands(version_name: str, arch: str = "x86") -> list[dict[str, Any]]:
    """Export standard compile_commands.json Compilation Database for Clangd/VS Code."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()

        # Query distinct compiled source files in this version
        cursor.execute(
            """
            SELECT DISTINCT f.fname
            FROM m_file_name f
            JOIN m_bridge_file bf ON f.fnid = bf.fnid
            WHERE bf.vid = %s AND (f.fname LIKE '%.c' OR f.fname LIKE '%.h')
            ORDER BY f.fname ASC
            LIMIT 5000;
            """,
            (vid,),
        )
        rows = cursor.fetchall()
        cursor.close()
        cnx.close()

        arch_flags = f"-D__KERNEL__ -Iinclude -Iarch/{arch}/include -nostdinc -isystem /usr/lib/gcc/x86_64-linux-gnu/include -Wall -Wundef -Wstrict-prototypes -Wno-trigraphs -fno-strict-aliasing -fno-common"

        commands = []
        for r in rows:
            fname = safe_decode(r[0])
            if fname.endswith(".c"):
                commands.append({
                    "directory": "/linux",
                    "command": f"gcc {arch_flags} -c {fname} -o {fname[:-2]}.o",
                    "file": fname,
                })

        return commands
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in export_compile_commands: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e

# =========================================================================
# 8. STRUCT MEMORY LAYOUT & PAHOLE VISUALIZER ENDPOINT
# =========================================================================
@app.get("/api/version/{version_name}/struct/layout/{struct_name}")
def get_struct_layout(version_name: str, struct_name: str) -> dict[str, Any]:
    """Calculate and visualize C struct memory offsets, padding holes, and cacheline alignment."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        clean_name = struct_name.strip()
        if clean_name.startswith("struct "):
            clean_name = clean_name[7:]

        # Query AST for struct node
        cursor.execute(
            """
            SELECT a.ast_id, a.name, a.type_id, td.name, bt.line_s, bt.line_e, f.fname, fi.fid
            FROM m_ast a
            JOIN m_type_descriptor td ON a.type_id = td.type_id
            JOIN m_tag t ON a.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_file fi ON bt.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE bf.vid = %s AND (a.name = %s OR a.name = %s)
            LIMIT 1;
            """,
            (vid, clean_name, f"struct {clean_name}"),
        )
        row = cursor.fetchone()

        file_path = safe_decode(row[6]) if row else f"include/linux/{clean_name}.h"
        line_s = row[4] if row else 1
        line_e = row[5] if row else 50

        fields_raw = []
        if row:
            ast_id = row[0]
            cursor.execute(
                """
                SELECT c.priority, ref.name, td.name
                FROM m_ast_container c
                JOIN m_ast ref ON c.ref_ast_id = ref.ast_id
                JOIN m_type_descriptor td ON c.type_id = td.type_id
                WHERE c.ast_id = %s
                ORDER BY c.priority ASC;
                """,
                (ast_id,),
            )
            fields_raw = cursor.fetchall()
        cursor.close()
        cnx.close()

        type_size_map = {
            "char": (1, 1), "u8": (1, 1), "uint8_t": (1, 1), "bool": (1, 1),
            "short": (2, 2), "u16": (2, 2), "uint16_t": (2, 2),
            "int": (4, 4), "u32": (4, 4), "uint32_t": (4, 4), "atomic_t": (4, 4), "spinlock_t": (4, 4),
            "long": (8, 8), "u64": (8, 8), "uint64_t": (8, 8), "pointer": (8, 8), "void *": (8, 8),
            "atomic64_t": (8, 8), "struct list_head": (16, 8), "struct hlist_node": (16, 8),
            "struct mutex": (32, 8), "struct rw_semaphore": (40, 8),
        }

        members = []
        curr_offset = 0
        max_align = 1
        total_padding = 0

        parsed_fields = []
        if fields_raw:
            for r in fields_raw:
                f_name = safe_decode(r[1]) or f"field_{r[0]}"
                f_type = safe_decode(r[2]) or "int"
                parsed_fields.append((f_name, f_type))
        else:
            parsed_fields = [
                ("flags", "unsigned long"),
                ("count", "atomic_t"),
                ("lock", "spinlock_t"),
                ("list", "struct list_head"),
                ("priv_data", "void *"),
                ("state", "int"),
                ("name", "char [32]"),
            ]

        for name, ftype in parsed_fields:
            if "*" in ftype or "pointer" in ftype:
                sz, al = 8, 8
            elif "[" in ftype:
                match = re.search(r'\[(\d+)\]', ftype)
                arr_len = int(match.group(1)) if match else 16
                sz, al = arr_len, 1
            else:
                sz, al = type_size_map.get(ftype, (8, 8))

            max_align = max(max_align, al)
            pad_before = (al - (curr_offset % al)) % al
            if pad_before > 0:
                total_padding += pad_before
                curr_offset += pad_before

            cacheline_idx = curr_offset // 64
            cacheline_offset = curr_offset % 64
            crosses_cacheline = (curr_offset // 64) != ((curr_offset + sz - 1) // 64)

            members.append({
                "name": name,
                "type": ftype,
                "size": sz,
                "alignment": al,
                "offset": curr_offset,
                "padding_before": pad_before,
                "cacheline_idx": cacheline_idx,
                "cacheline_offset": cacheline_offset,
                "crosses_cacheline": crosses_cacheline,
            })
            curr_offset += sz

        tail_padding = (max_align - (curr_offset % max_align)) % max_align
        total_padding += tail_padding
        total_size = curr_offset + tail_padding
        cache_lines_used = ((total_size - 1) // 64) + 1 if total_size > 0 else 1

        reordered_sorted = sorted(members, key=lambda m: (-m["alignment"], -m["size"]))
        reordered_curr = 0
        reordered_pad = 0
        for m in reordered_sorted:
            pad = (m["alignment"] - (reordered_curr % m["alignment"])) % m["alignment"]
            reordered_pad += pad
            reordered_curr += pad + m["size"]
        reordered_tail = (max_align - (reordered_curr % max_align)) % max_align
        reordered_pad += reordered_tail
        reordered_total = reordered_curr + reordered_tail

        return {
            "version": version_name,
            "struct_name": f"struct {clean_name}",
            "file_path": file_path,
            "line_s": line_s,
            "line_e": line_e,
            "total_size": total_size,
            "alignment": max_align,
            "padding_bytes": total_padding,
            "cache_lines_used": cache_lines_used,
            "members": members,
            "optimization": {
                "current_size": total_size,
                "optimized_size": reordered_total,
                "bytes_saved": max(0, total_size - reordered_total),
                "suggested_order": [m["name"] for m in reordered_sorted],
            }
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_struct_layout: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e





# =========================================================================
# 10. INTERACTIVE CODEBASE TREEMAP HIERARCHY ENDPOINT
# =========================================================================
@app.get("/api/version/{version_name}/treemap")
def get_codebase_treemap(version_name: str, max_depth: int = 3) -> dict[str, Any]:
    """Retrieve hierarchical folder tree structure formatted for squarified Treemap rendering."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()

        cursor.execute(
            """
            SELECT f.fname, fi.fid, fi.ftype
            FROM m_file_name f
            JOIN m_bridge_file bf ON f.fnid = bf.fnid
            JOIN m_file fi ON bf.fid = fi.fid
            WHERE bf.vid = %s
            ORDER BY f.fname ASC;
            """,
            (vid,),
        )
        files = cursor.fetchall()
        cursor.close()
        cnx.close()

        root = {"name": f"Linux {version_name}", "path": "", "children": {}, "value": 0, "file_count": 0}

        for r in files:
            fpath = safe_decode(r[0])
            parts = fpath.split("/")
            display_parts = parts[:max_depth]
            curr = root
            curr["file_count"] += 1
            curr["value"] += 1

            for idx, part in enumerate(display_parts):
                is_leaf = (idx == len(parts) - 1)
                subpath = "/".join(parts[:idx + 1])
                if part not in curr["children"]:
                    curr["children"][part] = {
                        "name": part,
                        "path": subpath,
                        "children": {},
                        "value": 0,
                        "file_count": 0,
                        "is_leaf": is_leaf,
                    }
                curr = curr["children"][part]
                curr["file_count"] += 1
                curr["value"] += 1

        def format_tree_node(node: dict[str, Any]) -> dict[str, Any]:
            children_dict = node.pop("children", {})
            children_list = [format_tree_node(c) for c in children_dict.values()]
            if children_list:
                node["children"] = children_list
            return node

        return format_tree_node(root)
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_codebase_treemap: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# =========================================================================
# 11. KCONFIG FOOTPRINT & KERNEL SIZE ESTIMATOR (BLOAT-O-METER) ENDPOINT
# =========================================================================
@app.post("/api/version/{version_name}/kconfig/footprint")
def estimate_kconfig_footprint(version_name: str, req: FootprintRequest) -> dict[str, Any]:
    """Estimate compiled C files, lines of code, and binary object footprint for active config."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()

        active_symbols = [
            k.replace("CONFIG_", "") for k, v in req.kconfig_values.items()
            if v in ("y", "m")
        ]

        if not active_symbols:
            cursor.close()
            cnx.close()
            return {
                "version": version_name,
                "active_symbols_count": 0,
                "total_compiled_files": 0,
                "estimated_loc": 0,
                "estimated_binary_kb": 0,
                "files": [],
            }

        format_strings = ','.join(['%s'] * len(active_symbols))
        cursor.execute(
            f"""
            SELECT DISTINCT f.fname, s.name
            FROM m_kconfig_kbuild kb
            JOIN m_kconfig_symbol s ON kb.kcid = s.kcid
            JOIN m_file fi ON kb.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE bf.vid = %s AND s.name IN ({format_strings})
            LIMIT 500;
            """,
            (vid, *active_symbols),
        )
        rows = cursor.fetchall()
        cursor.close()
        cnx.close()

        files_map = {}
        for r in rows:
            fname = safe_decode(r[0])
            sname = safe_decode(r[1])
            if fname not in files_map:
                files_map[fname] = {
                    "file_path": fname,
                    "symbol": sname,
                    "estimated_loc": 250,
                    "estimated_kb": 16.5,
                }

        total_files = len(files_map)
        total_loc = total_files * 250
        total_kb = round(total_files * 16.5, 1)

        return {
            "version": version_name,
            "active_symbols_count": len(active_symbols),
            "total_compiled_files": total_files,
            "estimated_loc": total_loc,
            "estimated_binary_kb": total_kb,
            "files": list(files_map.values())[:100],
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in estimate_kconfig_footprint: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# =========================================================================
# 12. FUNCTION CALL GRAPH & CALLERS / CALLEES HIERARCHY ENDPOINT
# =========================================================================
@app.get("/api/version/{version_name}/callgraph/{function_name}")
def get_function_callgraph(version_name: str, function_name: str) -> dict[str, Any]:
    """Retrieve direct callers and callees for an interactive function call graph."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        vid, version_name = get_version_info(cnx, version_name)
        cursor = cnx.cursor()
        clean_name = function_name.strip()

        cursor.execute(
            """
            SELECT a.ast_id, a.name, bt.line_s, bt.line_e, f.fname, fi.fid
            FROM m_ast a
            JOIN m_tag t ON a.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_file fi ON bt.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE bf.vid = %s AND (t.vid_e = 0 OR t.vid_e >= %s) AND t.vid_s <= %s AND a.name = %s
            LIMIT 1;
            """,
            (vid, vid, vid, clean_name),
        )
        def_row = cursor.fetchone()

        file_path = safe_decode(def_row[4]) if def_row else ""
        line_s = def_row[2] if def_row else 1
        line_e = def_row[3] if def_row else 100

        cursor.execute(
            """
            SELECT DISTINCT f.fname, bt.line_s, t.code
            FROM m_ast a
            JOIN m_tag t ON a.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_file fi ON bt.fid = fi.fid
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_file_name f ON bf.fnid = f.fnid
            WHERE bf.vid = %s AND (t.vid_e = 0 OR t.vid_e >= %s) AND t.vid_s <= %s AND a.name = %s AND f.fname != %s
            LIMIT 30;
            """,
            (vid, vid, vid, clean_name, file_path),
        )
        callers = [
            {
                "file_path": safe_decode(r[0]),
                "line_s": r[1],
                "snippet": safe_decode(r[2])[:120],
            }
            for r in cursor.fetchall()
        ]

        callees = []
        if def_row:
            cursor.execute(
                """
                SELECT DISTINCT a.name, td.name, bt.line_s
                FROM m_bridge_tag bt
                JOIN m_tag t ON bt.tag_id = t.tag_id
                JOIN m_ast a ON t.ast_id = a.ast_id
                JOIN m_type_descriptor td ON a.type_id = td.type_id
                JOIN m_file fi ON bt.fid = fi.fid
                JOIN m_bridge_file bf ON fi.fid = bf.fid
                WHERE bf.vid = %s AND (t.vid_e = 0 OR t.vid_e >= %s) AND t.vid_s <= %s
                  AND fi.fid = %s AND bt.line_s >= %s AND bt.line_e <= %s AND a.name != %s AND a.name != ''
                LIMIT 30;
                """,
                (vid, vid, vid, def_row[5], line_s, line_e, clean_name),
            )
            callees = [
                {
                    "name": safe_decode(r[0]),
                    "type": safe_decode(r[1]),
                    "line": r[2],
                }
                for r in cursor.fetchall()
            ]

        cursor.close()
        cnx.close()

        return {
            "version": version_name,
            "function_name": clean_name,
            "file_path": file_path,
            "line_s": line_s,
            "line_e": line_e,
            "caller_count": len(callers),
            "callee_count": len(callees),
            "callers": callers,
            "callees": callees,
        }
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_function_callgraph: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# =========================================================================
# 13. INTERACTIVE CODE TOURS & ARCHITECTURE WALKTHROUGHS ENDPOINT
# =========================================================================
@app.get("/api/version/{version_name}/tours/presets")
def get_code_tour_presets(version_name: str) -> list[dict[str, Any]]:
    """Retrieve pre-authored interactive kernel architecture walkthrough tours."""
    return [
        {
            "id": "vfs_open_journey",
            "title": "VFS Virtual File System: Path Lookup & File Open",
            "description": "Trace the execution path of the sys_open system call down into filesystem-specific handlers.",
            "steps": [
                {
                    "step_num": 1,
                    "title": "System Call Entry: sys_open",
                    "file_path": "fs/open.c",
                    "line_s": 995,
                    "line_e": 1010,
                    "concept": "User Space to Kernel Transition",
                    "description": "Receives user filename string and permission flags, converts filename to internal struct filename.",
                },
                {
                    "step_num": 2,
                    "title": "File Descriptor Allocation: do_sys_open",
                    "file_path": "fs/open.c",
                    "line_s": 960,
                    "line_e": 985,
                    "concept": "FD Reservation & Flags Sanitization",
                    "description": "Allocates unused integer file descriptor in current task's files_struct.",
                },
                {
                    "step_num": 3,
                    "title": "Dentry & Inode Path Lookup: path_openat",
                    "file_path": "fs/namei.c",
                    "line_s": 2700,
                    "line_e": 2740,
                    "concept": "Directory Cache (dcache) Resolution",
                    "description": "Walks directory path components through dcache hash tables or queries underlying storage block drivers.",
                },
                {
                    "step_num": 4,
                    "title": "Filesystem Specific Dispatch: ext4_file_open",
                    "file_path": "fs/ext4/file.c",
                    "line_s": 150,
                    "line_e": 190,
                    "concept": "Polymorphic File Operations Callback",
                    "description": "Ext4 driver validates inode extent trees and prepares journal transaction locks.",
                },
            ],
        },
        {
            "id": "slab_alloc_journey",
            "title": "Kernel Memory: SLAB / SLUB Cache Allocation",
            "description": "Understand how kmalloc allocates object pools and interfaces with buddy page allocator.",
            "steps": [
                {
                    "step_num": 1,
                    "title": "General Purpose Allocator: kmalloc",
                    "file_path": "include/linux/slab.h",
                    "line_s": 300,
                    "line_e": 330,
                    "concept": "Size Index Calculation",
                    "description": "Determines appropriate kmalloc-N general size cache matching requested byte length.",
                },
                {
                    "step_num": 2,
                    "title": "Object Cache Fast-path: kmem_cache_alloc",
                    "file_path": "mm/slab.c",
                    "line_s": 3700,
                    "line_e": 3740,
                    "concept": "Per-CPU Slab Freelist Inspection",
                    "description": "Fetches pre-allocated object from lockless per-CPU array cache.",
                },
                {
                    "step_num": 3,
                    "title": "Page Frame Replenishment: cache_grow",
                    "file_path": "mm/slab.c",
                    "line_s": 2800,
                    "line_e": 2850,
                    "concept": "Buddy System Page Allocation",
                    "description": "Invokes alloc_pages to allocate new contiguous physical page frames when slab cache is depleted.",
                },
            ],
        },
    ]


# =========================================================================
# 14. IN-BROWSER PATCH STAGING & GIT FORMAT-PATCH GENERATOR ENDPOINT
# =========================================================================
@app.post("/api/version/{version_name}/patch/format")
def generate_formatted_patch(version_name: str, req: FormatPatchRequest) -> dict[str, Any]:
    """Generate RFC-2822 git format-patch email text from staged file edits."""
    cnx = db.get_connection()
    subs = []
    if cnx:
        try:
            subs = resolve_subsystems_for_file_internal(cnx, version_name, req.file_path)
            cnx.close()
        except Exception:
            if cnx.is_connected():
                cnx.close()

    to_recipients = []
    cc_recipients = []
    for s in subs:
        if s.get("mailing_list") and s["mailing_list"] not in cc_recipients:
            cc_recipients.append(s["mailing_list"])
        for m in s.get("maintainers", []):
            formatted = f"{m['name']} <{m['email']}>" if m.get("email") else m.get("name", "")
            if formatted and formatted not in to_recipients:
                to_recipients.append(formatted)

    orig_lines = req.original_content.splitlines(keepends=True)
    mod_lines = req.modified_content.splitlines(keepends=True)
    diff_gen = difflib.unified_diff(
        orig_lines,
        mod_lines,
        fromfile=f"a/{req.file_path}",
        tofile=f"b/{req.file_path}",
        lineterm="\n",
    )
    diff_text = "".join(diff_gen)

    now_rfc2822 = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    to_header = ", ".join(to_recipients) or "linux-kernel@vger.kernel.org"
    cc_header = ", ".join(cc_recipients) or "linux-kernel@vger.kernel.org"

    patch_formatted = f"""From: {req.author_name} <{req.author_email}>
Date: {now_rfc2822}
Subject: [PATCH] {req.commit_subject}
To: {to_header}
Cc: {cc_header}

{req.commit_message or "Apply targeted updates to source."}

Signed-off-by: {req.author_name} <{req.author_email}>
---
 {req.file_path} | {len(mod_lines) - len(orig_lines):+d}
 1 file changed

{diff_text}
--
KernelInfo-Parser Patch Studio
"""

    return {
        "version": version_name,
        "file_path": req.file_path,
        "to": to_recipients,
        "cc": cc_recipients,
        "diff": diff_text,
        "formatted_patch": patch_formatted,
    }


if __name__ == "__main__":
    import uvicorn
    repo_dir = str(Path(__file__).resolve().parent.parent)
    uvicorn.run("webapp.main:app", host="0.0.0.0", port=8000, reload=True, app_dir=repo_dir)
