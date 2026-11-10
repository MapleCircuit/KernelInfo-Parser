"""parser/rust_ast/rust_ast.py - Rust AST Parser Orchestrator & Lifecycle Manager.

Runs `rustc -Z unpretty=ast-tree` on a Rust (.rs) source file, builds the `Ast_Rust` node tree
(parser/rust_ast/rust_tree_parser.py) and maps it into ChangeSet operations (parser/rust_ast/rust_ast_type.py).
A file rustc cannot dump (missing rustc, a syntax it no longer accepts, a timeout) is tagged whole by the raw
parser instead, so it still has a tag and a history.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from core.globalstuff import G, REF_NO_REF, FILE_ERROR
from core.SymbolRows import finish as finish_symbol_rows
from parser.c_ast.tag_tracker import get_prior_tags as _load_prior_tags
from parser.rust_ast.rust_ast_type import ChangeSetType
from parser.rust_ast.rust_tree_parser import parse_rust_ast_tree

logger = logging.getLogger(__name__)

# Seconds rustc may take on one file (the largest kernel file, rust/zerocopy/src/lib.rs, takes under 1 s)
RUSTC_TIMEOUT = 120
RUSTC_ARGS = ("--edition=2021", "--crate-type=lib", "-Z", "unpretty=ast-tree", "-o", "-")

# Once per process: rustc's version (logged when first run) and whether it could not be started
_rustc_state: dict[str, Any] = {"version": None, "missing": False}


def rust_ast_parse(CS: ChangeSetType) -> None:
    """Entry point for parsing Rust source files into ChangeSet operations."""
    if CS.file_operation == "R100":
        return
    if CS.file_operation == "A":
        Rust_Manager(CS)
    elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation.startswith("R")):
        get_prior_tags(CS)
        Rust_Manager(CS)
    elif CS.file_operation == "D":
        pass  # Tags are immutable content records: a deleted file stages nothing
    # Emit the symbol definitions recorded while parsing (core/SymbolRows.py)
    finish_symbol_rows(CS)


def get_prior_tags(CS: ChangeSetType) -> None:
    """Previous version's tags of the file with the name/position indices and the stored symbol items of its tags."""
    _load_prior_tags(CS, with_indexes=True, with_symbols=True)


def rustc_version() -> str | None:
    """`rustc --version` of the toolchain in use (cached per process), None when rustc cannot run."""
    if _rustc_state["version"] is None and not _rustc_state["missing"]:
        try:
            out = subprocess.run(["rustc", "--version"], capture_output=True, text=True, timeout=30, check=False)
            _rustc_state["version"] = out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            _rustc_state["missing"] = True
    return _rustc_state["version"]


def run_rustc_ast_tree(path: str) -> str:
    """rustc's AST dump of `path`, or "" when rustc is missing, fails or times out."""
    if _rustc_state["missing"]:
        return ""
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    try:
        proc = subprocess.run(
            ["rustc", *RUSTC_ARGS, path],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=RUSTC_TIMEOUT,
        )
    except FileNotFoundError:
        _rustc_state["missing"] = True
        logger.error("rustc not found: Rust files are tagged whole by the raw parser")
        return ""
    except subprocess.TimeoutExpired:
        logger.warning(f"rustc timed out after {RUSTC_TIMEOUT}s on {path}")
        return ""
    except Exception as e:
        logger.error(f"Failed to execute rustc on {path}: {e}")
        return ""
    if _rustc_state["version"] is None:
        logger.info(f"Rust parser uses {rustc_version() or 'rustc (unknown version)'}")
    if proc.returncode == 0 or "Crate {" in proc.stdout:
        return proc.stdout
    first_error = next((ln for ln in proc.stderr.splitlines() if ln.strip()), "")
    logger.warning(f"rustc could not parse {path}: {first_error}")
    return ""


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

    def file_exists(self, repo_path: str) -> bool:
        """Whether the version's tree holds `repo_path` (a `mod x;` target)."""
        return os.path.isfile(os.path.join(self.mfdir, repo_path))

    def init_parse(self, CS: ChangeSetType) -> None:
        """Read source file, run rustc AST extraction, and extract nodes to ChangeSet."""
        try:
            unsplit_rawfile = Path(self.fullfilename).read_text(encoding="latin-1")
        except Exception as e:
            raise FILE_ERROR(e) from e

        self.rawfile = tuple(unsplit_rawfile.split("\n"))
        ast_text = run_rustc_ast_tree(self.fullfilename)
        if ast_text:
            self.children = parse_rust_ast_tree(
                ast_text,
                self.rawfile,
                rel_path=self.filename,
                file_exists=self.file_exists,
                source_path=self.fullfilename,
            )
        if not self.children and unsplit_rawfile.strip():
            # Nothing parsed: one whole-file tag, so the file still has a tag and a history
            from parser.raw_ast.raw_ast import RawManager

            with CS(REF_NO_REF):
                RawManager(CS)
            return
        self.extract(CS)

    def extract(self, CS: ChangeSetType) -> None:
        """Extract all top-level AST nodes to ChangeSet operations."""
        binding_cache: dict[str, Any] = {}  # Rust_BindingRef AST of each C symbol the file uses, staged once
        for item in self.children:
            with CS(REF_NO_REF):
                item.extract(CS, binding_cache)
