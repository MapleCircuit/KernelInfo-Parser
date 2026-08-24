"""parser/maintainer_ast/maintainer_ast.py - Linux MAINTAINERS AST Extraction & ChangeSet Integration.

Parses MAINTAINERS source file into subsystem AST hierarchies, person records,
pattern rules, role associations, code tags, and file-to-maintainer links in ChangeSets.
"""
from __future__ import annotations
import logging
from typing import Any

import core
from core.globalstuff import (
    G,
    REF_ROOT,
    REF_POS,
    REF_C_AST,
    ASTT,
    OP_REF,
)

m_file_name = m_file = m_bridge_file = m_type_descriptor = m_ast = m_ast_container = None
m_tag = m_bridge_tag = m_map_ast = m_bridge_map = None
m_maintainer_person = m_maintainer_section = m_maintainer_member = None
m_maintainer_pattern = m_maintainer_file = m_credits_entry = None


def _init_tables() -> None:
    global m_file_name, m_file, m_bridge_file, m_type_descriptor, m_ast, m_ast_container
    global m_tag, m_bridge_tag, m_map_ast, m_bridge_map
    global m_maintainer_person, m_maintainer_section, m_maintainer_member
    global m_maintainer_pattern, m_maintainer_file, m_credits_entry
    if m_file_name is not None:
        return
    import core.DBLayout as db_layout
    m_file_name = db_layout.m_file_name
    m_file = db_layout.m_file
    m_bridge_file = db_layout.m_bridge_file
    m_type_descriptor = db_layout.m_type_descriptor
    m_ast = db_layout.m_ast
    m_ast_container = db_layout.m_ast_container
    m_tag = db_layout.m_tag
    m_bridge_tag = db_layout.m_bridge_tag
    m_map_ast = db_layout.m_map_ast
    m_bridge_map = db_layout.m_bridge_map
    m_maintainer_person = db_layout.m_maintainer_person
    m_maintainer_section = db_layout.m_maintainer_section
    m_maintainer_member = db_layout.m_maintainer_member
    m_maintainer_pattern = db_layout.m_maintainer_pattern
    m_maintainer_file = db_layout.m_maintainer_file
    m_credits_entry = db_layout.m_credits_entry


from parser.c_ast.c_ast_type import Line
from parser.c_ast.c_ast import get_prior_tags, close_prior_tags
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

logger = logging.getLogger(__name__)


def maintainer_ast_parse(CS: Any) -> None:
    """Entry point for parsing MAINTAINERS file into ChangeSet database operations."""
    _init_tables()
    with CS(REF_C_AST):
        if CS.file_operation == "A":
            MaintainerManager(CS)
        elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation[0] == "R"):
            get_prior_tags(CS)
            MaintainerManager(CS)
            close_prior_tags(CS)
        elif CS.file_operation == "D":
            get_prior_tags(CS)
            close_prior_tags(CS)
        else:
            logger.warning("Unhandled file operation '%s' for %s", CS.file_operation, CS.current_path)


def credits_ast_parse(CS: Any) -> None:
    """Entry point for parsing CREDITS file into ChangeSet database operations."""
    _init_tables()
    with CS(REF_C_AST):
        if CS.file_operation == "A":
            CreditsManager(CS)
        elif CS.file_operation == "M" or (CS.file_operation and CS.file_operation[0] == "R"):
            get_prior_tags(CS)
            CreditsManager(CS)
            close_prior_tags(CS)
        elif CS.file_operation == "D":
            get_prior_tags(CS)
            close_prior_tags(CS)
        else:
            logger.warning("Unhandled file operation '%s' for %s", CS.file_operation, CS.current_path)



class MaintainerManager:
    """Orchestrates parsing of the MAINTAINERS file and emits ChangeSet operations."""

    def __init__(self, CS: Any) -> None:
        _init_tables()
        self.CS = CS
        self.current_path = CS.current_path
        G.CURRENT_PARSING_FILE = self.current_path
        CS.parsers["MAINTAINERS"] = self

        self.rawfile = ""
        if CS.mf:
            try:
                self.rawfile = CS.mf.get_file(self.current_path, CS.gp.Version_Name)
            except Exception as e:
                logger.debug("Failed reading from MasterFile for %s: %s", self.current_path, e)

        if not self.rawfile and hasattr(CS, "raw_content"):
            self.rawfile = CS.raw_content

        self.parser = MaintainerParser(self.rawfile)
        self.sections = self.parser.parse()
        self.matcher = MaintainerMatcher(self.sections)

        self.VID = CS.gp.VID if hasattr(CS, "gp") and hasattr(CS.gp, "VID") else 1
        self.Old_VID = CS.gp.Old_VID if hasattr(CS, "gp") and hasattr(CS.gp, "Old_VID") else 0

        self.old_sections_map: dict[str, MaintainerSection] = {}
        if self.Old_VID != 0 and CS.mf and CS.gp and getattr(CS.gp, "Old_Version_Name", None):
            try:
                old_raw = CS.mf.get_file(self.current_path, CS.gp.Old_Version_Name)
                if old_raw:
                    old_sections = MaintainerParser(old_raw).parse()
                    for os_sec in old_sections:
                        self.old_sections_map[os_sec.name.strip().lower()] = os_sec
            except Exception as e:
                logger.debug("Failed loading prior MAINTAINERS for diffing: %s", e)

        self._extract_all()

    def _extract_all(self) -> None:
        """Walk parsed MaintainerSection items and emit ChangeSet records across relational tables."""
        CS = self.CS

        for section in self.sections:
            sec_key = section.name.strip().lower()
            old_sec = self.old_sections_map.get(sec_key)
            is_sec_unchanged = False
            if old_sec is not None:
                if (
                    old_sec.status.strip() == section.status.strip()
                    and old_sec.scm_tree.strip() == section.scm_tree.strip()
                    and old_sec.web_page.strip() == section.web_page.strip()
                    and old_sec.mailing_list.strip() == section.mailing_list.strip()
                    and len(old_sec.members) == len(section.members)
                    and all(
                        om.name == nm.name and om.email == nm.email and om.role == nm.role
                        for om, nm in zip(old_sec.members, section.members)
                    )
                    and len(old_sec.patterns) == len(section.patterns)
                    and all(
                        op.pat_type == np.pat_type and op.pattern == np.pattern
                        for op, np in zip(old_sec.patterns, section.patterns)
                    )
                ):
                    is_sec_unchanged = True

            # 1. AST Node for Subsystem Section
            with CS(REF_POS):
                CS.store(m_ast.set(
                    None,
                    section.name,
                    ASTT.Maintainer_Section.value,
                ))
                ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

            # 2. Tag & Spatial Coordinate Mapping
            self._tag_and_map(
                ast_ref,
                section.line_s,
                section.line_e,
                1,
                1,
                section.raw_text,
            )

            # 3. Store Subsystem Section descriptor ONLY if new or changed
            if not is_sec_unchanged:
                with CS(REF_POS):
                    CS.store(m_maintainer_section.get_set(
                        None,
                        self.VID,
                        0,
                        section.name,
                        section.status,
                        section.scm_tree,
                        section.web_page,
                        section.mailing_list,
                        ast_ref,
                    ))
                    sec_ref = ((m_maintainer_section.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

                # 4. Store Maintainers & Reviewers (Members)
                for priority, member in enumerate(section.members):
                    with CS(REF_POS):
                        CS.store(m_maintainer_person.get_set(
                            None,
                            member.name,
                            member.email,
                        ))
                        person_ref = ((m_maintainer_person.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

                    with CS(REF_POS):
                        CS.store(m_maintainer_member.set(
                            sec_ref,
                            person_ref,
                            int(member.role),
                            priority,
                        ))

                # 5. Store Pattern Rules ('F:', 'X:', 'K:', 'N:')
                for pattern in section.patterns:
                    with CS(REF_POS):
                        CS.store(m_maintainer_pattern.set(
                            sec_ref,
                            int(pattern.pat_type),
                            pattern.pattern,
                            pattern.priority,
                        ))

    def _tag_and_map(self, ast_ref: Any, line_s: int, line_e: int, char_s: int, char_e: int, code: str) -> Any:
        """Create or recycle m_tag, m_bridge_tag, m_map_ast, and m_bridge_map entries."""
        CS = self.CS
        extent = Line(line_s, line_e)
        extent.line_pos = (line_s, line_e)
        extent.char_pos = (char_s, char_e)
        extent.code = code

        current_tag = (
            None,
            self.VID,
            0,
            extent.code,
            ast_ref,
            0,
            0,
        )

        if CS.prior_tags and (extent.code != ""):
            lookup = getattr(CS, "prior_tags_map", None)
            if lookup is not None:
                tag_list = lookup.get(extent.code)
                if tag_list is not None:
                    tag_match = None
                    for item in tag_list:
                        if item[0] not in CS.active_tag_list:
                            tag_match = item
                            break
                    if tag_match is not None:
                        x, tag_id = tag_match
                        if isinstance(CS.active_tag_list, set):
                            CS.active_tag_list.add(x)
                        else:
                            CS.active_tag_list.append(x)
                        with CS(REF_POS):
                            CS.store(m_bridge_tag.set(
                                ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                                tag_id,
                                extent.line_pos[0],
                                extent.line_pos[1],
                                extent.char_pos[0],
                                extent.char_pos[1],
                            ))
                        return tag_id

        with CS(REF_POS):
            CS.store(m_tag.set(*current_tag))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        with CS(REF_POS):
            CS.store(m_bridge_tag.set(
                ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                tag_ref,
                extent.line_pos[0],
                extent.line_pos[1],
                extent.char_pos[0],
                extent.char_pos[1],
            ))
        self._map_spatial(ast_ref, tag_ref, extent)
        return tag_ref

    def _map_spatial(self, ast_ref: Any, tag_ref: Any, extent: Line) -> None:
        CS = self.CS
        line_s = 1
        char_s = 1
        line_e = max(1, extent.line_pos[1] - extent.line_pos[0] + 1)
        char_e = extent.char_pos[1]

        ast_target = (
            ast_ref
            if (isinstance(ast_ref, tuple) and len(ast_ref) == 3 and ast_ref[1] == OP_REF)
            or isinstance(ast_ref, int)
            else CS.ref(m_ast.ast_id, *ast_ref)
        )
        tag_target = (
            tag_ref
            if (isinstance(tag_ref, tuple) and len(tag_ref) == 3 and tag_ref[1] == OP_REF)
            or isinstance(tag_ref, int)
            else CS.ref(m_tag.tag_id, *tag_ref)
        )

        with CS(REF_POS):
            CS.store(m_map_ast.set(
                tag_target,
                line_s,
                char_s,
                line_e,
                char_e,
                ast_target,
            ))
        if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_target, tag_target):
            with CS(REF_POS):
                CS.store(m_bridge_map.set(
                    tag_target,
                    tag_target,
                ))


class CreditsManager:
    """Orchestrates parsing of the CREDITS file and emits ChangeSet operations."""

    def __init__(self, CS: Any) -> None:
        _init_tables()
        self.CS = CS
        self.current_path = CS.current_path
        G.CURRENT_PARSING_FILE = self.current_path
        CS.parsers["CREDITS"] = self

        self.rawfile = ""
        if CS.mf:
            try:
                self.rawfile = CS.mf.get_file(self.current_path, CS.gp.Version_Name)
            except Exception as e:
                logger.debug("Failed reading from MasterFile for %s: %s", self.current_path, e)

        if not self.rawfile and hasattr(CS, "raw_content"):
            self.rawfile = CS.raw_content

        self.parser = CreditsParser(self.rawfile)
        self.entries = self.parser.parse()

        self.VID = CS.gp.VID if hasattr(CS, "gp") and hasattr(CS.gp, "VID") else 1
        self.Old_VID = CS.gp.Old_VID if hasattr(CS, "gp") and hasattr(CS.gp, "Old_VID") else 0

        self.old_entries_map: dict[tuple[str, str], CreditsEntry] = {}
        if self.Old_VID != 0 and CS.mf and CS.gp and getattr(CS.gp, "Old_Version_Name", None):
            try:
                old_raw = CS.mf.get_file(self.current_path, CS.gp.Old_Version_Name)
                if old_raw:
                    old_entries = CreditsParser(old_raw).parse()
                    for oe in old_entries:
                        key = (oe.name.strip().lower(), oe.email.strip().lower())
                        self.old_entries_map[key] = oe
            except Exception as e:
                logger.debug("Failed loading prior CREDITS for diffing: %s", e)

        self._extract_all()

    def _extract_all(self) -> None:
        """Walk parsed CreditsEntry items and emit ChangeSet records across relational tables."""
        CS = self.CS

        for entry in self.entries:
            key = (entry.name.strip().lower(), entry.email.strip().lower())
            old_entry = self.old_entries_map.get(key)
            is_unchanged = False
            if old_entry is not None:
                if (
                    old_entry.web_page.strip() == entry.web_page.strip()
                    and old_entry.pgp_key.strip() == entry.pgp_key.strip()
                    and old_entry.description.strip() == entry.description.strip()
                    and old_entry.snail_mail.strip() == entry.snail_mail.strip()
                ):
                    is_unchanged = True

            # 1. AST Node for Contributor Entry
            with CS(REF_POS):
                CS.store(m_ast.set(
                    None,
                    entry.name,
                    ASTT.Credits_Entry.value,
                ))
                ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

            # 2. Tag & Spatial Coordinate Mapping
            self._tag_and_map(
                ast_ref,
                entry.line_s,
                entry.line_e,
                1,
                1,
                entry.raw_text,
            )

            # 3. Deduplicate / Store Contributor Person in m_maintainer_person
            with CS(REF_POS):
                CS.store(m_maintainer_person.get_set(
                    None,
                    entry.name,
                    entry.email,
                ))
                person_ref = ((m_maintainer_person.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

            # 4. Store Credits Record in m_credits_entry ONLY if new or changed
            if not is_unchanged:
                with CS(REF_POS):
                    CS.store(m_credits_entry.get_set(
                        None,
                        self.VID,
                        0,
                        person_ref,
                        entry.web_page,
                        entry.pgp_key,
                        entry.description,
                        entry.snail_mail,
                        ast_ref,
                    ))

    def _tag_and_map(self, ast_ref: Any, line_s: int, line_e: int, char_s: int, char_e: int, code: str) -> Any:
        """Create or recycle m_tag, m_bridge_tag, m_map_ast, and m_bridge_map entries."""
        CS = self.CS
        extent = Line(line_s, line_e)
        extent.line_pos = (line_s, line_e)
        extent.char_pos = (char_s, char_e)
        extent.code = code

        current_tag = (
            None,
            self.VID,
            0,
            extent.code,
            ast_ref,
            0,
            0,
        )

        if CS.prior_tags and (extent.code != ""):
            lookup = getattr(CS, "prior_tags_map", None)
            if lookup is not None:
                tag_list = lookup.get(extent.code)
                if tag_list is not None:
                    tag_match = None
                    for item in tag_list:
                        if item[0] not in CS.active_tag_list:
                            tag_match = item
                            break
                    if tag_match is not None:
                        x, tag_id = tag_match
                        if isinstance(CS.active_tag_list, set):
                            CS.active_tag_list.add(x)
                        else:
                            CS.active_tag_list.append(x)
                        with CS(REF_POS):
                            CS.store(m_bridge_tag.set(
                                ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                                tag_id,
                                extent.line_pos[0],
                                extent.line_pos[1],
                                extent.char_pos[0],
                                extent.char_pos[1],
                            ))
                        return tag_id

        with CS(REF_POS):
            CS.store(m_tag.set(*current_tag))
            tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

        with CS(REF_POS):
            CS.store(m_bridge_tag.set(
                ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
                tag_ref,
                extent.line_pos[0],
                extent.line_pos[1],
                extent.char_pos[0],
                extent.char_pos[1],
            ))
        self._map_spatial(ast_ref, tag_ref, extent)
        return tag_ref

    def _map_spatial(self, ast_ref: Any, tag_ref: Any, extent: Line) -> None:
        CS = self.CS
        line_s = 1
        char_s = 1
        line_e = max(1, extent.line_pos[1] - extent.line_pos[0] + 1)
        char_e = extent.char_pos[1]

        ast_target = (
            ast_ref
            if (isinstance(ast_ref, tuple) and len(ast_ref) == 3 and ast_ref[1] == OP_REF)
            or isinstance(ast_ref, int)
            else CS.ref(m_ast.ast_id, *ast_ref)
        )
        tag_target = (
            tag_ref
            if (isinstance(tag_ref, tuple) and len(tag_ref) == 3 and tag_ref[1] == OP_REF)
            or isinstance(tag_ref, int)
            else CS.ref(m_tag.tag_id, *tag_ref)
        )

        with CS(REF_POS):
            CS.store(m_map_ast.set(
                tag_target,
                line_s,
                char_s,
                line_e,
                char_e,
                ast_target,
            ))
        if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_target, tag_target):
            with CS(REF_POS):
                CS.store(m_bridge_map.set(
                    tag_target,
                    tag_target,
                ))


# -----------------------------------------------------------------------------
# High-Level Query Helpers
# -----------------------------------------------------------------------------

def query_maintainers_for_file(
    file_path: str,
    sections: list[MaintainerSection] | None = None,
    raw_maintainers_content: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve which maintainer sections and maintainers handle a given file path."""
    if sections is None:
        if raw_maintainers_content is None:
            raise ValueError("Must provide either sections or raw_maintainers_content")
        sections = MaintainerParser(raw_maintainers_content).parse()

    matcher = MaintainerMatcher(sections)
    matching_secs = matcher.match_file(file_path)

    results: list[dict[str, Any]] = []
    for sec in matching_secs:
        results.append({
            "section": sec.name,
            "status": sec.status,
            "scm_tree": sec.scm_tree,
            "web_page": sec.web_page,
            "mailing_list": sec.mailing_list,
            "maintainers": [
                {"name": m.name, "email": m.email, "role": "Maintainer"}
                for m in sec.get_maintainers()
            ],
            "reviewers": [
                {"name": r.name, "email": r.email, "role": "Reviewer"}
                for r in sec.get_reviewers()
            ],
            "patterns": [
                {"type": p.pat_type.name, "pattern": p.pattern}
                for p in sec.patterns
            ],
        })
    return results


def query_credits(
    raw_credits_content: str | None = None,
    entries: list[CreditsEntry] | None = None,
) -> list[dict[str, Any]]:
    """Parse and return formatted contributor credit profiles."""
    if entries is None:
        if raw_credits_content is None:
            raise ValueError("Must provide either entries or raw_credits_content")
        entries = CreditsParser(raw_credits_content).parse()

    results: list[dict[str, Any]] = []
    for entry in entries:
        results.append({
            "name": entry.name,
            "email": entry.email,
            "web_page": entry.web_page,
            "pgp_key": entry.pgp_key,
            "description": entry.description,
            "snail_mail": entry.snail_mail,
        })
    return results

