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
    REF_NO_REF,
    FILE_ERROR,
)
from parser.c_ast.tag_tracker import get_prior_tags as _load_prior_tags
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
    elif CS.file_operation == "D":
        pass  # Tags are immutable content records: a deleted file stages nothing


def get_prior_tags(CS: ChangeSetType) -> None:
    """Previous version's tags of the file with the name/position indices transition matching uses.

    The worker's batch prefetch answers it; Rust records no symbol items, so none are loaded.
    """
    _load_prior_tags(CS, with_indexes=True, with_symbols=False)


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
