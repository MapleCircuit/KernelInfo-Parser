"""parser/rust_ast/rust_ast.py - Rust AST Parser Orchestrator & Lifecycle Manager.

Orchestrates parsing of Rust (.rs) source files using rustc AST extraction,
constructs intermediate AST node trees, maps relational database operations into
ChangeSets, and manages cross-version tag lifecycle and recycling.
"""
from __future__ import annotations

import os
import subprocess
import logging
from pathlib import Path
from typing import Any

from core.globalstuff import (
    G,
    COLOR,
    REF_ROOT,
    REF_OLD,
    REF_NO_REF,
    REF_POS,
    FILE_ERROR,
    ASTT,
)
from core.DBLayout import (
    m_file_name,
    m_file,
    m_bridge_file,
    m_tag,
    m_bridge_tag,
)
from parser.rust_ast.rust_ast_type import ChangeSetType
from parser.rust_ast.rust_tree_parser import parse_rust_ast_tree

logger = logging.getLogger(__name__)


def rust_ast_parse(CS: ChangeSetType) -> None:
    """Entry point for parsing Rust source files into ChangeSet operations."""
    if CS.file_operation == "R100":
        return
    elif CS.file_operation == "A":
        Rust_Manager(CS)
    elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation.startswith("R")):
        get_prior_tags(CS)
        Rust_Manager(CS)
        close_prior_tags(CS)
    elif CS.file_operation == "D":
        get_prior_tags(CS)
        close_prior_tags(CS)


def get_prior_tags(CS: ChangeSetType) -> None:
    """Query TableEngine for existing active AST tags registered in the previous version."""
    CS.active_tag_list = set()
    CS.prior_tags = None
    CS.prior_tags_map = {}

    old_vid = getattr(CS.gp, "Old_VID", 0)
    if old_vid <= 0:
        return

    lookup_path = CS.old_path if (CS.file_operation and CS.file_operation.startswith("R") and CS.old_path) else CS.current_path
    fn_row = m_file_name.get(None, lookup_path)
    if not fn_row or len(fn_row) < 3 or not fn_row[2]:
        return
    fnid = fn_row[2][0]

    bf_row = m_bridge_file.get(old_vid, fnid, None)
    if not bf_row or len(bf_row) < 3 or not bf_row[2]:
        return
    old_fid = bf_row[2][2]

    CS.prior_tags = m_bridge_tag.view_get_multiple(
        ((m_bridge_tag.tag_id, m_tag.tag_id, 1),),
        old_fid,
        None,  # 1: m_bridge_tag.tag_id
        None,  # 2: m_bridge_tag.line_s
        None,  # 3: m_bridge_tag.line_e
        None,  # 4: m_bridge_tag.char_s
        None,  # 5: m_bridge_tag.char_e
        None,  # 6: m_tag.tag_id
        None,  # 7: m_tag.vid_s
        None,  # 8: m_tag.vid_e
        None,  # 9: m_tag.code
        None,  # 10: m_tag.ast_id
        None,  # 11: m_tag.hl_s
        None,  # 12: m_tag.hl_l
    )
    if CS.prior_tags:
        CS.prior_tags_map = {}
        for x, tag in enumerate(CS.prior_tags):
            if len(tag) > 9:
                code = tag[9]
                if code is not None:
                    if code not in CS.prior_tags_map:
                        CS.prior_tags_map[code] = []
                    tag_id = tag[1] if len(tag) > 1 else tag[0]
                    CS.prior_tags_map[code].append((x, tag_id))


def close_prior_tags(CS: ChangeSetType) -> None:
    """Mark prior version tags as closed/inactive if they were not recycled in current version."""
    if CS.prior_tags:
        with CS(REF_OLD):
            for x, tag in enumerate(CS.prior_tags):
                if x in CS.active_tag_list:
                    continue
                if len(tag) >= 13:
                    CS.store(m_tag.update(
                        tag[6],          # m_tag.tag_id
                        tag[7],          # m_tag.vid_s
                        CS.gp.Old_VID,   # m_tag.vid_e
                        tag[9],          # m_tag.code
                        tag[10],         # m_tag.ast_id
                        tag[11],         # m_tag.hl_s
                        tag[12],         # m_tag.hl_l
                    ))


class Rust_Manager:
    """Manages invocation of rustc AST extractor and ChangeSet extraction."""

    def __init__(self, CS: ChangeSetType) -> None:
        self.mfdir = CS.mf.version_dict[CS.gp.Version_Name]
        self.filename = CS.current_path
        self.fullfilename = f"{self.mfdir}/{self.filename}"
        G.CURRENT_PARSING_FILE = self.filename
        self.children: list[Any] = []
        CS.parsers["RUST_RM"] = self
        self.init_parse(CS)

    def init_parse(self, CS: ChangeSetType) -> None:
        """Read source file, run rustc AST extraction, and extract nodes to ChangeSet."""
        try:
            unsplit_rawfile = Path(self.fullfilename).read_text(encoding="latin-1")
        except Exception as e:
            raise FILE_ERROR(e) from e

        self.rawfile = tuple(unsplit_rawfile.split("\n"))

        env = os.environ.copy()
        env["RUSTC_BOOTSTRAP"] = "1"

        try:
            proc = subprocess.run(
                [
                    "rustc",
                    "--edition=2021",
                    "--crate-type=lib",
                    "-Z", "unpretty=ast-tree",
                    "-o", "-",
                    self.fullfilename,
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            ast_text = proc.stdout if (proc.returncode == 0 or "Crate {" in proc.stdout) else ""
        except Exception as e:
            logger.error(f"Failed to execute rustc on {self.fullfilename}: {e}")
            ast_text = ""

        if ast_text:
            self.children = parse_rust_ast_tree(ast_text, self.rawfile)

        self.extract(CS)

    def extract(self, CS: ChangeSetType) -> None:
        """Extract all top-level AST nodes to ChangeSet operations."""
        for item in self.children:
            with CS(REF_NO_REF):
                item.extract(CS)
