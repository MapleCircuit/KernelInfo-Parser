"""parser/raw_ast/raw_ast.py - Fallback Raw Content AST Parser & Lifecycle Manager.

Orchestrates parsing of unhandled/generic files by capturing content hash into
code occurrence tags (m_tag) and managing tag recycling/lifecycle across versions.
"""
from __future__ import annotations
import hashlib
import logging
from typing import Any

from core.globalstuff import (
    G,
    REF_ROOT,
    REF_OLD,
    REF_POS,
    REF_NO_REF,
    ASTT,
    OP_REF,
    FILE_ERROR,
)
from core.DBLayout import (
    m_file_name,
    m_file,
    m_bridge_file,
    m_ast,
    m_tag_code,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
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
            close_prior_tags(CS)
        elif CS.file_operation == "D":
            get_prior_tags(CS)
            close_prior_tags(CS)
        else:
            logger.warning("Unhandled file operation '%s' for %s", CS.file_operation, CS.current_path)


def get_prior_tags(CS: Any) -> None:
    """Query TableEngine for existing active tags registered in the previous version."""
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


def close_prior_tags(CS: Any) -> None:
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
        content_hash = hashlib.sha256(content_bytes).digest()
        content_hex = hashlib.sha256(content_bytes).hexdigest()

        lines = content.split("\n")
        line_count = max(1, len(lines))
        last_char_count = max(1, len(lines[-1])) if lines else 1

        # Check if content hash matches prior tag (recycled)
        if getattr(CS, "prior_tags", None) and getattr(CS, "prior_tags_map", None):
            tag_list = CS.prior_tags_map.get(content_hash)
            if tag_list:
                for item in tag_list:
                    if item[0] not in CS.active_tag_list:
                        x, tag_id = item
                        CS.active_tag_list.add(x)
                        with CS(REF_POS):
                            CS.store(m_bridge_tag.set(
                                ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                                tag_id,
                                1,
                                line_count,
                                1,
                                last_char_count,
                            ))
                        return

        # New tag required using content_hex for AST name and content_hash for m_tag
        with CS(REF_POS):
            CS.store(m_ast.get_set(
                None,
                content_hex,
                ASTT.Raw_Content,
            ))
            ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        with CS(REF_POS):
            CS.store(m_tag.set(
                None,
                CS.gp.VID,
                0,
                content_hash,
                ast_ref,
                0,
                0,
            ))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
            CS.store(m_tag_code.get_set(content_hash, content))

        with CS(REF_POS):
            CS.store(m_bridge_tag.set(
                ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                tag_ref,
                1,
                line_count,
                1,
                last_char_count,
            ))

        with CS(REF_POS):
            CS.store(m_map_ast.set(
                tag_ref,
                1,
                1,
                line_count,
                last_char_count,
                ast_ref,
            ))

        if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_ref, tag_ref):
            with CS(REF_POS):
                CS.store(m_bridge_map.set(
                    tag_ref,
                    tag_ref,
                ))

