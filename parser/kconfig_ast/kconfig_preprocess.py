"""parser/kconfig_ast/kconfig_preprocess.py - Kconfig macro language (scripts/kconfig/preprocess.c port).

Linux 4.18+ Kconfig files can define variables (`name := value`, `name = value`, `name += value`)
and reference them as `$(name)` / `$(name,arg1,...)` anywhere in a token or a quoted string.
References resolve, in order, to a Kconfig variable (recursive ones are re-expanded with `$(1)`...
bound to the arguments), a built-in function (`shell`, `info`, `warning-if`, `error-if`,
`filename`, `lineno`), or an environment variable. Expansion happens while lexing, so results
depend on the order files are read (see KconfigModelBuilder).

`$(shell,cmd)` is answered by the `shell` callback instead of a real shell: the web application
plugs in a toolchain profile (parser/kconfig_ast/kconfig_toolchain.py). Every environment lookup
and shell call is recorded so the user can see and override what influenced the configuration.
"""
from __future__ import annotations

from typing import Callable

FUNCTION_MAX_ARGS = 16


class PreprocessError(Exception):
    pass


class KconfigPreprocessor:
    def __init__(self, env: dict[str, str], shell: Callable[[str], str] | None = None) -> None:
        self.env = dict(env)
        self.shell = shell or (lambda cmd: "")
        self.variables: dict[str, tuple[str, str]] = {}  # name -> (value, "simple" | "recursive")
        self._exp_count: dict[str, int] = {}
        # Provenance for the UI: environment lookups, shell calls, messages
        self.env_used: dict[str, str | None] = {}
        self.shell_calls: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []
        self.file = ""
        self.lineno = 0

    # ------------------------------------------------------------------ variables
    def add_variable(self, name: str, value: str, flavor: str) -> None:
        """variable_add: flavor is ':=' (simple), '=' (recursive) or '+=' (append)."""
        existing = self.variables.get(name)
        append = False
        if flavor == "+=":
            if existing:
                append = True
                kind = existing[1]
            else:
                kind = "recursive"
        else:
            kind = "simple" if flavor == ":=" else "recursive"
        new_value = self.expand_string(value) if kind == "simple" else value
        if append:
            new_value = f"{existing[0]} {new_value}"
        self.variables[name] = (new_value, kind)

    def _variable_expand(self, name: str, args: list[str]) -> str | None:
        var = self.variables.get(name)
        if var is None:
            return None
        count = self._exp_count.get(name, 0)
        if not args and count:
            raise PreprocessError(f"Recursive variable '{name}' references itself (eventually)")
        if count > 1000:
            raise PreprocessError("Too deep recursive expansion")
        self._exp_count[name] = count + 1
        try:
            value, kind = var
            return self.expand_string(value, args) if kind == "recursive" else value
        finally:
            self._exp_count[name] = count

    # ------------------------------------------------------------------ functions
    def _function(self, name: str, args: list[str]) -> str | None:
        arity = {"error-if": (2, 2), "filename": (0, 0), "info": (1, 1), "lineno": (0, 0), "shell": (1, 1), "warning-if": (2, 2)}
        if name not in arity:
            return None
        lo, hi = arity[name]
        if not lo <= len(args) <= hi:
            raise PreprocessError(f"wrong number of arguments passed to '{name}'")
        where = {"file": self.file, "line": self.lineno}
        if name == "shell":
            result = self.shell(args[0])
            # do_shell: trailing newlines dropped, inner newlines become spaces
            result = (result or "").rstrip("\n").replace("\n", " ")
            self.shell_calls.append({"command": args[0], "result": result, **where})
            return result
        if name == "error-if":
            if args[0] == "y":
                self.messages.append({"level": "error", "text": args[1], **where})
            return ""
        if name == "warning-if":
            if args[0] == "y":
                self.messages.append({"level": "warning", "text": args[1], **where})
            return ""
        if name == "info":
            self.messages.append({"level": "info", "text": args[0], **where})
            return ""
        if name == "filename":
            return self.file
        return str(self.lineno)

    # ------------------------------------------------------------------ expansion
    def _eval_clause(self, clause: str, args: list[str]) -> str:
        if clause.isdigit() and 0 < int(clause) <= len(args):
            return args[int(clause) - 1]
        parts: list[str] = []
        nest = 0
        prev = 0
        for i, ch in enumerate(clause):
            if nest == 0 and ch == ",":
                parts.append(clause[prev:i])
                prev = i + 1
            elif ch == "(":
                nest += 1
            elif ch == ")":
                nest -= 1
        parts.append(clause[prev:])
        if len(parts) > FUNCTION_MAX_ARGS + 1:
            raise PreprocessError("too many function arguments")
        name = self.expand_string(parts[0], args)
        new_args = [self.expand_string(p, args) for p in parts[1:]]
        res = self._variable_expand(name, new_args)
        if res is not None:
            return res
        res = self._function(name, new_args)
        if res is not None:
            return res
        if not new_args and name:
            value = self.env.get(name)
            if name not in self.env_used:
                self.env_used[name] = value
            if value is not None:
                return value
        return ""

    def expand_dollar(self, text: str, pos: int, args: list[str] | None = None) -> tuple[str, int]:
        """Expand the reference starting right after a '$' at text[pos]; returns (value, next pos)."""
        if pos >= len(text) or text[pos] != "(":
            return "$", pos
        nest = 0
        q = pos + 1
        while q < len(text):
            ch = text[q]
            if ch == "(":
                nest += 1
            elif ch == ")":
                if nest == 0:
                    break
                nest -= 1
            q += 1
        if q >= len(text):
            raise PreprocessError(f"unterminated reference to '{text[pos + 1:]}': missing ')'")
        return self._eval_clause(text[pos + 1:q], args or []), q + 1

    def _expand(self, text: str, end_of_token: bool, args: list[str] | None = None) -> tuple[str, int]:
        out: list[str] = []
        i = 0
        start = 0
        while True:
            if i < len(text) and text[i] == "$":
                out.append(text[start:i])
                value, i = self.expand_dollar(text, i + 1, args)
                out.append(value)
                start = i
                continue
            if i >= len(text) or (end_of_token and not (text[i].isalnum() or text[i] in "_-")):
                break
            i += 1
        out.append(text[start:i])
        return "".join(out), i

    def expand_string(self, text: str, args: list[str] | None = None) -> str:
        return self._expand(text, False, args)[0]

    def expand_one_token(self, text: str) -> tuple[str, int]:
        """expand_one_token: expand a token made of [A-Za-z0-9_-] and $(...); returns (value, consumed)."""
        return self._expand(text, True)
