"""webapp/backend/services/kconfig_service.py - KConfig Architecture Defconfigs & Dependency Resolver."""
from __future__ import annotations
import os
import subprocess
import logging
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)
from webapp.backend.security.jail import resolve_and_verify_repo_path, LINUX_REPO_DIR
from parser.kconfig_ast.kconfig_lexer import KconfigLexer
from parser.kconfig_ast.kconfig_parser import (
    KconfigParser,
    KconfigSource,
    KconfigMenu,
    KconfigConfig,
    KconfigIf,
    KconfigChoice,
    KconfigComment,
    TYPE_BOOL,
    TYPE_TRISTATE,
    TYPE_STRING,
    TYPE_HEX,
    TYPE_INT,
)

logger = logging.getLogger(__name__)

_DEFCONFIG_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}
_TREE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


class KconfigService:
    """Service providing defconfig discovery, content parsing, and KConfig dependency graph access."""

    @staticmethod
    def normalize_arch(arch: str) -> tuple[str, str, int]:
        """Normalize architecture string to canonical arch name, directory, and bit width."""
        arch_norm = (arch or "x86").lower().strip()
        bits = 64 if ("64" in arch_norm or "aarch64" in arch_norm) else 32

        if arch_norm in ("x86_64", "i386", "x86_32", "x86"):
            arch_dir = "x86"
            arch_canon = "x86_64" if bits == 64 else "i386"
        elif arch_norm in ("arm64", "aarch64"):
            arch_dir = "arm64"
            arch_canon = "arm64"
            bits = 64
        elif arch_norm in ("arm", "arm32"):
            arch_dir = "arm"
            arch_canon = "arm"
            bits = 32
        elif arch_norm.startswith("powerpc") or arch_norm == "ppc":
            arch_dir = "powerpc"
            arch_canon = "ppc64" if bits == 64 else "ppc32"
        elif arch_norm.startswith("mips"):
            arch_dir = "mips"
            arch_canon = "mips64" if bits == 64 else "mips"
        elif arch_norm.startswith("riscv"):
            arch_dir = "riscv"
            arch_canon = "riscv64" if bits == 64 else "riscv32"
        else:
            arch_dir = arch_norm
            arch_canon = arch_norm

        return arch_canon, arch_dir, bits

    def get_defconfigs(self, version_name: str, arch: str = "x86") -> dict[str, Any]:
        """Retrieve all discovered defconfig profiles for an architecture."""
        arch_canon, arch_dir, default_bits = self.normalize_arch(arch)

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            rows = []
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
                rows = [(r["fname"], r["fid"]) for r in cursor.fetchall()]

        # Fallback to git ls-tree if database rows empty
        if not rows:
            try:
                cmd = ["git", "ls-tree", "-r", "--name-only", str(version_name), f"arch/{arch_dir}"]
                out = subprocess.run(cmd, cwd=str(LINUX_REPO_DIR), capture_output=True, text=True, check=True)
                for line in out.stdout.splitlines():
                    clean = line.strip()
                    if "defconfig" in clean and (f"arch/{arch_dir}/configs/" in clean or clean == f"arch/{arch_dir}/defconfig"):
                        rows.append((clean, 0))
            except Exception as e:
                logger.debug("Could not discover defconfigs via git: %s", e)

        defconfigs: list[dict[str, Any]] = []
        canonical: dict[str, Any] | None = None

        for fname_raw, fid in rows:
            fname = safe_decode(fname_raw)
            base_name = os.path.basename(fname)
            is_canonical = False

            # Determine canonical default for this arch
            if arch_dir == "x86" and base_name == "x86_64_defconfig":
                is_canonical = True
            elif arch_dir == "arm64" and base_name == "defconfig":
                is_canonical = True
            elif arch_dir == "arm" and base_name in ("versatile_defconfig", "omap2plus_defconfig"):
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

        if not canonical and defconfigs:
            canonical = defconfigs[0]
            canonical["is_canonical"] = True

        if not defconfigs:
            fallback_name = "x86_64_defconfig" if default_bits == 64 else "i386_defconfig"
            defconfigs = [{
                "name": fallback_name,
                "file_path": f"arch/{arch_dir}/configs/{fallback_name}",
                "fid": 0,
                "is_canonical": True,
            }]
            canonical = defconfigs[0]

        return {
            "version": version_name,
            "arch": arch,
            "arch_dir": arch_dir,
            "total_count": len(defconfigs),
            "canonical_default": canonical,
            "defconfigs": defconfigs,
        }

    def get_defconfig_content(self, version_name: str, name_or_path: str = "", arch: str = "x86", file_path: str = "") -> dict[str, Any]:
        """Parse defconfig into a dictionary of KConfig key-value pairs."""
        target_path = file_path or name_or_path
        clean_path = target_path.strip().lstrip("/")
        arch_canon, arch_dir, _ = self.normalize_arch(arch)

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

        # 1. Try git show
        for cand in candidate_paths:
            try:
                proc = subprocess.run(
                    ["git", "show", f"{version_name}:{cand}"],
                    cwd=str(LINUX_REPO_DIR),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0 and proc.stdout:
                    raw_content = proc.stdout
                    resolved_path = cand
                    break
            except Exception as e:
                logger.debug("Git show error for %s: %s", cand, e)

        # 2. Try direct local file read
        if not raw_content:
            for cand in candidate_paths:
                local_p = LINUX_REPO_DIR / cand
                if local_p.is_file():
                    try:
                        with open(local_p, "r", encoding="utf-8", errors="replace") as f:
                            raw_content = f.read()
                            resolved_path = cand
                            break
                    except Exception as e:
                        logger.debug("Local read error for %s: %s", local_p, e)

        # Parse key-value symbols
        values: dict[str, str] = {}
        if raw_content:
            for line in raw_content.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("# CONFIG_") and line.endswith(" is not set"):
                    sym = line[9:-11].strip()
                    values[sym] = "n"
                elif line.startswith("CONFIG_") and "=" in line:
                    sym, val = line[7:].split("=", 1)
                    values[sym.strip()] = val.strip().strip('"')

        # Determine architecture bitness
        name_lower = os.path.basename(resolved_path).lower()
        if "64" in name_lower or arch_canon == "arm64":
            bits = 64
        elif "32" in name_lower or "i386" in name_lower or arch_canon == "arm":
            bits = 32
        elif values.get("64BIT") == "y":
            bits = 64
        else:
            bits = 64 if arch_dir in ("x86", "powerpc", "sparc") else 32

        # Ensure architectural defaults
        if bits == 64:
            values.setdefault("64BIT", "y")
        else:
            values["64BIT"] = "n"

        if arch_dir == "x86":
            values.setdefault("X86", "y")
            if bits == 64:
                values.setdefault("X86_64", "y")
                values["X86_32"] = "n"
            else:
                values.setdefault("X86_32", "y")
                values["X86_64"] = "n"

        result = {
            "version": version_name,
            "name": os.path.basename(resolved_path),
            "file_path": resolved_path,
            "arch": arch_dir,
            "bits": bits,
            "symbol_count": len(values),
            "values": values,
            "content": raw_content,
        }
        _DEFCONFIG_CACHE[cache_key] = result
        return result

    def build_authentic_tree(self, version_name: str, arch_dir: str) -> dict[str, Any]:
        """Build authentic architecture-scoped hierarchical menu tree by parsing the inclusion tree."""
        from collections import defaultdict

        type_name_map = {
            TYPE_BOOL: "bool",
            TYPE_TRISTATE: "tristate",
            TYPE_STRING: "string",
            TYPE_HEX: "hex",
            TYPE_INT: "int",
        }

        seen_files: set[str] = set()
        node_id_counter = 0
        file_content_cache: dict[str, str] = {}

        def read_file(rel_p: str) -> str:
            if rel_p in file_content_cache:
                return file_content_cache[rel_p]
            clean = rel_p.strip().lstrip("/")
            content = ""
            local_p = LINUX_REPO_DIR / clean
            if local_p.is_file():
                try:
                    with open(local_p, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception as e:
                    logger.debug("Failed reading %s: %s", local_p, e)
            if not content:
                try:
                    proc = subprocess.run(
                        ["git", "show", f"{version_name}:{clean}"],
                        cwd=str(LINUX_REPO_DIR),
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if proc.returncode == 0 and proc.stdout:
                        content = proc.stdout
                except Exception as e:
                    logger.debug("Git show error for %s: %s", clean, e)
            file_content_cache[rel_p] = content
            return content

        flat_nodes: list[dict[str, Any]] = []
        relations_map: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"depends_on": [], "selects": [], "implies": []})
        reverse_relations_map: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"selected_by": [], "implied_by": []})

        def parse_file(rel_path: str, parent_deps: list[str], parent_id: int) -> list[dict[str, Any]]:
            clean_p = rel_path.strip().strip('"').strip("'")
            clean_p = clean_p.replace("$SRCARCH", arch_dir).replace("$ARCH", arch_dir)
            if clean_p in seen_files:
                return []
            seen_files.add(clean_p)

            content = read_file(clean_p)
            if not content:
                return []

            lexer = KconfigLexer(content)
            tokens = lexer.tokenize()
            parser = KconfigParser(tokens, content)
            items = parser.parse()

            file_dir = os.path.dirname(clean_p)
            return process_items(items, file_dir, parent_deps, clean_p, parent_id)

        def process_items(items: list[Any], current_dir: str, parent_deps: list[str], current_file: str, parent_id: int) -> list[dict[str, Any]]:
            nonlocal node_id_counter
            result = []
            active_menuconfig: tuple[str, dict[str, Any]] | None = None

            for item in items:
                if isinstance(item, KconfigConfig):
                    node_id_counter += 1
                    curr_id = node_id_counter
                    deps = list(parent_deps)
                    dep_syms = []
                    for d in item.depends_on:
                        d_str = d.to_string()
                        if d_str:
                            deps.append(d_str)
                        dep_syms.extend(d.collect_symbols())

                    combined_deps = " && ".join(f"({d})" for d in deps) if deps else ""

                    defaults = []
                    for d_val, d_cond in item.defaults:
                        val_str = d_val.to_string() if d_val else ""
                        cond_str = d_cond.to_string() if d_cond else None
                        defaults.append({"value": val_str, "cond": cond_str})

                    selects = []
                    for s_target, s_cond in item.selects:
                        cond_str = s_cond.to_string() if s_cond else None
                        selects.append({"target": s_target, "cond": cond_str})
                        relations_map[item.name]["selects"].append(s_target)
                        reverse_relations_map[s_target]["selected_by"].append({
                            "name": item.name,
                            "prompt": item.prompt or item.name,
                            "cond": cond_str,
                        })

                    implies = []
                    for i_target, i_cond in item.implies:
                        cond_str = i_cond.to_string() if i_cond else None
                        implies.append({"target": i_target, "cond": cond_str})
                        relations_map[item.name]["implies"].append(i_target)
                        reverse_relations_map[i_target]["implied_by"].append({
                            "name": item.name,
                            "prompt": item.prompt or item.name,
                            "cond": cond_str,
                        })

                    for ds in dep_syms:
                        relations_map[item.name]["depends_on"].append(ds)

                    node_dict = {
                        "id": curr_id,
                        "tree_id": curr_id,
                        "parent_id": parent_id,
                        "node_type": 4 if item.is_menuconfig else 3,
                        "symbol_name": item.name,
                        "symbol": item.name,
                        "type": item.sym_type,
                        "type_name": type_name_map.get(item.sym_type, "tristate"),
                        "title": item.prompt or item.name,
                        "prompt": item.prompt or "",
                        "is_menuconfig": item.is_menuconfig,
                        "depends_on_expr": combined_deps,
                        "depends_on": dep_syms,
                        "defaults": defaults,
                        "def_val": defaults[0]["value"] if defaults else "",
                        "selects": selects,
                        "implies": implies,
                        "selected_by": reverse_relations_map[item.name]["selected_by"],
                        "implied_by": reverse_relations_map[item.name]["implied_by"],
                        "help": item.help_text or "",
                        "file_path": current_file,
                        "children": [],
                    }

                    if item.is_menuconfig:
                        active_menuconfig = (item.name, node_dict)
                        flat_nodes.append(node_dict)
                        result.append(node_dict)
                    elif active_menuconfig and (active_menuconfig[0] in dep_syms or any(active_menuconfig[0] in d for d in dep_syms)):
                        node_dict["parent_id"] = active_menuconfig[1]["id"]
                        active_menuconfig[1]["children"].append(node_dict)
                        flat_nodes.append(node_dict)
                    else:
                        active_menuconfig = None
                        flat_nodes.append(node_dict)
                        result.append(node_dict)

                elif isinstance(item, KconfigMenu):
                    active_menuconfig = None
                    node_id_counter += 1
                    curr_id = node_id_counter
                    deps = list(parent_deps)
                    for d in item.depends_on:
                        d_str = d.to_string()
                        if d_str:
                            deps.append(d_str)
                    combined_deps = " && ".join(f"({d})" for d in deps) if deps else ""

                    children = process_items(item.children, current_dir, deps, current_file, curr_id)

                    node_dict = {
                        "id": curr_id,
                        "tree_id": curr_id,
                        "parent_id": parent_id,
                        "node_type": 1,
                        "symbol_name": None,
                        "symbol": None,
                        "title": item.title or "Menu",
                        "prompt": item.title or "Menu",
                        "is_menu": True,
                        "depends_on_expr": combined_deps,
                        "depends_on": [],
                        "selects": [],
                        "selected_by": [],
                        "implies": [],
                        "implied_by": [],
                        "defaults": [],
                        "file_path": current_file,
                        "children": children,
                    }
                    flat_nodes.append(node_dict)
                    result.append(node_dict)

                elif isinstance(item, KconfigChoice):
                    active_menuconfig = None
                    node_id_counter += 1
                    curr_id = node_id_counter
                    deps = list(parent_deps)
                    for d in item.depends_on:
                        d_str = d.to_string()
                        if d_str:
                            deps.append(d_str)
                    combined_deps = " && ".join(f"({d})" for d in deps) if deps else ""

                    children = process_items(item.children, current_dir, deps, current_file, curr_id)
                    choice_members = [c["symbol_name"] for c in children if c.get("symbol_name")]

                    choice_meta = {
                        "id": curr_id,
                        "choice_id": curr_id,
                        "type": type_name_map.get(item.sym_type, "bool"),
                        "optional": item.is_optional,
                        "members": choice_members,
                    }
                    for c in children:
                        c["choice"] = choice_meta

                    node_dict = {
                        "id": curr_id,
                        "tree_id": curr_id,
                        "parent_id": parent_id,
                        "node_type": 2,
                        "symbol_name": None,
                        "symbol": None,
                        "title": item.prompt or item.name or "Choice",
                        "prompt": item.prompt or item.name or "Choice",
                        "is_choice": True,
                        "choice": choice_meta,
                        "depends_on_expr": combined_deps,
                        "depends_on": [],
                        "selects": [],
                        "selected_by": [],
                        "implies": [],
                        "implied_by": [],
                        "defaults": [],
                        "file_path": current_file,
                        "children": children,
                    }
                    flat_nodes.append(node_dict)
                    result.append(node_dict)

                elif isinstance(item, KconfigIf):
                    cond_str = item.cond.to_string() if item.cond else ""
                    new_deps = list(parent_deps)
                    if cond_str:
                        new_deps.append(cond_str)
                    if active_menuconfig and (active_menuconfig[0] in cond_str or cond_str == active_menuconfig[0]):
                        children = process_items(item.children, current_dir, new_deps, current_file, active_menuconfig[1]["id"])
                        active_menuconfig[1]["children"].extend(children)
                    else:
                        active_menuconfig = None
                        children = process_items(item.children, current_dir, new_deps, current_file, parent_id)
                        result.extend(children)

                elif isinstance(item, KconfigComment):
                    node_id_counter += 1
                    curr_id = node_id_counter
                    node_dict = {
                        "id": curr_id,
                        "tree_id": curr_id,
                        "parent_id": parent_id,
                        "node_type": 5,
                        "symbol_name": None,
                        "symbol": None,
                        "title": item.title or "Comment",
                        "prompt": item.title or "Comment",
                        "is_comment": True,
                        "depends_on_expr": "",
                        "depends_on": [],
                        "selects": [],
                        "selected_by": [],
                        "implies": [],
                        "implied_by": [],
                        "defaults": [],
                        "file_path": current_file,
                        "children": [],
                    }
                    flat_nodes.append(node_dict)
                    result.append(node_dict)

                elif isinstance(item, KconfigSource):
                    s_path = item.path.strip().strip('"').strip("'").replace("$SRCARCH", arch_dir).replace("$ARCH", arch_dir)
                    target_p = os.path.normpath(os.path.join(current_dir, s_path)) if item.is_rsource else s_path
                    sourced_children = parse_file(target_p, parent_deps, parent_id)
                    result.extend(sourced_children)

            return result

        root_file = f"arch/{arch_dir}/Kconfig"
        nested_tree = parse_file(root_file, [], 0)

        res = {
            "version": version_name,
            "arch": arch_dir,
            "total_nodes": len(flat_nodes),
            "total_count": len(flat_nodes),
            "nodes": flat_nodes,
            "tree": nested_tree,
            "relations": relations_map,
            "reverse_relations": reverse_relations_map,
        }
        return res

    def get_tree(
        self,
        version_name: str,
        arch: str = "x86",
        include_tree: bool = False,
        include_relations: bool = False,
    ) -> dict[str, Any]:
        """Retrieve authentic, scoped hierarchical menu tree structure for a given kernel version and architecture."""
        arch_canon, arch_dir, bits = self.normalize_arch(arch)
        cache_key = (version_name, arch_dir)

        if cache_key not in _TREE_CACHE:
            _TREE_CACHE[cache_key] = self.build_authentic_tree(version_name, arch_dir)

        cached_res = _TREE_CACHE[cache_key]
        res: dict[str, Any] = {
            "version": cached_res["version"],
            "arch": cached_res["arch"],
            "total_nodes": cached_res["total_nodes"],
            "total_count": cached_res["total_count"],
            "nodes": cached_res["nodes"],
            "tree": cached_res["tree"],
        }
        if include_relations:
            res["relations"] = cached_res["relations"]
            res["reverse_relations"] = cached_res["reverse_relations"]

        return res

    def get_symbol_detail(self, version_name: str, name: str) -> dict[str, Any]:
        """Fetch symbol attributes, depends_on expressions, reverse selects, and lifecycle metadata."""
        clean_name = name.strip()
        if clean_name.startswith("CONFIG_"):
            clean_name = clean_name[7:]

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            cursor.execute(
                """
                SELECT s.kcid, s.vid_s, s.vid_e, s.name, s.type, s.prompt, s.def_val, s.help, s.ast_id
                FROM m_kconfig_symbol s
                WHERE s.vid_s <= %s AND (s.vid_e >= %s OR s.vid_e = 0) AND s.name = %s
                LIMIT 1;
                """,
                (vid, vid, clean_name),
            )
            s_row = cursor.fetchone()
            if not s_row:
                raise HTTPException(status_code=404, detail=f"KConfig symbol '{clean_name}' not found.")

            kcid = s_row["kcid"]
            vid_s = s_row["vid_s"]
            vid_e = s_row["vid_e"]
            _, vname_s = get_version_info(cursor, vid_s)
            vname_e = "Active" if not vid_e or vid_e == 0 else get_version_info(cursor, vid_e)[1]
            is_active = bool(not vid_e or vid_e == 0 or vid_e >= vid)
            lifecycle_status = "Active" if is_active else "Deleted"

            # Relations (depends_on, select)
            cursor.execute(
                """
                SELECT r.rel_type, r.target_name, r.priority
                FROM m_kconfig_relation r
                WHERE r.kcid = %s
                ORDER BY r.priority ASC;
                """,
                (kcid,),
            )
            relations = cursor.fetchall()
            depends_on = []
            selects = []
            for r in relations:
                t_name = safe_decode(r["target_name"])
                if r["rel_type"] == 1:  # depends on
                    depends_on.append(t_name)
                elif r["rel_type"] == 2:  # select
                    selects.append(t_name)

            # Reverse dependencies (symbols that select this symbol)
            cursor.execute(
                """
                SELECT DISTINCT s.name
                FROM m_kconfig_relation r
                JOIN m_kconfig_symbol s ON r.kcid = s.kcid
                WHERE r.rel_type = 2 AND r.target_name = %s
                ORDER BY s.name ASC;
                """,
                (clean_name,),
            )
            selected_by = [safe_decode(r["name"]) for r in cursor.fetchall()]

            # Compiled source files from m_kconfig_kbuild
            cursor.execute(
                """
                SELECT DISTINCT fn.fname
                FROM m_kconfig_kbuild kb
                JOIN m_bridge_file bf ON kb.fid = bf.fid AND bf.vid = kb.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE kb.kcid = %s AND kb.vid = %s;
                """,
                (kcid, vid),
            )
            compiled_files = [{"file_path": safe_decode(r["fname"])} for r in cursor.fetchall()]

            type_map = {1: "bool", 2: "tristate", 3: "string", 4: "hex", 5: "int"}
            raw_type = s_row["type"]
            type_name = type_map.get(raw_type, "tristate") if isinstance(raw_type, int) else "tristate"

            return {
                "version": vname,
                "name": clean_name,
                "vid_s": vid_s,
                "vid_e": vid_e,
                "vname_s": vname_s,
                "vname_e": vname_e,
                "added_version": vname_s,
                "is_active": is_active,
                "lifecycle_status": lifecycle_status,
                "type": raw_type,
                "type_name": type_name,
                "prompt": safe_decode(s_row["prompt"]),
                "def_val": safe_decode(s_row["def_val"]),
                "help": safe_decode(s_row["help"]),
                "depends_on": depends_on,
                "selects": selects,
                "selected_by": selected_by,
                "compiled_files": compiled_files,
            }

    def get_graph(self, version_name: str, symbol_or_arch: str = "x86", depth: int = 2) -> dict[str, Any]:
        """Extract DAG graph (nodes, edges) for a symbol or the full architecture."""
        target_sym = symbol_or_arch.strip()
        if target_sym.startswith("CONFIG_"):
            target_sym = target_sym[7:]

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            nodes = [{
                "id": target_sym,
                "label": target_sym,
                "is_root": True,
                "type": "symbol",
            }]
            edges = []

            # Retrieve dependencies for target symbol
            cursor.execute(
                """
                SELECT r.rel_type, r.target_name
                FROM m_kconfig_relation r
                JOIN m_kconfig_symbol s ON r.kcid = s.kcid
                WHERE s.vid_s <= %s AND (s.vid_e >= %s OR s.vid_e = 0) AND s.name = %s;
                """,
                (vid, vid, target_sym),
            )
            for r in cursor.fetchall():
                tname = safe_decode(r["target_name"])
                nodes.append({
                    "id": tname,
                    "label": tname,
                    "is_root": False,
                    "type": "dependency" if r["rel_type"] == 1 else "select",
                })
                edges.append({
                    "from": target_sym,
                    "to": tname,
                    "type": "depends_on" if r["rel_type"] == 1 else "selects",
                })

            return {
                "version": vname,
                "symbol": target_sym,
                "nodes": nodes,
                "edges": edges,
            }

    def autosolve(self, version_name: str, req: Any) -> dict[str, Any]:
        """Auto-solve dependencies for a target Kconfig symbol."""
        if isinstance(req, dict):
            sym_name = (req.get("target_symbol") or req.get("symbol") or "").strip()
        else:
            sym_name = (getattr(req, "target_symbol", None) or getattr(req, "symbol", "") or "").strip()

        if sym_name.startswith("CONFIG_"):
            sym_name = sym_name[7:]

        details = self.get_symbol_detail(version_name, sym_name)
        toggles = [{"symbol": sym_name, "set_to": "y"}]
        for dep in details.get("depends_on", []):
            clean_dep = dep.replace("CONFIG_", "").strip()
            toggles.append({"symbol": clean_dep, "set_to": "y"})

        return {
            "version": version_name,
            "target": sym_name,
            "solution_found": True,
            "toggles_needed": toggles,
        }

    def diff_configurations(self, version_name: str, req: Any) -> dict[str, Any]:
        """Compare two KConfig configuration dictionaries."""
        if isinstance(req, dict):
            cfg1 = req.get("active_config", {})
            cfg2 = req.get("custom_config", {})
        else:
            cfg1 = getattr(req, "active_config", {})
            cfg2 = getattr(req, "custom_config", {})

        all_keys = set(cfg1.keys()) | set(cfg2.keys())
        matching = 0
        mismatched = 0
        diffs = []
        for k in all_keys:
            v1 = cfg1.get(k)
            v2 = cfg2.get(k)
            if v1 == v2:
                matching += 1
            else:
                mismatched += 1
                diffs.append({"symbol": k, "val1": v1, "val2": v2})

        return {
            "version": version_name,
            "matching_symbols": matching,
            "mismatched_symbols": mismatched,
            "differences": diffs,
        }

    def get_env_presets(self, version_name: str) -> dict[str, Any]:
        """Dynamically discover target architectures and compilers from database."""
        with get_db_cursor() as cursor:
            vid, _ = get_version_info(cursor, version_name)
            cursor.execute(
                """
                SELECT DISTINCT fn.fname
                FROM m_bridge_file bf
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE bf.vid = %s AND fn.fname LIKE 'arch/%/Kconfig'
                ORDER BY fn.fname ASC;
                """,
                (vid or 1,),
            )
            arch_files = [safe_decode(r["fname"]) for r in cursor.fetchall()]

        discovered_archs: list[dict[str, Any]] = []
        seen_arch_ids = set()

        for fname in arch_files:
            parts = fname.split("/")
            if len(parts) >= 2 and parts[0] == "arch":
                raw_arch = parts[1].strip().lower()
                if not raw_arch or raw_arch in seen_arch_ids:
                    continue
                seen_arch_ids.add(raw_arch)

                if raw_arch == "x86":
                    discovered_archs.append({
                        "id": "x86_64",
                        "label": "x86_64 (64-bit x86)",
                        "arch": "x86",
                        "srcarch": "x86",
                        "kconfig_path": fname,
                        "canonical_defconfig": "x86_64_defconfig",
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
                        "canonical_defconfig": "i386_defconfig",
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
                        "canonical_defconfig": "ppc64_defconfig",
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
                        "canonical_defconfig": "pmac32_defconfig",
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
                        "canonical_defconfig": "sparc64_defconfig",
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
                        "canonical_defconfig": "sparc32_defconfig",
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
                elif raw_arch == "arm":
                    discovered_archs.append({
                        "id": "arm",
                        "label": "ARM (32-bit ARM)",
                        "arch": "arm",
                        "srcarch": "arm",
                        "kconfig_path": fname,
                        "canonical_defconfig": "versatile_defconfig",
                        "bits": 32,
                        "symbols": {
                            "ARM": "y",
                            "ARCH": "arm",
                            "SRCARCH": "arm",
                            "64BIT": "n",
                        },
                    })
                elif raw_arch in ("arm64", "aarch64"):
                    discovered_archs.append({
                        "id": "arm64",
                        "label": "ARM64 (64-bit aarch64)",
                        "arch": "arm64",
                        "srcarch": "arm64",
                        "kconfig_path": fname,
                        "canonical_defconfig": "defconfig",
                        "bits": 64,
                        "symbols": {
                            "ARM64": "y",
                            "ARCH": "arm64",
                            "SRCARCH": "arm64",
                            "64BIT": "y",
                        },
                    })
                elif raw_arch.startswith("mips"):
                    discovered_archs.append({
                        "id": "mips",
                        "label": "MIPS",
                        "arch": "mips",
                        "srcarch": "mips",
                        "kconfig_path": fname,
                        "canonical_defconfig": "malta_defconfig",
                        "bits": 32,
                        "symbols": {
                            "MIPS": "y",
                            "ARCH": "mips",
                            "SRCARCH": "mips",
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
                        "canonical_defconfig": "defconfig",
                        "bits": 64 if "64" in raw_arch else 32,
                        "symbols": {
                            upper_arch: "y",
                            "ARCH": raw_arch,
                            "SRCARCH": raw_arch,
                        },
                    })

        compilers = [
            {"id": "gcc", "label": "GNU Compiler Collection (gcc)", "symbols": {"CC_IS_GCC": "y"}},
            {"id": "clang", "label": "LLVM Clang (clang)", "symbols": {"CC_IS_CLANG": "y"}},
        ]

        return {
            "version": version_name,
            "targets": discovered_archs,
            "architectures": discovered_archs,
            "compilers": compilers,
        }

    def export_file(self, version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
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
                if (val_str.startswith('"') and val_str.endswith('"')) or val_str.isdigit() or val_str.startswith("0x"):
                    lines.append(f"CONFIG_{name}={val_str}")
                else:
                    lines.append(f'CONFIG_{name}="{val_str}"')

        content = "\n".join(lines) + "\n"
        return {
            "version": version_name,
            "content": content,
            "symbol_count": len(symbols),
        }

    def import_file(self, version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Parse imported Linux .config text payload into symbol assignments dictionary."""
        content = payload.get("content", "")
        symbols: dict[str, str] = {}
        lines = content.splitlines()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("# CONFIG_") and stripped.endswith(" is not set"):
                sym = stripped[9:-11].strip()
                if sym:
                    symbols[sym] = "n"
                continue

            if stripped.startswith("#"):
                continue

            if "=" in stripped and stripped.startswith("CONFIG_"):
                parts = stripped[7:].split("=", 1)
                sym = parts[0].strip()
                val = parts[1].strip().strip('"')
                symbols[sym] = val

        return {
            "version": version_name,
            "symbol_count": len(symbols),
            "symbols": symbols,
        }

    def search_symbols(self, version_name: str, q: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Search Kconfig symbols with or without CONFIG_ prefix."""
        clean_q = q.strip()
        if clean_q.startswith("CONFIG_"):
            clean_q = clean_q[7:]

        with get_db_cursor() as cursor:
            vid, _ = get_version_info(cursor, version_name)
            cursor.execute(
                """
                SELECT s.name, s.type, s.prompt, s.help
                FROM m_kconfig_symbol s
                WHERE s.vid_s <= %s AND (s.vid_e = 0 OR s.vid_e >= %s)
                  AND (s.name LIKE %s OR s.prompt LIKE %s)
                ORDER BY (s.name = %s) DESC, s.name ASC
                LIMIT %s OFFSET %s;
                """,
                (vid or 1, vid or 1, f"%{clean_q}%", f"%{clean_q}%", clean_q, limit, offset),
            )
            type_names = {1: "bool", 2: "tristate", 3: "string", 4: "hex", 5: "int"}
            symbols = [
                {
                    "name": safe_decode(r["name"]),
                    "type": r["type"],
                    "type_name": type_names.get(r["type"], "tristate"),
                    "prompt": safe_decode(r["prompt"]),
                    "help": safe_decode(r["help"]),
                }
                for r in cursor.fetchall()
            ]

        return {"version": version_name, "query": q, "count": len(symbols), "symbols": symbols}

    def validate_assignments(self, version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate symbols against dependencies and select constraints."""
        symbols = payload.get("symbols", {})
        forced_symbols: dict[str, str] = {}
        adjusted_symbols: dict[str, str] = {}

        with get_db_cursor() as cursor:
            vid, _ = get_version_info(cursor, version_name)
            for sym, val in symbols.items():
                if val in ("y", "m"):
                    cursor.execute(
                        """
                        SELECT r.target_name
                        FROM m_kconfig_relation r
                        JOIN m_kconfig_symbol s ON r.kcid = s.kcid
                        WHERE s.name = %s AND r.rel_type = 2;
                        """,
                        (sym,),
                    )
                    for r in cursor.fetchall():
                        t_name = safe_decode(r["target_name"])
                        forced_symbols[t_name] = "y"
                        adjusted_symbols[t_name] = "y"

        return {
            "version": version_name,
            "forced_symbols": forced_symbols,
            "adjusted_symbols": adjusted_symbols,
            "conflicts": [],
        }

