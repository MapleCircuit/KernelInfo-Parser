"""parser/raw_ast/raw_ast.py - Fallback Raw Content AST Parser & Lifecycle Manager.

Orchestrates parsing of unhandled/generic files by capturing content hash into
code occurrence tags (m_tag) and managing tag recycling/lifecycle across versions.
"""
from __future__ import annotations
import hashlib
import logging
from typing import Any

from core.globalstuff import (
    REF_POS,
    REF_NO_REF,
    ASTT,
    OP_REF,
    FILE_ERROR,
)
from core.DBLayout import m_ast
from parser.c_ast.tag_tracker import (
    check_exact_match,
    get_prior_tags as _load_prior_tags,
    stage_bridge_tag,
    stage_new_tag,
    stage_tag_map,
)

logger = logging.getLogger(__name__)


def raw_ast_parse(CS: Any) -> None:
    """Entry point for parsing unhandled / raw files into ChangeSet operations."""
    if CS.file_operation == "R100":
        return
    with CS(REF_NO_REF):
        if CS.file_operation == "A":
            RawManager(CS)
        elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation.startswith("R")):
            get_prior_tags(CS)
            RawManager(CS)
        elif CS.file_operation in ("D", "U"):
            pass  # Tags are immutable content records: a deleted file stages nothing
        else:
            logger.warning("Unhandled file operation '%s' for %s", CS.file_operation, CS.current_path)


def get_prior_tags(CS: Any) -> None:
    """Previous version's tags of the file, indexed by code hash only (a raw file has one whole-file tag)."""
    _load_prior_tags(CS, with_indexes=False, with_symbols=False)


class RawManager:
    """Manages raw content extraction, hashing, and tag creation for unparsed files."""

    def __init__(self, CS: Any) -> None:
        self.CS = CS
        self.parse_and_extract()

    def parse_and_extract(self) -> None:
        """Read full file content, compute SHA-256 hash, recycle or create tags in ChangeSet."""
        CS = self.CS
        try:
            content = CS.mf.get_file(CS.current_path, CS.gp.Version_Name)
        except Exception as e:
            raise FILE_ERROR(e) from e

        content_bytes = content.encode("latin-1") if isinstance(content, str) else bytes(content)
        digest = hashlib.sha256(content_bytes)
        content_hash = digest.digest()
        content_hex = digest.hexdigest()

        lines = content.split("\n")
        line_count = max(1, len(lines))
        last_char_count = max(1, len(lines[-1])) if lines else 1
        line_pos, char_pos = (1, line_count), (1, last_char_count)

        # Check if content hash matches prior tag (recycled)
        tag_id = check_exact_match(CS, content_hash)
        if tag_id is not None:
            stage_bridge_tag(CS, tag_id, line_pos, char_pos, positional=True)
            return

        # Modified content: transition from the first prior tag not recycled or transitioned yet
        s_tag_id = None
        if getattr(CS, "prior_tags", None):
            transitioned = CS.transitioned_tag_list
            for idx, tag in enumerate(CS.prior_tags):
                if idx not in CS.active_tag_list and idx not in transitioned:
                    transitioned.add(idx)
                    s_tag_id = tag[6] if len(tag) > 6 and tag[6] is not None else (tag[1] if len(tag) > 1 and tag[1] is not None else tag[0])
                    break

        # New tag required using content_hex for AST name and content_hash for m_tag
        with CS(REF_POS):
            CS.store(m_ast.view(
                ((m_ast.ast_id,),),
                None,
                content_hex,
                ASTT.Raw_Content,
            ))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        tag_ref = stage_new_tag(CS, content_hash, ast_ref, s_tag_id)
        stage_bridge_tag(CS, tag_ref, line_pos, char_pos, positional=True)
        stage_tag_map(CS, tag_ref, ast_ref, line_count, last_char_count, positional=True)
