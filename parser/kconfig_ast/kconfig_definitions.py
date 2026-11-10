"""parser/kconfig_ast/kconfig_definitions.py - Per-symbol definition facts of one Kconfig file (pure).

One parse of a Kconfig file gives every config/menuconfig definition it holds, by symbol, with its
properties and the dependencies it inherits from enclosing if/menu/choice blocks. Shared by the webapp's
Kconfig history and Compare's Kconfig diff (webapp/backend/services/kconfig_facts.py caches it per blob
SHA), so a Kconfig blob is parsed once for all its symbols (parser/kconfig_ast/KCONFIG_API.md section 9).
"""
from __future__ import annotations

import json
import logging
import zlib
from typing import Any

from parser.kconfig_ast.kconfig_lexer import KconfigLexer
from parser.kconfig_ast.kconfig_parser import KconfigChoice, KconfigConfig, KconfigIf, KconfigMenu, KconfigParser

logger = logging.getLogger(__name__)

KCONFIG_TYPE_NAMES = {1: "bool", 2: "tristate", 3: "string", 4: "hex", 5: "int"}
# Keys of a definition compared by kconfig_field_changes(): scalar ones, then lists
SCALAR_FIELDS = ("kind", "type", "prompt", "choice", "transitional", "help")
LIST_FIELDS = ("defaults", "depends_on", "inherited_depends", "selects", "implies", "ranges", "options")
# Extra keys for Compare's Kconfig diff: the prompt without its condition, the first default's value
RAW_FIELDS = ("prompt_text", "first_default")


def _expr(e: Any) -> str:
    return e.to_string() if e is not None else ""


def _cond(text: str, cond: Any) -> str:
    return f"{text} if {_expr(cond)}" if cond is not None else text


def _config_facts(cfg: KconfigConfig, inherited: list[str], choice: str | None, sym_type: int = 0) -> dict[str, Any]:
    t = sym_type if sym_type else cfg.sym_type
    return {
        "line_s": cfg.line_s,
        "line_e": cfg.line_e,
        "kind": "menuconfig" if cfg.is_menuconfig else "config",
        "type": KCONFIG_TYPE_NAMES.get(t, ""),
        "prompt": _cond(f'"{cfg.prompt}"', cfg.prompt_cond) if cfg.prompt else "",
        "defaults": [_cond(_expr(v), c) for v, c in cfg.defaults],
        "depends_on": [_expr(d) for d in cfg.depends_on],
        "inherited_depends": inherited,
        "selects": [_cond(t, c) for t, c in cfg.selects],
        "implies": [_cond(t, c) for t, c in cfg.implies],
        "ranges": [_cond(f"{_expr(lo)} {_expr(hi)}", c) for lo, hi, c in cfg.ranges],
        "choice": choice or "",
        "options": list(cfg.options),
        "transitional": cfg.transitional,
        "help": cfg.help_text or "",
        "prompt_text": cfg.prompt or "",
        "first_default": _expr(cfg.defaults[0][0]) if cfg.defaults else "",
    }


def kconfig_definitions(text: str, raw_fields: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Every config/menuconfig definition of one Kconfig file, by symbol, in file order.

    Each definition holds `line_s`, `line_e`, `kind`, `type`, `prompt` (with its condition), `defaults`,
    `depends_on`, `inherited_depends`, `selects`, `implies`, `ranges`, `choice`, `options`,
    `transitional` and `help`; with raw_fields also `prompt_text` and `first_default`. A file that does
    not parse (malformed historical Kconfig) gives {}."""
    try:
        items = KconfigParser(KconfigLexer(text).tokenize(), text).parse()
    except Exception as e:  # noqa: BLE001 - the caller reports the snippet without properties
        logger.debug("Kconfig parse failed: %s", e)
        return {}
    found: dict[str, list[dict[str, Any]]] = {}

    def walk(nodes: list[Any], inherited: list[str], choice: str | None, choice_type: int = 0) -> None:
        for node in nodes:
            if isinstance(node, KconfigConfig):
                effective_type = node.sym_type or choice_type
                facts = _config_facts(node, inherited, choice, effective_type)
                if not raw_fields:
                    for key in RAW_FIELDS:
                        facts.pop(key, None)
                found.setdefault(node.name, []).append(facts)
            elif isinstance(node, KconfigIf):
                walk(node.children, inherited + [_expr(node.cond)], choice, choice_type)
            elif isinstance(node, KconfigMenu):
                menu_inherited = inherited + [_expr(d) for d in node.depends_on]
                if node.visible_if is not None:
                    menu_inherited.append(f"visible if {_expr(node.visible_if)}")
                walk(node.children, menu_inherited, choice, 0)
            elif isinstance(node, KconfigChoice):
                c_type = node.sym_type
                if not c_type:
                    for child in node.children:
                        if isinstance(child, KconfigConfig) and child.sym_type:
                            c_type = child.sym_type
                            break
                walk(node.children, inherited + [_expr(d) for d in node.depends_on], node.prompt or node.name or "choice", c_type)

    walk(items, [], None, 0)
    return found


def without_raw_fields(facts: dict[str, Any]) -> dict[str, Any]:
    """A definition without the RAW_FIELDS keys (the shape Kconfig history reports)."""
    return {k: v for k, v in facts.items() if k not in RAW_FIELDS}


def kconfig_field_changes(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Property-level differences between two definitions of one Kconfig symbol."""
    if not old or not new:
        return []
    changes: list[dict[str, Any]] = []
    for key in SCALAR_FIELDS:
        if old.get(key) != new.get(key):
            changes.append({"field": key, "before": old.get(key), "after": new.get(key)})
    for key in LIST_FIELDS:
        before, after = old.get(key) or [], new.get(key) or []
        if before != after:
            changes.append({
                "field": key,
                "added": [x for x in after if x not in before],
                "removed": [x for x in before if x not in after],
                "before": before,
                "after": after,
            })
    return changes


# ----------------------------------------------------------------------------- m_blob_facts family 4
# The definitions of a Kconfig blob (`kconfig_definitions(text, raw_fields=True)`, help texts included:
# ~2.5 MB per full v6.6 tree compressed, about twice the size with help hashed, and the readers then never
# parse a blob from Git), read by the webapp's Kconfig history and Compare (kconfig_facts.definitions()).
FAMILY = 4
FORMAT = 1


def applies(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base == "Kconfig" or base.startswith("Kconfig.")


def extract(text: str) -> dict[str, list[dict[str, Any]]]:
    return kconfig_definitions(text or "", raw_fields=True)


def encode(record: dict[str, list[dict[str, Any]]] | None) -> bytes:
    if not record:
        return b""
    return zlib.compress(json.dumps(record, separators=(",", ":")).encode(), 3)


def decode(data: bytes | bytearray | memoryview | None) -> dict[str, list[dict[str, Any]]] | None:
    if data is None:
        return None
    raw = bytes(data)
    if not raw:
        return {}
    try:
        record = json.loads(zlib.decompress(raw))
    except (zlib.error, ValueError):
        return None
    return record if isinstance(record, dict) else None
