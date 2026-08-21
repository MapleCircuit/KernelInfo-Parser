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
import json
import logging
from typing import Any
from collections import defaultdict
import mysql.connector
from mysql.connector import pooling
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

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


class DatabaseManager:
    """Manage MySQL connection pool and execute queries with automatic reconnect resilience."""

    def __init__(self) -> None:
        self.user = os.getenv("MYSQL_USER", "root")
        self.password = os.getenv("MYSQL_PASSWORD", "Passe123")
        self.database = os.getenv("MYSQL_DATABASE", "main")
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
        try:
            self.pool = pooling.MySQLConnectionPool(
                pool_name="kernelinfo_pool",
                pool_size=5,
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset="utf8mb4",
                collation="utf8mb4_bin",
                autocommit=True,
            )
            logger.info("Connected to MySQL at %s:%d/%s", self.host, self.port, self.database)
        except Exception as e:
            logger.warning("Could not initialize connection pool (%s). Will retry on query.", e)
            self.pool = None

    def get_connection(self):
        if self.pool is None:
            self._init_pool()
        if self.pool is None:
            # Fallback direct connection
            return mysql.connector.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset="utf8mb4",
                collation="utf8mb4_bin",
            )
        try:
            cnx = self.pool.get_connection()
            if not cnx.is_connected():
                cnx.reconnect(attempts=3, delay=1)
            return cnx
        except Exception:
            self._init_pool()
            return self.pool.get_connection() if self.pool else None


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
def get_all_versions() -> list[dict[str, Any]]:
    """Fetch all registered Linux kernel release versions from m_v_main."""
    cnx = db.get_connection()
    if not cnx:
        raise HTTPException(status_code=500, detail="Database connection unavailable")
    try:
        cursor = cnx.cursor()
        cursor.execute("SELECT vid, vname FROM m_v_main ORDER BY vid ASC;")
        rows = cursor.fetchall()
        cursor.close()
        cnx.close()
        return [{"vid": r[0], "vname": safe_decode(r[1])} for r in rows]
    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
        logger.error("Error in get_all_versions: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/type_descriptors")
@app.get("/type_descriptors")
def get_type_descriptors() -> list[dict[str, Any]]:
    """Fetch all AST construct categories from m_type_descriptor for frontend highlight styling."""
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
                JOIN m_v_main v ON bf.vid = v.vid
                WHERE v.vname = %s AND f.fname LIKE '%/%'
                ORDER BY dir_prefix ASC;
                """,
                (version_name,),
            )
            sub_dirs = [safe_decode(r[0]) for r in cursor.fetchall() if r[0] is not None]
            sub_dir_set = set(sub_dirs)

            # Query top-level files (no slash in fname, not in sub_dirs)
            cursor.execute(
                """
                SELECT f.fname, fi.fid, fi.ftype, fi.s_stat, fi.e_stat
                FROM m_file_name f
                JOIN m_bridge_file bf ON f.fnid = bf.fnid
                JOIN m_v_main v ON bf.vid = v.vid
                JOIN m_file fi ON bf.fid = fi.fid
                WHERE v.vname = %s AND f.fname NOT LIKE '%/%'
                ORDER BY fi.ftype ASC, f.fname ASC;
                """,
                (version_name,),
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
            SELECT v.vid, v.vname, f.fnid, f.fname, fi.fid, fi.vid_s, fi.vid_e, fi.ftype, fi.s_stat, fi.e_stat
            FROM m_file_name f
            JOIN m_bridge_file bf ON f.fnid = bf.fnid
            JOIN m_v_main v ON bf.vid = v.vid
            JOIN m_file fi ON bf.fid = fi.fid
            WHERE v.vname = %s AND f.fname = %s
            LIMIT 1;
            """,
            (version_name, norm_path),
        )
        file_row = cursor.fetchone()

        # If not a file or if ftype == 0 (Directory instance)
        if file_row is None or file_row[7] == 0:
            prefix = f"{norm_path}/"
            prefix_len = len(prefix) + 1

            # Query immediate child subdirectories
            cursor.execute(
                """
                SELECT DISTINCT SUBSTRING_INDEX(SUBSTRING(f.fname, %s), '/', 1) AS subdir
                FROM m_file_name f
                JOIN m_bridge_file bf ON f.fnid = bf.fnid
                JOIN m_v_main v ON bf.vid = v.vid
                WHERE v.vname = %s AND f.fname LIKE %s AND f.fname LIKE %s
                ORDER BY subdir ASC;
                """,
                (prefix_len, version_name, f"{prefix}%", f"{prefix}%/%"),
            )
            sub_dirs = [f"{norm_path}/{safe_decode(r[0])}" for r in cursor.fetchall() if r[0] is not None]
            sub_dir_set = set(sub_dirs)

            # Query immediate child files under this directory (excluding sub_dirs)
            cursor.execute(
                """
                SELECT f.fname, fi.fid, fi.ftype, fi.s_stat, fi.e_stat
                FROM m_file_name f
                JOIN m_bridge_file bf ON f.fnid = bf.fnid
                JOIN m_v_main v ON bf.vid = v.vid
                JOIN m_file fi ON bf.fid = fi.fid
                WHERE v.vname = %s AND f.fname LIKE %s AND f.fname NOT LIKE %s
                ORDER BY fi.ftype ASC, f.fname ASC;
                """,
                (version_name, f"{prefix}%", f"{prefix}%/%"),
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
        # 3. Path is an active source file -> Fetch full AST tags and spatial maps
        # ---------------------------------------------------------------------
        fid = file_row[4]
        file_meta = {
            "vid": file_row[0],
            "vname": safe_decode(file_row[1]),
            "fnid": file_row[2],
            "fname": safe_decode(file_row[3]),
            "fid": fid,
            "vid_s": file_row[5],
            "vid_e": file_row[6],
            "ftype": file_row[7],
            "s_stat": safe_decode(file_row[8]),
            "e_stat": safe_decode(file_row[9]),
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
            raw_ast = safe_decode(r[14])
            parsed_raw = None
            if raw_ast:
                try:
                    parsed_raw = json.loads(raw_ast)
                except Exception:
                    parsed_raw = raw_ast

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

        cursor.close()
        cnx.close()
        return {
            "type": "file",
            "version": version_name,
            "path": norm_path,
            "file_info": file_meta,
            "tags": tags,
        }

    except Exception as e:
        if cnx and cnx.is_connected():
            cnx.close()
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
            SELECT v.vid, v.vname, f.fnid, f.fname, fi.fid, fi.vid_s, fi.vid_e, fi.ftype, fi.s_stat, fi.e_stat
            FROM m_file fi
            JOIN m_bridge_file bf ON fi.fid = bf.fid
            JOIN m_v_main v ON bf.vid = v.vid
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

        file_meta = {
            "vid": file_row[0],
            "vname": safe_decode(file_row[1]),
            "fnid": file_row[2],
            "fname": safe_decode(file_row[3]),
            "fid": fid,
            "vid_s": file_row[5],
            "vid_e": file_row[6],
            "ftype": file_row[7],
            "s_stat": safe_decode(file_row[8]),
            "e_stat": safe_decode(file_row[9]),
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

        cursor.close()
        cnx.close()
        return {
            "type": "file",
            "file_info": file_meta,
            "tags": tags,
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


@app.get("/api/dev/tables")
def get_dev_table_counts() -> dict[str, Any]:
    """Dev introspection endpoint returning row counts for all schema tables."""
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
            "description": "Inspect row counts across all 14 database schema tables",
            "sample_call": "/api/dev/tables",
        },
    ]


if __name__ == "__main__":
    import uvicorn
    repo_dir = str(Path(__file__).resolve().parent.parent)
    uvicorn.run("webapp.main:app", host="0.0.0.0", port=8000, reload=True, app_dir=repo_dir)
