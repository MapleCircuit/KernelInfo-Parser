"""parser/kconfig_ast/kconfig_eval.py - Kconfig value evaluator over a finalized model.

Port of scripts/kconfig/symbol.c (sym_calc_value, sym_calc_visibility, sym_calc_choice, ranges,
sym_set_*_value) and confdata.c (conf_read, conf_write, conf_write_defconfig,
conf_set_all_new_symbols) operating on the model produced by kconfig_model.KconfigModelBuilder.

Values are computed lazily and memoized until the next change, like kconfig's SYMBOL_VALID flag:
a symbol requested while it is being computed reads its provisional value (n / ""), which is what
kconfig does on recursion. The client-side engine (webapp/frontend/js/views/kconfig/kconfig_engine.js)
implements the same algorithm; keep both in sync.
"""
from __future__ import annotations

import re
from typing import Any

TRI_CHAR = ("n", "m", "y")
TRI_OF = {"n": 0, "m": 1, "y": 2}
STRING_TYPES = ("string", "int", "hex")


def _c_number(text: str, base: int) -> int:
    """The unbounded value C strtoll()/strtoull() read: whitespace, sign, 0x prefix for base 16."""
    m = re.match(r"\s*([+-]?)(0[xX])?([0-9a-fA-F]*)" if base == 16 else r"\s*([+-]?)()([0-9]*)", text or "")
    if not m:
        return 0
    digits = ""
    for ch in m.group(3):
        if int(ch, 16) >= base:
            break
        digits += ch
    if not digits:
        return 0
    value = int(digits, base)
    return -value if m.group(1) == "-" else value


def _strtoll(text: str, base: int) -> int:
    """C strtoll(): optional whitespace/sign, 0x prefix for base 16, stops at the first bad digit."""
    return max(-(1 << 63), min((1 << 63) - 1, _c_number(text, base)))


def _strtoull(text: str, base: int) -> int:
    """C strtoull(): like strtoll, saturating at 2^64-1; a leading '-' negates in unsigned arithmetic."""
    value = _c_number(text, base)
    if abs(value) > (1 << 64) - 1:
        return (1 << 64) - 1
    return value % (1 << 64)


def _parse_number(text: str, base: int, unsigned: bool) -> int | None:
    """strtoll/strtoull(text, &tail, base) when the whole text is one valid number in range, else None."""
    m = re.fullmatch(r"([ \t\n\v\f\r]*)([+-]?)(.*)", text or "", re.S)
    sign, body = m.group(2), m.group(3)
    if base in (0, 16) and re.match(r"0[xX][0-9a-fA-F]", body):
        body, base = body[2:], 16
    elif base == 0:
        base = 8 if body.startswith("0") else 10
    digits = re.match({8: r"[0-7]*", 10: r"[0-9]*", 16: r"[0-9a-fA-F]*"}[base], body).group(0)
    if not digits or digits != body:
        return None
    value = int(digits, base)
    value = -value if sign == "-" else value
    if unsigned:
        return value % (1 << 64) if abs(value) <= (1 << 64) - 1 else None
    return value if -(1 << 63) <= value <= (1 << 63) - 1 else None


def numeric_in_bounds(sym_type: str, text: str) -> bool:
    """sym_string_check_bounds (Linux 7.4+): int fits a signed, hex an unsigned 64-bit integer (no ERANGE)."""
    if sym_type == "int":
        return -(1 << 63) <= _c_number(text, 10) <= (1 << 63) - 1
    if sym_type == "hex":
        return abs(_c_number(text, 16)) <= (1 << 64) - 1
    return True


def string_valid(sym_type: str, text: str) -> bool:
    """sym_string_valid."""
    if sym_type == "string":
        return True
    if sym_type == "int":
        return re.fullmatch(r"-?(0|[1-9][0-9]*)", text or "") is not None
    if sym_type == "hex":
        return re.fullmatch(r"(0[xX])?[0-9a-fA-F]+", text or "") is not None
    if sym_type in ("bool", "tristate"):
        return bool(text) and text[0] in "yYmMnN"
    return False


def escape_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class KconfigEvaluator:
    """Configuration state (user values) and computed symbol values for one model."""

    def __init__(self, model: dict[str, Any]) -> None:
        self.model = model
        self.exprs: list[list[Any]] = model["exprs"]
        self.syms: dict[str, dict[str, Any]] = model["symbols"]
        self.nodes: list[dict[str, Any]] = model["nodes"]
        self.modules = model.get("modules_sym") or "MODULES"
        # Revision-specific conf behaviour (detected from its scripts/kconfig sources)
        self.sem: dict[str, bool] = model.get("semantics") or {}
        self.choices = [name for name, s in self.syms.items() if s.get("is_choice")]
        # S_DEF_USER: bool/tristate symbols hold "y"/"m"/"n", others the raw string
        self.user: dict[str, str] = {}
        # Choice user selection (def[S_DEF_USER].val), kept even when the choice loses its flag
        self.choice_user: dict[str, str] = {}
        # def[S_DEF_USER].tri of choices, kept when conf_read drops their "has value" flag
        self.choice_def_tri: dict[str, str] = {}
        # 6.11+: choice members in priority order (menu::choice_members), declaration order at first
        self.modern_choice = bool(self.sem.get("choice_priority"))
        self.choice_order: dict[str, list[str]] = {}
        self._reset_choice_order()
        self.warnings: list[str] = []
        self._clear()

    def _reset_choice_order(self) -> None:
        self.choice_order = {c: list(self.syms[c]["members"]) for c in self.choices}

    def _promote(self, choice: str, member: str) -> None:
        """list_move(&sym->choice_link, &choice->choice_members): `member` gets the highest priority."""
        order = self.choice_order.setdefault(choice, list(self.syms[choice]["members"]))
        if member in order:
            order.remove(member)
        order.insert(0, member)

    # ------------------------------------------------------------------ cache
    def _clear(self) -> None:
        self._cur: dict[str, Any] = {}
        self._write: dict[str, bool] = {}
        self._vis: dict[str, int] = {}
        self._rev: dict[str, int] = {}
        self._implied: dict[str, int] = {}
        self._dir: dict[str, int] = {}
        self._sel: dict[str, str | None] = {}
        self._expr_memo: dict[int, int] = {}

    def invalidate(self) -> None:
        """sym_clear_all_valid."""
        self._clear()

    # ------------------------------------------------------------------ expressions
    def leaf_str(self, eid: int) -> str:
        """sym_get_string_value of a leaf (constants and undefined symbols evaluate to their name)."""
        node = self.exprs[eid]
        if node[0] == "c":
            return node[1]
        return self.sym_str(node[1])

    def expr_tri(self, eid: int) -> int:
        if eid is None or eid < 0:
            return 2
        cached = self._expr_memo.get(eid)
        if cached is not None:
            return cached
        node = self.exprs[eid]
        op = node[0]
        if op == "s":
            val = self.sym_tri(node[1])
        elif op == "c":
            val = TRI_OF.get(node[1], 0) if len(node[1]) == 1 else 0
        elif op == "&":
            val = min(self.expr_tri(node[1]), self.expr_tri(node[2]))
        elif op == "|":
            val = max(self.expr_tri(node[1]), self.expr_tri(node[2]))
        elif op == "!":
            val = 2 - self.expr_tri(node[1])
        else:
            val = self._compare(op, node[1], node[2])
        # Safe to memoize: kconfig rejects dependency loops, and choices are computed before their values
        self._expr_memo[eid] = val
        return val

    def _parse_leaf(self, text: str, sym_type: str) -> tuple[str, int]:
        """expr_parse_string: (kind, value), kind "signed" | "unsigned" | "string" (5.0+) / "invalid"."""
        if sym_type in ("bool", "tristate"):
            return "signed", TRI_OF.get(text, -1)
        if sym_type == "int":
            value, kind = _parse_number(text, 10, False), "signed"
        elif sym_type == "hex":
            value, kind = _parse_number(text, 16, True), "unsigned"
        else:
            value, kind = _parse_number(text, 0, False), "signed"
        if value is None:
            return ("string" if self.sem.get("compare_fallback_strcmp") else "invalid"), 0
        return kind, value

    def _compare(self, op: str, a: int, b: int) -> int:
        """E_EQUAL/E_UNEQUAL/E_LTH/E_LEQ/E_GTH/E_GEQ of expr_calc_value.

        Before 4.2 (=, != only) the values are compared as strings. Since 4.2 both sides are parsed
        by their symbol type (bool/tristate n<m<y, int signed, hex unsigned, others strtoll base 0)
        and compared numerically; a side that does not parse makes `<`... false (4.x) or falls
        back to strcmp (5.0+), and = / != compare the strings.
        """
        s1, s2 = self.leaf_str(a), self.leaf_str(b)
        t1, t2 = self._leaf_type(a), self._leaf_type(b)
        k1 = k2 = "string"
        v1 = v2 = 0
        if self.sem.get("typed_comparisons") and (t1 != "string" or t2 != "string"):
            k1, v1 = self._parse_leaf(s1, t1)
            k2, v2 = self._parse_leaf(s2, t2)
        if k1 == "string" or k2 == "string":
            res = (s1 > s2) - (s1 < s2)
        elif k1 == "invalid" or k2 == "invalid":
            if op not in ("=", "!="):
                return 0
            res = (s1 > s2) - (s1 < s2)
        elif k1 == "unsigned" or k2 == "unsigned":
            u1, u2 = v1 % (1 << 64), v2 % (1 << 64)
            res = (u1 > u2) - (u1 < u2)
        else:
            res = (v1 > v2) - (v1 < v2)
        ok = {"=": res == 0, "!=": res != 0, "<": res < 0, "<=": res <= 0, ">": res > 0, ">=": res >= 0}[op]
        return 2 if ok else 0

    def expr_str(self, eid: int, with_values: bool = False) -> str:
        """Kconfig source form of an expression; with_values appends each symbol's value like menuconfig."""
        if eid is None or eid < 0:
            return "y"

        def fmt(e: int, parent_prec: int) -> str:
            node = self.exprs[e]
            op = node[0]
            if op == "s":
                name = node[1]
                if with_values and name not in TRI_OF:
                    return f"{name} [={self.sym_str(name) if self.is_defined(name) else 'n'}]"
                return name
            if op == "c":
                return '"' + escape_string(node[1]) + '"'
            if op == "!":
                return "!" + fmt(node[1], 3)
            if op in ("&", "|"):
                prec = 2 if op == "&" else 1
                text = f"{fmt(node[1], prec)} {'&&' if op == '&' else '||'} {fmt(node[2], prec)}"
                return f"({text})" if prec < parent_prec else text
            return f"{fmt(node[1], 4)}{op}{fmt(node[2], 4)}"

        return fmt(eid, 0)

    def _leaf_type(self, eid: int) -> str:
        node = self.exprs[eid]
        if node[0] == "s" and node[1] in self.syms:
            return self.syms[node[1]]["type"]
        # Quoted "y"/"m"/"n" are the same constant symbols (sym_lookup)
        if node[1] in TRI_OF and self.sem.get("const_tristate_type"):
            return "tristate"  # symbol_yes/mod/no are typed S_TRISTATE since 6.10
        return "unknown"

    # ------------------------------------------------------------------ symbol values
    def is_defined(self, name: str) -> bool:
        s = self.syms.get(name)
        return s is not None and s["type"] != "unknown"

    def sym_tri(self, name: str) -> int:
        if name in TRI_OF:
            return TRI_OF[name]
        if not self.is_defined(name):
            return 0
        self._calc(name)
        val = self._cur[name]
        return val if isinstance(val, int) else 0

    def sym_str(self, name: str) -> str:
        """sym_get_string_value."""
        if name in TRI_OF or not self.is_defined(name):
            return name
        self._calc(name)
        val = self._cur[name]
        return TRI_CHAR[val] if isinstance(val, int) else val

    def value(self, name: str) -> str:
        return self.sym_str(name)

    def modules_val(self) -> int:
        return self.sym_tri(self.modules)

    def get_type(self, name: str) -> str:
        """sym_get_type: tristate degrades to bool without modules or inside a y choice."""
        s = self.syms[name]
        t = s["type"]
        if t == "tristate":
            if s.get("choice") and self.visibility(name) == 2:
                return "bool"
            if name != self.modules and self.modules_val() == 0:
                return "bool"
        return t

    def has_user(self, name: str) -> bool:
        return name in self.user

    def visibility(self, name: str) -> int:
        """sym_calc_visibility (prompt visibility, plus direct and reverse dependency states)."""
        if name in self._vis:
            return self._vis[name]
        s = self.syms[name]
        if s.get("transitional"):
            # SYMBOL_TRANS (6.18+): visible, so its .config value is used; dependencies are never computed
            self._vis[name] = 2
            self._dir[name] = self._rev[name] = self._implied[name] = 0
            return 2
        vis = 0
        hide_mod = (self.sem.get("tristate_choice_mod_hidden") and s.get("choice") and s["type"] == "tristate"
                    and self.sym_tri(s["choice"]) == 2)
        for pv in s["prompts"]:
            tri = self.expr_tri(pv)
            if hide_mod and tri == 1:
                tri = 0  # m-visible tristate values of a y choice are hidden (newer kconfig)
            vis = max(vis, tri)
        if vis == 1 and (s["type"] != "tristate" or self.modules_val() == 0):
            vis = 2
        self._vis[name] = vis
        if s.get("choice"):
            self._dir[name], self._rev[name], self._implied[name] = 2, 0, 0
            return vis
        dir_dep = self.expr_tri(s["dir_dep"]) if s["dir_dep"] >= 0 else 2
        if dir_dep == 1 and self.get_type(name) == "bool":
            dir_dep = 2
        self._dir[name] = dir_dep
        rev = self.expr_tri(s["rev_dep"]) if s["rev_dep"] >= 0 else 0
        if rev == 1 and self.get_type(name) == "bool":
            rev = 2
        self._rev[name] = rev
        implied = self.expr_tri(s["implied"]) if s.get("implied", -1) >= 0 else 0
        if implied and self.sem.get("implied_needs_dir_dep") and dir_dep == 0:
            implied = 0  # 4.10-5.6: imply only acts on symbols whose dependencies are met
        if implied == 1 and self.get_type(name) == "bool":
            implied = 2
        self._implied[name] = implied
        return vis

    def rev_dep(self, name: str) -> int:
        self.visibility(name)
        return self._rev[name]

    def dir_dep(self, name: str) -> int:
        self.visibility(name)
        return self._dir[name]

    def _default_prop(self, name: str) -> tuple[int, int] | None:
        """sym_get_default_prop: first default whose condition (incl. dependencies) is not n."""
        for value_id, vis_id in self.syms[name]["defaults"]:
            vis = self.expr_tri(vis_id)
            if vis != 0:
                return value_id, vis
        return None

    def _calc(self, name: str) -> None:
        if name in self._cur:
            return
        s = self.syms[name]
        string_type = s["type"] in STRING_TYPES
        self._cur[name] = self._empty(s["type"]) if string_type else 0  # provisional value seen on recursion
        self._calc_inner(name, s, string_type)

    def _empty(self, sym_type: str) -> str:
        """Value of an int/hex/string symbol without user value or default ("0"/"0x0" in newer kconfig)."""
        if self.sem.get("int_hex_zero_default"):
            return {"int": "0", "hex": "0x0"}.get(sym_type, "")
        return ""

    def _calc_inner(self, name: str, s: dict[str, Any], string_type: bool) -> None:
        modern_write = self.sem.get("write_on_nonzero_default", False)
        write = False
        vis = self.visibility(name)
        if modern_write and vis != 0:
            write = True
        rev = self._rev[name]
        stype = self.get_type(name)
        choice = s.get("choice")
        prop = None  # the default property used (sym_calc_value's `prop`)
        if not string_type:
            if choice and self.modern_choice:
                # sym_calc_choice sets the visible members (6.11+); a hidden member stays n
                newval = 2 if vis != 0 and self.choice_selection(choice) == name else 0
            elif choice and vis == 2:
                newval = 2 if self.choice_selection(choice) == name else 0
            else:
                newval = 0
                user_done = False
                if vis != 0:
                    write = True
                    if self.has_user(name):
                        newval = min(TRI_OF[self.user[name]], vis)
                        user_done = True
                if not user_done:
                    if rev != 0:
                        write = True
                    if not s.get("is_choice"):
                        prop = self._default_prop(name)
                        if prop is not None:
                            newval = min(self.expr_tri(prop[0]), prop[1])
                            # Older kconfig wrote every symbol with an active default, newer only non-n ones
                            if newval != 0 or not modern_write:
                                write = True
                        implied = self._implied.get(name, 0)
                        if implied != 0:
                            write = True
                            newval = max(newval, implied)
                            if self.sem.get("imply_obeys_dir_dep", True):  # 5.7+
                                newval = min(newval, self._dir[name])
                newval = max(newval, rev)
            if newval == 1 and stype == "bool":
                newval = 2
            elif newval == 1 and self.sem.get("implied_y_forces_y") and self._implied.get(name, 0) == 2:
                newval = 2  # 4.10-5.6: a symbol implied by y cannot be m
            self._inherit_transitional(name, prop, TRI_CHAR[newval])
            self._cur[name] = newval
        else:
            newval = self._empty(s["type"])
            if vis != 0:
                write = True
            if vis != 0 and self.has_user(name):
                newval = self.user[name]
            else:
                prop = self._default_prop(name)
                if prop is not None:
                    write = True
                    newval = self.leaf_str(prop[0])
            self._inherit_transitional(name, prop, newval)
            self._cur[name] = newval
            if s["type"] in ("int", "hex"):
                self._validate_range(name)
        if s.get("is_choice") and self._cur[name] == 2 and not self.modern_choice:
            self._sel[name] = self._calc_choice(name)
        if choice:
            # A written choice writes its visible values (sticky SYMBOL_WRITE in kconfig)
            if self._choice_raw_write(choice) and vis != 0:
                write = True
        if s.get("is_choice"):
            self._write["\0raw:" + name] = write
        if s.get("auto") or s.get("transitional"):
            write = False
        self._write[name] = write

    def _inherit_transitional(self, name: str, prop: tuple[int, int] | None, newval: str) -> None:
        """6.18+: a value defaulting to a transitional symbol that has a .config value becomes a user value."""
        if prop is None or self.has_user(name):
            return
        node = self.exprs[prop[0]]
        if node[0] == "s" and self.syms.get(node[1], {}).get("transitional") and self.has_user(node[1]):
            self.user[name] = newval

    def _choice_raw_write(self, choice: str) -> bool:
        self._calc(choice)
        return self._write.get("\0raw:" + choice, False)

    def _active_range(self, name: str) -> tuple[int, int] | None:
        """sym_get_range_prop: the first range whose condition is not n."""
        for lo_id, hi_id, vis_id in self.syms[name]["ranges"]:
            if self.expr_tri(vis_id) != 0:
                return lo_id, hi_id
        return None

    def _near_range_bound(self, name: str, value: str) -> int | None:
        """sym_get_near_range_bound: the bound an out-of-range value is moved to (None: in range)."""
        t = self.syms[name]["type"]
        rng = self._active_range(name)
        if rng is None or t not in ("int", "hex"):
            return None
        base = 16 if t == "hex" else 10
        unsigned = base == 16 and self.sem.get("hex_range_unsigned")
        conv = _strtoull if unsigned else _strtoll
        val = conv(value, base)
        if val >= self._range_val(rng[0], base, unsigned):
            if val <= self._range_val(rng[1], base, unsigned):
                return None
            return rng[1]
        return rng[0]

    def _bound_text(self, name: str, bound_id: int) -> str:
        """The value an out-of-range value becomes: the bound's own value (6.7+) or it reformatted."""
        if self.sem.get("range_bound_text"):
            return self.leaf_str(bound_id)
        t = self.syms[name]["type"]
        base = 16 if t == "hex" else 10
        bound = self._range_val(bound_id, base)
        return str(bound) if t == "int" else f"0x{bound & ((1 << 64) - 1):x}"

    def _validate_range(self, name: str) -> None:
        bound_id = self._near_range_bound(name, self._cur[name])
        if bound_id is not None:
            self._cur[name] = self._bound_text(name, bound_id)

    def _range_val(self, eid: int, base: int, unsigned: bool = False) -> int:
        """sym_get_range_val: a bound naming an int/hex symbol is read in that symbol's base."""
        node = self.exprs[eid]
        if node[0] == "s" and self.is_defined(node[1]):
            t = self.syms[node[1]]["type"]
            base = 10 if t == "int" else 16 if t == "hex" else base
        if unsigned and base == 16:
            return _strtoull(self.leaf_str(eid), base)
        return _strtoll(self.leaf_str(eid), base)

    def _choice_default(self, choice: str) -> str | None:
        """sym_choice_default."""
        s = self.syms[choice]
        for value_id, vis_id in s["defaults"]:
            if self.expr_tri(vis_id) == 0:
                continue
            node = self.exprs[value_id]
            if node[0] == "s" and node[1] in self.syms and self.visibility(node[1]) != 0:
                return node[1]
        for member in s["members"]:
            if self.visibility(member) != 0:
                return member
        return None

    def _calc_choice(self, choice: str) -> str | None:
        """sym_calc_choice."""
        for member in self.syms[choice]["members"]:
            self.visibility(member)
        selected = self.choice_user.get(choice)
        if selected and selected in self.syms and self.visibility(selected) != 0:
            return selected
        selected = self._choice_default(choice)
        if selected is None:
            self._cur[choice] = 0
        return selected

    def _calc_choice_modern(self, choice: str) -> str | None:
        """sym_calc_choice (6.11+): members by priority, then the default, then declaration order."""
        members = self.syms[choice]["members"]
        order = self.choice_order.get(choice) or members
        res = next((m for m in order if self.visibility(m) != 0 and self.user.get(m) == "y"), None)
        if res is None:
            res = self._choice_default(choice)
            if res is not None and self.user.get(res) == "n":
                res = None
        if res is None:
            res = next((m for m in members if self.visibility(m) != 0 and not self.has_user(m)), None)
        if res is None:
            res = next((m for m in reversed(order) if self.visibility(m) != 0), None)
        return res

    def choice_selection(self, choice: str) -> str | None:
        if self.modern_choice:
            if choice not in self._sel:
                self._sel[choice] = None  # provisional value on recursion
                self._sel[choice] = self._calc_choice_modern(choice)
            return self._sel[choice]
        self._calc(choice)
        return self._sel.get(choice) if self._cur[choice] == 2 else None

    def writes(self, name: str) -> bool:
        self._calc(name)
        return self._write.get(name, False)

    def is_changeable(self, name: str) -> bool:
        """sym_is_changable."""
        if self.modern_choice and self.syms[name].get("is_choice"):
            return False
        return self.visibility(name) > self.rev_dep(name)

    # ------------------------------------------------------------------ setting values (symbol.c)
    def tristate_within_range(self, name: str, val: int) -> bool:
        s = self.syms[name]
        vis = self.visibility(name)
        stype = self.get_type(name)
        if vis == 0 or stype not in ("bool", "tristate"):
            return False
        if stype == "bool" and val == 1:
            return False
        if vis <= self.rev_dep(name):
            return False
        if val == 1 and self.sem.get("implied_y_forces_y") and self._implied.get(name, 0) == 2:
            return False  # 4.10-5.6
        if s.get("choice") and vis == 2 and not self.modern_choice:
            return val == 2
        return self.rev_dep(name) <= val <= vis

    def set_tristate(self, name: str, val: int) -> bool:
        """sym_set_tristate_value."""
        old = self.sym_tri(name)
        if old != val and not self.tristate_within_range(name, val):
            return False
        s = self.syms[name]
        choice = s.get("choice")
        if choice and val == 2 and self.modern_choice:
            self.choice_set_value(choice, name)
            return True
        if choice and val == 2:
            self.choice_user[choice] = name
            self.user.setdefault(choice, "n")
            for member in self.syms[choice]["members"]:
                if self.visibility(member) != 0:
                    self.user.setdefault(member, "n")
        self.user[name] = TRI_CHAR[val]
        self.invalidate()
        return True

    def choice_set_value(self, choice: str, name: str) -> None:
        """choice_set_value (6.11+): `name` y, the other visible members n, each promoted in turn."""
        for member in self.syms[choice]["members"]:
            if self.visibility(member) == 0:
                continue
            self.user[member] = "y" if member == name else "n"
            self._promote(choice, member)
        self.invalidate()

    def string_within_range(self, name: str, text: str) -> bool:
        s = self.syms[name]
        t = s["type"]
        if t == "string":
            return True
        if t in ("int", "hex"):
            if not string_valid(t, text):
                return False
            if self.sem.get("numeric_bounds_check") and not numeric_in_bounds(t, text):
                return False
            base = 16 if t == "hex" else 10
            rng = self._active_range(name)
            if rng is None:
                return True
            unsigned = base == 16 and self.sem.get("hex_range_unsigned")
            val = (_strtoull if unsigned else _strtoll)(text, base)
            return self._range_val(rng[0], base, unsigned) <= val <= self._range_val(rng[1], base, unsigned)
        if t in ("bool", "tristate") and text:
            return self.tristate_within_range(name, TRI_OF.get(text[0].lower(), -1))
        return False

    def set_value(self, name: str, text: str) -> bool:
        """sym_set_string_value (bool/tristate accept y/m/n)."""
        if name not in self.syms:
            return False
        t = self.syms[name]["type"]
        if t in ("bool", "tristate"):
            key = (text or "")[:1].lower()
            return key in TRI_OF and self.set_tristate(name, TRI_OF[key])
        if not self.string_within_range(name, text):
            return False
        if t == "hex" and not text.lower().startswith("0x"):
            text = "0x" + text
        self.user[name] = text
        self.invalidate()
        return True

    def toggle(self, name: str) -> int:
        """sym_toggle_tristate_value: n -> m -> y -> n, skipping values out of range."""
        choice = self.syms[name].get("choice")
        if choice and self.modern_choice:
            # sym_toggle_tristate_value: a choice member is selected (6.11+)
            self.choice_set_value(choice, name)
            return self.sym_tri(name)
        old = new = self.sym_tri(name)
        while True:
            new = (new + 1) % 3
            if self.set_tristate(name, new) or new == old:
                break
        return self.sym_tri(name)

    # ------------------------------------------------------------------ .config input (confdata.c)
    def load_config(self, text: str) -> dict[str, Any]:
        """conf_read(): load a .config/defconfig as user values. Returns a report of what was ignored."""
        self.user.clear()
        self.choice_user.clear()
        self.choice_def_tri.clear()
        self._reset_choice_order()
        report: dict[str, Any] = {"unknown": [], "invalid": [], "warnings": []}
        choice_flag: dict[str, bool] = {c: True for c in self.choices}
        choice_tri: dict[str, int] = {c: 0 for c in self.choices}

        def set_sym(name: str, val: str) -> None:
            self.user[name] = val
            choice = self.syms[name].get("choice")
            if not choice:
                return
            if self.modern_choice:
                # The last assignment to a member wins: it gets the highest priority (6.11+)
                self._promote(choice, name)
                return
            tri = TRI_OF.get(val, 0)
            if tri == 1 and choice_tri[choice] == 2:
                report["warnings"].append(f"{name} creates inconsistent choice state")
                choice_flag[choice] = False
            elif tri == 2:
                if choice_tri[choice] != 0:
                    report["warnings"].append(f"override: {name} changes choice state")
                self.choice_user[choice] = name
            choice_tri[choice] = max(choice_tri[choice], tri)

        for raw in text.splitlines():
            line = raw.rstrip("\r\n")
            if line.startswith("#"):
                m = re.match(r"^# CONFIG_(\S+) is not set", line)
                if not m:
                    continue
                name = m.group(1)
                if name not in self.syms:
                    report["unknown"].append(name)
                    continue
                if self.syms[name]["type"] in ("bool", "tristate"):
                    set_sym(name, "n")
                continue
            if not line.startswith("CONFIG_"):
                if line.strip():
                    report["warnings"].append(f"unexpected data: {line[:80]}")
                continue
            if "=" not in line:
                continue
            name, val = line[7:].split("=", 1)
            if name not in self.syms or self.syms[name].get("is_choice"):
                report["unknown"].append(name)
                continue
            t = self.syms[name]["type"]
            if t in ("bool", "tristate"):
                if t == "tristate" and val[:1] == "m":
                    set_sym(name, "m")
                elif val[:1] in ("y", "n"):
                    set_sym(name, val[:1])
                else:
                    report["invalid"].append({"symbol": name, "value": val})
            elif t == "string":
                if not val.startswith('"'):
                    continue
                out, i, closed = [], 1, False
                while i < len(val):
                    ch = val[i]
                    if ch == '"':
                        closed = True
                        break
                    if ch == "\\" and i + 1 < len(val):
                        i += 1
                        ch = val[i]
                    out.append(ch)
                    i += 1
                if not closed:
                    report["invalid"].append({"symbol": name, "value": val})
                    continue
                self.user[name] = "".join(out)
            elif t in ("int", "hex"):
                if string_valid(t, val):
                    self.user[name] = val
                else:
                    report["invalid"].append({"symbol": name, "value": val})
        if not self.modern_choice:
            for choice in self.choices:
                self.user[choice] = self.choice_def_tri[choice] = TRI_CHAR[choice_tri[choice]]
        self.invalidate()

        # conf_read: count values that differ from what would be saved, settle choice flags
        unsaved = 0
        for name, s in self.syms.items():
            if s["type"] == "unknown":
                continue
            if s.get("is_choice"):
                if not self.modern_choice:
                    for member in s["members"]:
                        if self.visibility(member) != 0 and member not in self.user:
                            choice_flag[name] = False
                continue
            if s.get("auto"):
                continue
            written = self.writes(name)
            if self.has_user(name) and written:
                if self.user[name] != self.sym_str(name):
                    unsaved += 1
            elif self.has_user(name) or written:
                unsaved += 1
        if not self.modern_choice:
            for choice, keep in choice_flag.items():
                if not keep:
                    self.user.pop(choice, None)
        keep_invisible = self.sem.get("conf_read_keeps_invisible")
        keep_out_of_range = self.sem.get("conf_read_keeps_out_of_range")
        for name in list(self.user):
            s = self.syms.get(name)
            if s is None or s.get("choice"):
                continue
            if self.visibility(name) == 0 and not unsaved and not keep_invisible:
                del self.user[name]
            elif (s["type"] in STRING_TYPES and not keep_out_of_range
                  and not self.string_within_range(name, self.user[name])):
                del self.user[name]
                unsaved += 1
        self.invalidate()
        report["unsaved"] = unsaved
        return report

    def set_all_new_symbols(self, mode: str) -> None:
        """conf_set_all_new_symbols for 'yes' / 'mod' / 'no' / 'default' (all*config, defconfig)."""
        fill = {"yes": "y", "mod": "m", "no": "n"}.get(mode)
        for name, s in self.syms.items():
            # Transitional symbols have no prompt: conf_set_all_new_symbols never assigns them
            if self.has_user(name) or s["type"] == "unknown" or s.get("transitional"):
                continue
            # 6.11+: choices and their members are left to sym_calc_choice
            if self.modern_choice and (s.get("choice") or s.get("is_choice")):
                continue
            if self.get_type(name) in ("bool", "tristate") and fill is not None:
                # `option allnoconfig_y` (3.15-5.x): allnoconfig sets it to y
                self.user[name] = "y" if mode == "no" and s.get("allnoconfig_y") else fill
        self.invalidate()
        if self.modern_choice:
            return
        for choice in self.choices:
            if self.has_user(choice):
                continue
            self._calc(choice)
            self.user[choice] = self.choice_def_tri.get(choice, "n")
            self.invalidate()

    # ------------------------------------------------------------------ menus and output
    def menu_visible(self, node_id: int) -> bool:
        """menu_is_visible."""
        node = self.nodes[node_id]
        if node["prompt"] is None:
            return False
        if node["vis_if"] >= 0 and self.expr_tri(node["vis_if"]) == 0:
            return False
        sym = node["symbol_name"]
        if sym:
            self._calc(sym)
        if self.expr_tri(node["pvis"]) != 0:
            return True
        if not sym or self.sym_tri(sym) == 0 or self.sem.get("menu_visible_no_children"):
            return False
        return any(self.menu_visible(child) for child in node["children"])

    def _symbol_line(self, name: str) -> str | None:
        s = self.syms[name]
        t = s["type"]
        if t in ("bool", "tristate"):
            tri = self.sym_tri(name)
            return f"# CONFIG_{name} is not set" if tri == 0 else f"CONFIG_{name}={TRI_CHAR[tri]}"
        if t == "string":
            return f'CONFIG_{name}="{escape_string(self.sym_str(name))}"'
        if t in ("int", "hex"):
            return f"CONFIG_{name}={self.sym_str(name)}"
        return None

    def write_config(self) -> str:
        """conf_write(): the full .config, in menu order, with menu comment blocks."""
        header = self.model.get("config_header") or "Automatically generated make config: don't edit"
        out = ["#", f"# {header}", f"# {self.model.get('mainmenu', '')}", "#"]
        written: set[str] = set()
        end_comments = self.sem.get("menu_end_comments", False)
        need_newline = False

        def walk(node_id: int) -> None:
            nonlocal need_newline
            node = self.nodes[node_id]
            sym = node["symbol_name"]
            if not sym:
                if self.menu_visible(node_id):
                    out.extend(["", "#", f"# {node['prompt']}", "#"])
                    need_newline = False
            elif not self.syms[sym].get("is_choice") and sym not in written and self.writes(sym):
                line = self._symbol_line(sym)
                if line is not None:
                    if need_newline:
                        out.append("")
                        need_newline = False
                    written.add(sym)
                    out.append(line)
            for child in node["children"]:
                walk(child)
            # Before 5.18 an empty menu got no "# end of" line (it was only written when leaving children)
            if (end_comments and not sym and node["kind"] == "menu" and self.menu_visible(node_id)
                    and (node["children"] or self.sem.get("end_comment_empty_menus", True))):
                out.append(f"# end of {node['prompt']}")
                need_newline = True

        for child in self.nodes[0]["children"]:
            walk(child)
        return "\n".join(out) + "\n"

    def string_default(self, name: str) -> str:
        """sym_get_string_default."""
        s = self.syms[name]
        t = s["type"]
        val, text = 0, ""
        prop = self._default_prop(name)
        if prop is not None:
            if t in ("bool", "tristate"):
                val = min(self.expr_tri(prop[0]), prop[1])
            else:
                text = self.leaf_str(prop[0])
        val = max(val, self.rev_dep(name))
        if val == 1 and not s.get("choice") and self.modules_val() == 0:
            val = 2
        if t == "bool" and val == 1:
            val = 2
        # Adjust the default value if this symbol is implied by another (4.10+; 0 before `imply`)
        val = max(val, self._implied.get(name, 0))
        if t in ("bool", "tristate"):
            return TRI_CHAR[val]
        if not text and self.sem.get("int_hex_zero_default"):
            text = {"int": "0", "hex": "0x0"}.get(t, text)
        if self.sem.get("default_range_clamp"):
            bound_id = self._near_range_bound(name, text)
            if bound_id is not None:
                text = self.leaf_str(bound_id)
        return text

    def write_defconfig(self) -> str:
        """conf_write_defconfig(): minimal config (savedefconfig)."""
        out: list[str] = []
        written: set[str] = set()
        for node in self.nodes[1:]:
            sym = node["symbol_name"]
            if not sym or self.syms[sym].get("is_choice") or sym in written:
                continue
            if not self.writes(sym):
                continue
            written.add(sym)
            if not self.is_changeable(sym):
                continue
            if self.sym_str(sym) == self.string_default(sym):
                continue
            choice = self.syms[sym].get("choice")
            if choice:
                cs = self.syms[choice]
                if not cs.get("optional") and sym == self._choice_default(choice):
                    if self.syms[sym]["type"] == "bool" and self.sym_tri(sym) == 2:
                        continue
            line = self._symbol_line(sym)
            if line is not None:
                out.append(line)
        return "\n".join(out) + ("\n" if out else "")

    def config_values(self) -> dict[str, str]:
        """Every written symbol's value (what .config/autoconf.h would contain)."""
        values: dict[str, str] = {}
        for name, s in self.syms.items():
            if s["type"] == "unknown" or s.get("is_choice"):
                continue
            if self.writes(name):
                values[name] = self.sym_str(name)
        return values


class _Blocked(Exception):
    pass


def solve(ev: KconfigEvaluator, target: str, want: int = 2, max_steps: int = 60) -> dict[str, Any]:
    """Greedy prerequisite solver: reach `target >= want` through legal user changes only.

    Each round plans the changes that would satisfy the target's prompt visibility (or, for
    prompt-less symbols, one of its selectors), applies the first legal change exactly as a user
    would in menuconfig (sym_set_*), and re-plans, until the target is reached or nothing legal is
    left. Symbols forced by selects, fixed by the architecture or without prompts are reported.
    """
    steps: list[dict[str, str]] = []
    blocked: list[str] = []
    if target not in ev.syms or ev.syms[target]["type"] == "unknown":
        return {"solution_found": False, "toggles_needed": [], "blocked_by": [f"{target} is not defined for this architecture"]}
    if ev.syms[target]["type"] not in ("bool", "tristate"):
        want = 2

    def need_sym(name: str, level: int, plan: list[tuple[str, str]], seen: set[str]) -> None:
        if name in ("y", "m", "n"):
            if TRI_OF[name] < level:
                raise _Blocked(f"constant {name} can never be {TRI_CHAR[level]}")
            return
        if not ev.is_defined(name):
            raise _Blocked(f"{name} is not defined for this architecture")
        if ev.sym_tri(name) >= level:
            return
        if name in seen:
            raise _Blocked(f"dependency loop through {name}")
        seen = seen | {name}
        s = ev.syms[name]
        if ev.get_type(name) == "bool" and level == 1:
            level = 2
        if s.get("choice") and ev.visibility(name) == 2:
            plan.append((name, "y"))
            return
        if ev.visibility(name) >= level and ev.tristate_within_range(name, level):
            plan.append((name, TRI_CHAR[level]))
            return
        if ev.visibility(name) >= level:
            raise _Blocked(f"{name} cannot be set to {TRI_CHAR[level]} (forced or out of range)")
        errors: list[str] = []
        for pvis in s["prompts"]:
            attempt: list[tuple[str, str]] = []
            try:
                need_expr(pvis, level, attempt, seen)
                plan.extend(attempt)
                plan.append((name, TRI_CHAR[level]))
                return
            except _Blocked as exc:
                errors.append(str(exc))
        # Prompt-less (or unreachable prompt): try enabling one of its selectors
        for selector in _selectors(ev, name):
            attempt = []
            try:
                need_sym(selector, level, attempt, seen)
                plan.extend(attempt)
                return
            except _Blocked as exc:
                errors.append(str(exc))
        if not s["prompts"] and not errors:
            defaults = "; ".join(f"default {ev.expr_str(v)} if {ev.expr_str(c)}" for v, c in s["defaults"][:3])
            raise _Blocked(f"{name} has no prompt and nothing selects it (value derived from: {defaults or 'no defaults'})")
        raise _Blocked(errors[0] if errors else f"{name} cannot be enabled")

    def lower_sym(name: str, level: int, plan: list[tuple[str, str]], seen: set[str]) -> None:
        if name in ("y", "m", "n") or not ev.is_defined(name):
            if (TRI_OF.get(name, 0)) > level:
                raise _Blocked(f"constant {name} cannot be lowered")
            return
        if ev.sym_tri(name) <= level:
            return
        if name in seen:
            raise _Blocked(f"dependency loop through {name}")
        seen = seen | {name}
        if ev.rev_dep(name) > level:
            raise _Blocked(f"{name} is forced to {ev.value(name)} by select ({', '.join(_selectors(ev, name)[:3])})")
        if ev.visibility(name) == 0:
            raise _Blocked(f"{name} has no visible prompt (value comes from its defaults)")
        if not ev.tristate_within_range(name, level):
            raise _Blocked(f"{name} cannot be set to {TRI_CHAR[level]}")
        plan.append((name, TRI_CHAR[level]))

    def need_expr(eid: int, level: int, plan: list[tuple[str, str]], seen: set[str]) -> None:
        if eid < 0 or ev.expr_tri(eid) >= level:
            return
        node = ev.exprs[eid]
        op = node[0]
        if op == "s":
            need_sym(node[1], level, plan, seen)
        elif op == "c":
            raise _Blocked(f'constant "{node[1]}" is false')
        elif op == "&":
            need_expr(node[1], level, plan, seen)
            need_expr(node[2], level, plan, seen)
        elif op == "|":
            best: list[tuple[str, str]] | None = None
            error = None
            for side in (node[1], node[2]):
                attempt: list[tuple[str, str]] = []
                try:
                    need_expr(side, level, attempt, seen)
                    if best is None or len(attempt) < len(best):
                        best = attempt
                except _Blocked as exc:
                    error = error or str(exc)
            if best is None:
                raise _Blocked(error or "no alternative can be satisfied")
            plan.extend(best)
        elif op == "!":
            lower_expr(node[1], 2 - level, plan, seen)
        elif op in ("=", "!="):
            left, right = ev.exprs[node[1]], ev.exprs[node[2]]
            sym_node, const = (left, right) if left[0] == "s" and ev.is_defined(left[1]) else (right, left)
            if sym_node[0] != "s" or not ev.is_defined(sym_node[1]):
                raise _Blocked(f"{ev.leaf_str(node[1])} {op} {ev.leaf_str(node[2])} is fixed")
            name, text = sym_node[1], const[1]
            if op == "=":
                if ev.syms[name]["type"] in ("bool", "tristate"):
                    if text in TRI_OF:
                        if TRI_OF[text] > ev.sym_tri(name):
                            need_sym(name, TRI_OF[text], plan, seen)
                        else:
                            lower_sym(name, TRI_OF[text], plan, seen)
                        return
                    raise _Blocked(f"{name} can never equal {text}")
                if ev.visibility(name) == 0:
                    raise _Blocked(f"{name} = {text} requires a value {name} cannot take (no visible prompt)")
                plan.append((name, text))
            else:
                if ev.syms[name]["type"] in ("bool", "tristate") and text in ("n", "y", "m"):
                    if text == "n":
                        need_sym(name, 1, plan, seen)
                    else:
                        lower_sym(name, 0, plan, seen)
                    return
                raise _Blocked(f"{name} != {text} cannot be arranged automatically")
        else:
            raise _Blocked(f"comparison {op} cannot be arranged automatically")

    def lower_expr(eid: int, level: int, plan: list[tuple[str, str]], seen: set[str]) -> None:
        if eid < 0:
            raise _Blocked("an unconditional dependency cannot be lowered")
        if ev.expr_tri(eid) <= level:
            return
        node = ev.exprs[eid]
        op = node[0]
        if op == "s":
            lower_sym(node[1], level, plan, seen)
        elif op == "c":
            raise _Blocked(f'constant "{node[1]}" is true')
        elif op == "|":
            lower_expr(node[1], level, plan, seen)
            lower_expr(node[2], level, plan, seen)
        elif op == "&":
            error = None
            for side in (node[1], node[2]):
                attempt: list[tuple[str, str]] = []
                try:
                    lower_expr(side, level, attempt, seen)
                    plan.extend(attempt)
                    return
                except _Blocked as exc:
                    error = error or str(exc)
            raise _Blocked(error or "cannot be lowered")
        elif op == "!":
            need_expr(node[1], 2 - level, plan, seen)
        else:
            raise _Blocked(f"{ev.leaf_str(node[1])} {op} {ev.leaf_str(node[2])} cannot be changed automatically")

    is_tri = ev.syms[target]["type"] in ("bool", "tristate")

    def reached() -> bool:
        # int/hex/string targets are "solved" once their prompt is visible (a value can be entered)
        return ev.sym_tri(target) >= want if is_tri else ev.visibility(target) > 0

    for _ in range(max_steps):
        if reached():
            break
        plan: list[tuple[str, str]] = []
        try:
            if is_tri:
                need_sym(target, want, plan, set())
            else:
                prompts = ev.syms[target].get("prompts", [])
                if not prompts:
                    raise _Blocked(f"{target} has no visible prompt")
                errors: list[str] = []
                satisfied = False
                for pvis in prompts:
                    attempt: list[tuple[str, str]] = []
                    try:
                        need_expr(pvis, 2, attempt, set())
                        plan.extend(attempt)
                        satisfied = True
                        break
                    except _Blocked as exc:
                        errors.append(str(exc))
                if not satisfied:
                    raise _Blocked(errors[0] if errors else f"{target} has no satisfiable prompt")
        except _Blocked as exc:
            blocked.append(str(exc))
            break
        applied = False
        for name, value in plan:
            before = ev.value(name)
            if before == value:
                continue
            if ev.set_value(name, value):
                steps.append({"symbol": name, "from": before, "set_to": value, "to": value})
                applied = True
                break
        if not applied:
            blocked.append(f"no legal change left (next step: {plan[0][0]}={plan[0][1]})" if plan else "no change planned")
            break

    done = reached()
    return {
        "solution_found": done,
        "toggles_needed": steps,
        "total_toggles": len(steps),
        "blocked_by": [] if done else blocked,
        "final_value": ev.value(target),
    }


def _selectors(ev: KconfigEvaluator, name: str) -> list[str]:
    """Symbols with a `select <name>` property."""
    index = ev.__dict__.get("_selector_index")
    if index is None:
        index = {}
        for sym_name, s in ev.syms.items():
            for target, _vis in s.get("selects", []):
                index.setdefault(target, []).append(sym_name)
        ev.__dict__["_selector_index"] = index
    return index.get(name, [])
