"""Kbuild / Makefile Parser for Linux Kernel Build System.

Extracts compilation goals, composite object definitions, and maps
Kconfig configuration symbols (CONFIG_*) to compiled source files (.c, .h, .S).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class KbuildBinding:
    """Represents a compiled source file binding associated with a Kconfig symbol."""
    symbol_name: str  # without CONFIG_ prefix, or "y" for core built-in, "m" for module
    compile_mode: int  # 1: built-in (y), 2: module (m), 3: conditional (y/m)
    target_obj: str  # e.g. "ext4.o", "drbd.o"
    source_file_rel: str  # e.g. "fs/ext4/balloc.c"
    is_composite: bool = False


class KbuildParser:
    """Deterministic parser for Linux Makefile and Kbuild syntax."""

    # Matches obj-$(CONFIG_XYZ) += ... or obj-y += ... or obj-m += ...
    OBJ_ASSIGN_RE = re.compile(
        r"^(?:obj-\$\(CONFIG_([A-Za-z0-9_]+)\)|obj-([ym]))\s*([:+?]?=)\s*(.*)$",
        re.MULTILINE,
    )

    # Matches composite target-y := ... or target-objs := ...
    COMPOSITE_RE = re.compile(
        r"^([A-Za-z0-9_\-]+)-(?:y|objs|\$\(CONFIG_([A-Za-z0-9_]+)\))\s*([:+?]?=)\s*(.*)$",
        re.MULTILINE,
    )

    def __init__(self, base_dir: str = "") -> None:
        self.base_dir = base_dir.replace("\\", "/").rstrip("/")

    def parse_makefile_content(self, content: str, dir_path: str = "") -> list[KbuildBinding]:
        """Parse the content of a Makefile or Kbuild file and extract symbol-to-source bindings.

        Args:
            content: Raw string content of Makefile / Kbuild.
            dir_path: Relative directory containing the Makefile (e.g. "fs/ext4").

        Returns:
            List of KbuildBinding objects.
        """
        clean_dir = dir_path.replace("\\", "/").strip("/")
        
        # 1. Join line continuations (lines ending with \)
        joined_lines = []
        current_line = []
        for line in content.splitlines():
            stripped = line.rstrip()
            if stripped.endswith("\\"):
                current_line.append(stripped[:-1].rstrip())
            else:
                current_line.append(stripped)
                joined_lines.append(" ".join(current_line))
                current_line = []
        if current_line:
            joined_lines.append(" ".join(current_line))

        # 2. Extract composite object mappings: target_name -> list of constituent .o files
        composites: dict[str, list[tuple[str, str | None]]] = {}
        for line in joined_lines:
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            m = self.COMPOSITE_RE.match(line_str)
            if m:
                target_base = m.group(1)
                sub_cond = m.group(2)  # if composite had $(CONFIG_...)
                vals = m.group(4).split()
                if target_base not in composites:
                    composites[target_base] = []
                for v in vals:
                    clean_v = v.strip()
                    if clean_v.endswith(".o"):
                        composites[target_base].append((clean_v, sub_cond))

        # 3. Extract obj-* rules
        bindings: list[KbuildBinding] = []
        for line in joined_lines:
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue

            m = self.OBJ_ASSIGN_RE.match(line_str)
            if m:
                config_sym = m.group(1)
                direct_mode = m.group(2)  # 'y' or 'm'
                items = m.group(4).split()

                if config_sym:
                    sym_name = config_sym
                    mode = 3  # conditional on CONFIG_
                elif direct_mode == "y":
                    sym_name = "y"
                    mode = 1
                elif direct_mode == "m":
                    sym_name = "m"
                    mode = 2
                else:
                    continue

                for item in items:
                    clean_item = item.strip()
                    if clean_item.endswith("/"):
                        # Subdirectory recursion: e.g. ethernet/
                        continue
                    elif clean_item.endswith(".o"):
                        target_base = clean_item[:-2]
                        if target_base in composites:
                            # It's a composite object composed of multiple .o files
                            for comp_obj, sub_sym in composites[target_base]:
                                src_base = comp_obj[:-2]
                                src_c = f"{clean_dir}/{src_base}.c" if clean_dir else f"{src_base}.c"
                                bound_sym = sub_sym if sub_sym else sym_name
                                bindings.append(KbuildBinding(
                                    symbol_name=bound_sym,
                                    compile_mode=mode,
                                    target_obj=clean_item,
                                    source_file_rel=src_c,
                                    is_composite=True,
                                ))
                        else:
                            # Direct object -> .c file
                            src_c = f"{clean_dir}/{target_base}.c" if clean_dir else f"{target_base}.c"
                            bindings.append(KbuildBinding(
                                symbol_name=sym_name,
                                compile_mode=mode,
                                target_obj=clean_item,
                                source_file_rel=src_c,
                                is_composite=False,
                            ))

        return bindings


def extract_kbuild_file_map(
    version_name: str,
    cursor: Any,
    vid: int,
) -> dict[str, list[dict[str, Any]]]:
    """Dynamically resolve compiled source files for all Kconfig symbols in a given kernel version.

    Args:
        version_name: Tag name of the version (e.g. 'v3.0').
        cursor: Database cursor.
        vid: Resolved Version ID.

    Returns:
        Dictionary mapping symbol name (without CONFIG_) to list of compiled file records.
    """
    # 1. Query all Makefile and Kbuild files for this version
    cursor.execute(
        """
        SELECT fn.fname, f.fid
        FROM m_bridge_file bf
        JOIN m_file_name fn ON bf.fnid = fn.fnid
        JOIN m_file f ON bf.fid = f.fid
        WHERE bf.vid = %s AND (fn.fname LIKE '%Makefile' OR fn.fname LIKE '%Kbuild')
        ORDER BY fn.fname ASC;
        """,
        (vid,),
    )
    makefile_rows = cursor.fetchall()

    # Query all .c, .h, .S files registered for this version to resolve fids
    cursor.execute(
        """
        SELECT fn.fname, f.fid
        FROM m_bridge_file bf
        JOIN m_file_name fn ON bf.fnid = fn.fnid
        JOIN m_file f ON bf.fid = f.fid
        WHERE bf.vid = %s AND (fn.fname LIKE '%.c' OR fn.fname LIKE '%.h' OR fn.fname LIKE '%.S');
        """,
        (vid,),
    )
    src_file_rows = cursor.fetchall()
    fid_by_path = {row[0]: row[1] for row in src_file_rows}

    # Also build basename lookup (e.g. "w83627hf_wdt.c" -> [("drivers/watchdog/w83627hf_wdt.c", fid)])
    fids_by_basename: dict[str, list[tuple[str, int]]] = {}
    for p, fid in fid_by_path.items():
        base = os.path.basename(p)
        if base not in fids_by_basename:
            fids_by_basename[base] = []
        fids_by_basename[base].append((p, fid))

    return {
        "fid_by_path": fid_by_path,
        "fids_by_basename": fids_by_basename,
    }
