"""webapp/backend/services/kconfig_service.py - Kconfig models, defconfigs, symbol provenance and usage.

Everything is read from the Git objects of the requested revision (never the work tree), so any
version can be configured regardless of what is checked out. The configuration model is built by
parser/kconfig_ast/kconfig_model.py (a port of scripts/kconfig parsing + menu_finalize) and
evaluated by parser/kconfig_ast/kconfig_eval.py, which reproduces `make <defconfig>`,
`make all{no,yes,mod}config` and `make savedefconfig` byte for byte.

An architecture is identified by its ARCH value (x86_64, i386, x86, arm, sparc64, ...): ARCH and
SRCARCH are what kconfig sees through `option env=`, and the same symbol name can be declared in
different files (and with different dependencies) per architecture, e.g. 64BIT.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from typing import Any

from fastapi import HTTPException

from parser.kbuild_conditions import KbuildConditionResolver, is_other_arch_path
from parser.kbuild_parser import KbuildParser
from parser.kconfig_ast.kconfig_eval import KconfigEvaluator, escape_string, solve
from parser.kconfig_ast.kconfig_preprocess import KconfigPreprocessor
from parser.kconfig_ast.kconfig_toolchain import ToolchainProfile, ToolchainShell
from parser.kconfig_ast.kconfig_model import (
    KconfigEnv,
    KconfigModelBuilder,
    arch_aliases,
    compact_model,
    kernel_version_string,
    resolve_kbuild_defconfig,
)
from webapp.backend.config import RepoScopedCache, get_active_repo_name, get_repo_dir
from webapp.backend.database.helpers import get_version_info, safe_decode
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.security.git import safe_rev
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services.git_reader import git_reader

logger = logging.getLogger(__name__)

DEFAULT_ARCH = "x86_64"
TYPE_CODES = {"bool": 1, "tristate": 2, "string": 3, "hex": 4, "int": 5}
# Spellings older clients used for the same ARCH values
_ARCH_SYNONYMS = {"aarch64": "arm64", "arm32": "arm", "ppc": "powerpc", "x86_32": "i386", "amd64": "x86_64"}
_MODEL_CACHE_SIZE = 6
_USAGE_LIMIT = 400
_BUILD_RESOLVER_CACHE_SIZE = 4
_BUILD_PATHS_LIMIT = 64
# #include "foo.c" / <bar.S>: sources compiled as part of another file
_INCLUDED_SOURCE_RE = r'#\s*include\s*["<][^">]+\.(c|S)[">]'
# Most source references a usage answer keeps. `git grep` for a symbol name is bounded by the tree,
# but a name is caller-supplied, so the matches it can produce must not be: see _safe_symbol().
_USAGE_REF_LIMIT = 20000


class _ModelEntry:
    """A built model plus server-side extras (help texts, reverse indexes, expression printer)."""

    def __init__(self, builder: KconfigModelBuilder, model: dict[str, Any], arch: dict[str, Any]) -> None:
        self.builder = builder
        self.model = model
        self.arch = arch
        self.compact = compact_model(model)
        self.help = builder.help_texts()
        self.menu_help = builder.menu_help()
        self.selected_by: dict[str, list[tuple[str, int]]] = {}
        self.implied_by: dict[str, list[tuple[str, int]]] = {}
        # Symbols named by each entry's own `depends on` lines (not inherited from menus/ifs)
        self.own_depends: dict[str, list[str]] = {}
        for target, refs in builder.references.items():
            for by in {r["by"] for r in refs if r["kind"] == "depends_on"}:
                self.own_depends.setdefault(by, []).append(target)
        for deps in self.own_depends.values():
            deps.sort()
        for name, sym in model["symbols"].items():
            for target, vis in sym["selects"]:
                self.selected_by.setdefault(target, []).append((name, vis))
            for target, vis in sym["implies"]:
                self.implied_by.setdefault(target, []).append((name, vis))

    def expr(self, eid: int) -> str:
        return self.builder.exprs.to_string(None if eid < 0 else eid)

    def evaluator(self) -> KconfigEvaluator:
        return KconfigEvaluator(self.model)


_MODELS: "RepoScopedCache" = RepoScopedCache()
# Guards _MODELS and _MODEL_BUILD_LOCKS only; a model is never built while it is held
_MODEL_LOCK = threading.Lock()
# One lock per model key, held for that key's build and dropped once it is cached
_MODEL_BUILD_LOCKS: dict[tuple[Any, ...], threading.Lock] = {}
# Concurrent model builds allowed across all keys (each costs ~1s of CPU and tens of MB)
_MODEL_BUILD_SEMAPHORE = threading.BoundedSemaphore(2)
# Bounded: keyed by revision, and any revision a caller names adds an entry
_LISTING_CACHE: "RepoScopedCache" = RepoScopedCache(maxsize=64)
# Separate and much smaller: each entry is a revision's whole file list (~4.5 MiB at v3.0)
_PATHS_CACHE: "RepoScopedCache" = RepoScopedCache(maxsize=4)
# Bounded: keyed by the requested symbol name, and each entry holds that symbol's reference list
_USAGE_CACHE: "RepoScopedCache" = RepoScopedCache(maxsize=256)
_BUILD_RESOLVERS: "RepoScopedCache" = RepoScopedCache()
_BUILD_LOCK = threading.Lock()


class KconfigService:
    """Kconfig configuration models, defconfigs, symbol details, usage and constraint services."""

    # ------------------------------------------------------------------ git access
    @staticmethod
    def _rev(version_name: str) -> str:
        """Git revision for a version name (numeric ids are resolved through the database).

        The result reaches `git grep` / `git ls-tree` as a positional argument, so it is validated
        here: every revision this service passes to git comes through this method.
        """
        ver = str(version_name or "v3.0").strip()
        if ver.isdigit():
            try:
                with get_db_cursor() as cursor:
                    _, vname = get_version_info(cursor, ver)
                    return safe_rev(vname or ver, "version")
            except HTTPException:
                raise
            except Exception as exc:
                logger.debug("Version id %s not resolvable: %s", ver, exc)
        return safe_rev(ver, "version")

    @staticmethod
    def _read(rev: str, path: str) -> str | None:
        try:
            raw, is_dir, _ = git_reader.read_file(rev, path)
        except Exception as exc:
            logger.debug("git read %s:%s failed: %s", rev, path, exc)
            return None
        if raw is None or is_dir:
            return None
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _safe_symbol(name: str) -> str:
        """A Kconfig symbol name (without the CONFIG_ prefix), or HTTP 400.

        The name reaches `git grep` patterns and keys the usage cache. A name carrying regex
        metacharacters would match far more than itself (`CONFIG_.*` finds every CONFIG_ reference
        in the tree), so the whole result would be read, parsed and cached under a name the caller
        chose. Patterns built from a validated name are still escaped at the call site.
        """
        clean = (name or "").strip()
        if clean.startswith("CONFIG_"):
            clean = clean[7:]
        if not is_valid_identifier(clean):
            raise HTTPException(status_code=400, detail="Invalid KConfig symbol name.")
        return clean

    @staticmethod
    def _git(args: list[str], timeout: float = 20.0) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=str(get_repo_dir()), capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            logger.debug("git %s failed: %s", args[:2], exc)
            return ""
        return proc.stdout if proc.returncode in (0, 1) else ""

    def _tree_listing(self, rev: str, path: str, recursive: bool = False) -> list[str]:
        key = ("ls", rev, path, recursive)
        # Read once into a local: both caches evict, so contains-then-get could race a miss
        listing = _LISTING_CACHE.get(key)
        if listing is None:
            args = ["ls-tree", "--name-only"] + (["-r"] if recursive else [])
            out = self._git(args + ([f"{rev}:{path}"] if path else [rev]))
            listing = [line.strip() for line in out.splitlines() if line.strip()]
            _LISTING_CACHE[key] = listing
        return listing

    def _all_paths(self, rev: str) -> set[str]:
        """Every path in the revision's tree. Kept in its own small cache: one entry is the whole
        file list (36,783 paths / ~4.5 MiB at v3.0, more in later releases), and any revision a
        caller names gets one, so only a few are retained."""
        paths = _PATHS_CACHE.get(rev)
        if paths is None:
            paths = set(self._git(["ls-tree", "-r", "--name-only", rev], timeout=60).splitlines())
            _PATHS_CACHE[rev] = paths
        return paths

    # ------------------------------------------------------------------ architectures
    def resolve_arch(self, version_name: str, arch: str | None) -> dict[str, Any]:
        """ARCH/SRCARCH/KERNELVERSION for an ARCH value, following the top Makefile."""
        rev = self._rev(version_name)
        arch_id = (arch or DEFAULT_ARCH).strip().lower()
        arch_id = _ARCH_SYNONYMS.get(arch_id, arch_id)
        makefile = self._read(rev, "Makefile") or ""
        aliases = arch_aliases(makefile)
        arch_dirs = set(self._tree_listing(rev, "arch"))
        srcarch = aliases.get(arch_id, arch_id)
        if srcarch not in arch_dirs:
            raise HTTPException(status_code=404, detail=f"Architecture '{arch_id}' does not exist in {rev}.")
        return {
            "rev": rev,
            "arch": arch_id,
            "srcarch": srcarch,
            "kernelversion": kernel_version_string(makefile),
        }

    def _root_kconfig(self, rev: str, srcarch: str) -> str:
        # Top-level Kconfig (sourcing arch/$SRCARCH/Kconfig) since 2.6.x; older trees start in arch/
        return "Kconfig" if self._read(rev, "Kconfig") is not None else f"arch/{srcarch}/Kconfig"

    @staticmethod
    def parse_toolchain(value: Any) -> dict[str, Any]:
        """Toolchain spec from a query parameter (JSON text) or a request body field (object)."""
        if not value:
            return {}
        if isinstance(value, dict):
            return value
        try:
            spec = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid toolchain JSON: {exc}") from exc
        if not isinstance(spec, dict):
            raise HTTPException(status_code=400, detail="toolchain must be a JSON object")
        return spec

    def toolchain_defaults(self, version_name: str, arch: str | None = None) -> dict[str, Any]:
        """Default gcc and clang profiles for a revision, and whether it has the Kconfig macro language."""
        info = self.resolve_arch(version_name, arch)
        kv = info["kernelversion"]
        return {
            "version": version_name,
            "arch": info["arch"],
            "kernelversion": kv,
            "macros": self._read(info["rev"], "scripts/kconfig/preprocess.c") is not None,
            "gcc": ToolchainProfile.from_dict({"cc": "gcc"}, kv).to_dict(),
            "clang": ToolchainProfile.from_dict({"cc": "clang"}, kv).to_dict(),
        }

    def get_model(self, version_name: str, arch: str | None = None, toolchain: Any = None) -> _ModelEntry:
        """Build (or fetch from the LRU cache) the finalized Kconfig model for one revision, ARCH and toolchain."""
        info = self.resolve_arch(version_name, arch)
        rev = info["rev"]
        try:
            profile = ToolchainProfile.from_dict(self.parse_toolchain(toolchain), info["kernelversion"])
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid toolchain profile: {exc}") from exc
        spec_hash = hashlib.sha1(json.dumps(profile.to_dict(), sort_keys=True).encode()).hexdigest()[:16]
        key = (rev, info["arch"], spec_hash)
        with _MODEL_LOCK:
            entry = _MODELS.get(key)
            if entry is not None:
                return entry
            # One lock per key: requests for the same model wait for that one build instead of
            # duplicating it, while requests for other models are not blocked behind it. Building
            # under the single global lock meant one ~1s build stalled every Kconfig request.
            build_lock = _MODEL_BUILD_LOCKS.setdefault(key, threading.Lock())

        with build_lock:
            with _MODEL_LOCK:
                entry = _MODELS.get(key)
                if entry is not None:
                    return entry
            try:
                # Caps how many models can be built at once, so distinct keys cannot be used to
                # occupy every worker thread with a fresh build
                with _MODEL_BUILD_SEMAPHORE:
                    entry = self._build_model(rev, info, profile, version_name)
            finally:
                with _MODEL_LOCK:
                    _MODEL_BUILD_LOCKS.pop(key, None)
            with _MODEL_LOCK:
                # Bounded LRU per repository (each model holds a few tens of MB once indexed)
                repo = get_active_repo_name()
                same_repo = [k for k in dict.keys(_MODELS) if k[0] == repo]
                if len(same_repo) >= _MODEL_CACHE_SIZE:
                    dict.pop(_MODELS, same_repo[0], None)
                _MODELS[key] = entry
            return entry

    def _build_model(self, rev: str, info: dict[str, Any], profile: ToolchainProfile,
                     version_name: str) -> _ModelEntry:
        """Parse and finalize one revision's Kconfig model. Runs without the model cache lock held."""
        read = lambda p: self._read(rev, p)  # noqa: E731
        env_vars = profile.environment(info["arch"], info["srcarch"], info["kernelversion"])
        env_vars.setdefault("KBUILD_DEFCONFIG", self._default_defconfig(rev, info["arch"], info["srcarch"]) or "")
        if info["arch"] == "um":
            env_vars.setdefault("SUBARCH", "x86")
        env = KconfigEnv(ARCH=env_vars["ARCH"], SRCARCH=env_vars["SRCARCH"], KERNELVERSION=env_vars["KERNELVERSION"], extra=env_vars)
        macros = read("scripts/kconfig/preprocess.c") is not None
        shell = pp = None
        if macros:
            shell = ToolchainShell(profile, env_vars, info["srcarch"], read)
            pp = KconfigPreprocessor(env_vars, shell)
            shell.pp = pp
        builder = KconfigModelBuilder(read, env, pp)
        model = builder.build(self._root_kconfig(rev, info["srcarch"]))
        model["version"] = version_name
        model["arch"] = info["arch"]
        model["srcarch"] = info["srcarch"]
        model["toolchain"] = self._toolchain_report(model, profile, env_vars, macros, pp, shell)
        return _ModelEntry(builder, model, info)

    @staticmethod
    def _toolchain_report(model: dict[str, Any], profile: ToolchainProfile, env_vars: dict[str, str], macros: bool,
                          pp: KconfigPreprocessor | None, shell: ToolchainShell | None) -> dict[str, Any]:
        """What the build environment contributed: variables read, probe answers, $(error-if) messages."""
        spans: dict[str, list[tuple[int, int, str]]] = {}
        for node in model["nodes"]:
            if node["symbol_name"] and node["file_path"]:
                spans.setdefault(node["file_path"], []).append((node["line_s"], node["line_e"], node["symbol_name"]))

        def symbol_at(file: str, line: int) -> str | None:
            hits = [(e - s, sym) for s, e, sym in spans.get(file, []) if s <= line <= e]
            return min(hits)[1] if hits else None

        probes = []
        for probe in (shell.probes.values() if shell else []):
            locations = [dict(loc, symbol=symbol_at(loc["file"], loc["line"])) for loc in probe["locations"]]
            probes.append({**probe, "locations": locations})
        if pp is not None:
            used = {k: v for k, v in pp.env_used.items() if not k.isdigit()}
        else:
            used = {s["env"]: env_vars.get(s["env"]) for s in model["symbols"].values() if s.get("env")}
        return {
            "macros": macros,
            "profile": profile.to_dict(),
            "summary": profile.summary(),
            "env": env_vars,
            "env_used": [{"name": k, "value": v, "overridden": k in profile.env} for k, v in sorted(used.items())],
            "probes": sorted(probes, key=lambda p: (p["kind"], p["key"])),
            "messages": list(pp.messages) if pp is not None else [],
        }

    def get_env_presets(self, version_name: str) -> dict[str, Any]:
        """Every ARCH value kconfig accepts at this revision, with its environment and default defconfig."""
        rev = self._rev(version_name)
        makefile = self._read(rev, "Makefile") or ""
        aliases = arch_aliases(makefile)
        kernelversion = kernel_version_string(makefile)
        arch_dirs = [d for d in self._tree_listing(rev, "arch") if "." not in d and d != "Kconfig"]
        presets: list[dict[str, Any]] = []
        for srcarch in sorted(arch_dirs):
            variants = [a for a, target in sorted(aliases.items()) if target == srcarch]
            if srcarch not in variants:
                variants.append(srcarch)
            # Prefer the 64-bit spelling first (x86_64 before i386 before the generic x86)
            variants.sort(key=lambda a: (0 if "64" in a else 1 if a != srcarch else 2, a))
            for arch_id in variants:
                defconfig = self._default_defconfig(rev, arch_id, srcarch)
                label = arch_id if arch_id == srcarch else f"{arch_id} ({srcarch})"
                if arch_id == srcarch and len(variants) > 1:
                    label = f"{arch_id} (generic: bitness chosen in menuconfig)"
                presets.append({
                    "id": arch_id,
                    "label": label,
                    "arch": arch_id,
                    "srcarch": srcarch,
                    "kconfig_path": f"arch/{srcarch}/Kconfig",
                    "canonical_defconfig": defconfig,
                    "bits": 64 if "64" in arch_id else None,
                    "env": {"ARCH": arch_id, "SRCARCH": srcarch, "KERNELVERSION": kernelversion},
                })
        presets.sort(key=lambda p: (0 if p["id"] == DEFAULT_ARCH else 1, p["srcarch"]))
        return {
            "version": version_name,
            "default_arch": DEFAULT_ARCH if any(p["id"] == DEFAULT_ARCH for p in presets) else (presets[0]["id"] if presets else None),
            "targets": presets,
            "architectures": presets,
            # Compiler identity only reaches Kconfig from Linux 4.18 on (CC_IS_GCC/CC_IS_CLANG are
            # computed by $(shell,...) macros, which this model does not evaluate)
            "compilers": [
                {"id": "gcc", "label": "GNU Compiler Collection (gcc)", "symbols": {"CC_IS_GCC": "y"}},
                {"id": "clang", "label": "LLVM Clang (clang)", "symbols": {"CC_IS_CLANG": "y"}},
            ],
        }

    def _default_defconfig(self, rev: str, arch_id: str, srcarch: str) -> str | None:
        """What `make ARCH=<arch> defconfig` loads: KBUILD_DEFCONFIG, else arch/$SRCARCH/defconfig."""
        arch_mk = self._read(rev, f"arch/{srcarch}/Makefile") or ""
        name = resolve_kbuild_defconfig(arch_mk, arch_id, srcarch)
        configs = self._tree_listing(rev, f"arch/{srcarch}/configs", recursive=True)
        if name and name in configs:
            return name
        if "defconfig" in self._tree_listing(rev, f"arch/{srcarch}"):
            return "defconfig"
        if f"{arch_id}_defconfig" in configs:
            return f"{arch_id}_defconfig"
        files = [c for c in configs if "defconfig" in c]
        return files[0] if files else None

    # ------------------------------------------------------------------ defconfigs
    def get_defconfigs(self, version_name: str, arch: str = DEFAULT_ARCH) -> dict[str, Any]:
        """List arch/$SRCARCH/configs/* (and arch/$SRCARCH/defconfig) with the default one flagged."""
        info = self.resolve_arch(version_name, arch)
        rev, srcarch = info["rev"], info["srcarch"]
        default = self._default_defconfig(rev, info["arch"], srcarch)
        items: list[dict[str, Any]] = []
        if "defconfig" in self._tree_listing(rev, f"arch/{srcarch}"):
            items.append({"name": "defconfig", "file_path": f"arch/{srcarch}/defconfig"})
        # Nested ones (powerpc: configs/40x/acadia_defconfig) load as `make 40x/acadia_defconfig`
        for name in sorted(self._tree_listing(rev, f"arch/{srcarch}/configs", recursive=True)):
            items.append({"name": name, "file_path": f"arch/{srcarch}/configs/{name}"})
        canonical = None
        for item in items:
            item["fid"] = 0
            item["is_canonical"] = item["name"] == default and canonical is None
            if item["is_canonical"]:
                canonical = item
        return {
            "version": version_name,
            "arch": info["arch"],
            "arch_dir": srcarch,
            "total_count": len(items),
            "canonical_default": canonical,
            "defconfigs": items,
        }

    def get_defconfig_content(self, version_name: str, name_or_path: str = "", arch: str = DEFAULT_ARCH, file_path: str = "", toolchain: Any = None) -> dict[str, Any]:
        """Raw defconfig text plus its literal assignments (nothing injected: kconfig derives the rest)."""
        info = self.resolve_arch(version_name, arch)
        rev, srcarch = info["rev"], info["srcarch"]
        clean = (file_path or name_or_path or "").strip().lstrip("/")
        if not clean:
            clean = self._default_defconfig(rev, info["arch"], srcarch) or ""
        if clean.startswith("arch/"):
            candidates = [clean]
        elif clean.startswith("configs/"):
            candidates = [f"arch/{srcarch}/{clean}"]
        else:
            candidates = [f"arch/{srcarch}/configs/{clean}", f"arch/{srcarch}/{clean}", f"arch/{srcarch}/configs/{clean}_defconfig"]
        content, resolved = None, candidates[0]
        for cand in candidates:
            content = self._read(rev, cand)
            if content is not None:
                resolved = cand
                break
        if content is None:
            raise HTTPException(status_code=404, detail=f"Defconfig '{clean}' not found for {info['arch']} in {rev}.")
        values = self.import_file(version_name, {"content": content})["symbols"]
        bits = None
        try:
            ev = self.get_model(version_name, info["arch"], toolchain).evaluator()
            ev.load_config(content)
            ev.set_all_new_symbols("default")
            if "64BIT" in ev.syms:
                bits = 64 if ev.value("64BIT") == "y" else 32
        except HTTPException:
            pass
        return {
            "version": version_name,
            "name": os.path.basename(resolved),
            "file_path": resolved,
            "arch": info["arch"],
            "srcarch": srcarch,
            "bits": bits,
            "symbol_count": len(values),
            "values": values,
            "content": content,
        }

    # ------------------------------------------------------------------ tree / model
    def get_tree(
        self,
        version_name: str,
        arch: str = DEFAULT_ARCH,
        include_tree: bool = False,
        include_relations: bool = False,
        include_nodes: bool = False,
        toolchain: Any = None,
    ) -> dict[str, Any]:
        """The finalized configuration model (compact transport form) for the client-side engine."""
        entry = self.get_model(version_name, arch, toolchain)
        res = dict(entry.compact)
        res["version"] = version_name
        res["total_nodes"] = res["total_count"] = len(entry.model["nodes"])
        if include_nodes or include_tree:
            res["nodes"] = [self._legacy_node(entry, n) for n in entry.model["nodes"][1:]]
        if include_relations:
            relations: dict[str, dict[str, list[str]]] = {}
            reverse: dict[str, dict[str, list[dict[str, Any]]]] = {}
            for ref_target, refs in entry.builder.references.items():
                for ref in refs:
                    kind = {"depends_on": "depends_on", "select": "selects", "imply": "implies"}.get(ref["kind"])
                    if kind:
                        relations.setdefault(ref["by"], {"depends_on": [], "selects": [], "implies": []})[kind].append(ref_target)
            for target, by in entry.selected_by.items():
                reverse.setdefault(target, {"selected_by": [], "implied_by": []})["selected_by"] = [{"name": n, "cond": entry.expr(v)} for n, v in by]
            for target, by in entry.implied_by.items():
                reverse.setdefault(target, {"selected_by": [], "implied_by": []})["implied_by"] = [{"name": n, "cond": entry.expr(v)} for n, v in by]
            res["relations"] = relations
            res["reverse_relations"] = reverse
        return res

    def _legacy_node(self, entry: _ModelEntry, node: dict[str, Any]) -> dict[str, Any]:
        """Readable per-entry view (tree_id/parent_id/node_type...) with relation names resolved."""
        sym_name = node["symbol_name"]
        sym = entry.model["symbols"].get(sym_name) if sym_name else None
        out = {k: v for k, v in node.items() if k != "children"}
        out["parent_id"] = max(node["parent_id"], 0)
        out["depends_on_expr"] = entry.expr(node["dep"]) if node["dep"] >= 0 else ""
        out["depends_on"] = []
        if sym is not None:
            out["type"] = TYPE_CODES.get(sym["type"], 0)
            out["type_name"] = sym["type"]
            out["selects"] = [{"target": t, "cond": entry.expr(v)} for t, v in sym["selects"]]
            out["implies"] = [{"target": t, "cond": entry.expr(v)} for t, v in sym["implies"]]
            out["selected_by"] = [{"name": n, "cond": entry.expr(v)} for n, v in entry.selected_by.get(sym_name, [])]
            out["implied_by"] = [{"name": n, "cond": entry.expr(v)} for n, v in entry.implied_by.get(sym_name, [])]
            out["defaults"] = [{"value": entry.expr(v), "cond": entry.expr(c) if c >= 0 else None} for v, c in sym["defaults"]]
            out["depends_on"] = entry.own_depends.get(sym_name, [])
            if sym.get("choice"):
                out["choice"] = {"id": sym["choice"], "members": entry.model["symbols"][sym["choice"]]["members"]}
        else:
            out.update({"selects": [], "implies": [], "selected_by": [], "implied_by": [], "defaults": []})
        return out

    # ------------------------------------------------------------------ symbols
    def _definitions(self, entry: _ModelEntry, name: str) -> list[dict[str, Any]]:
        nodes = entry.model["nodes"]
        out = []
        for mid in entry.model["symbols"][name]["menus"]:
            node = nodes[mid]
            out.append({
                "file_path": node["file_path"],
                "line_s": node["line_s"],
                "line_e": node["line_e"],
                "prompt": node["prompt"],
                "kind": node["kind"],
                "depends_on_expr": entry.expr(node["dep"]) if node["dep"] >= 0 else "",
                "visible_expr": entry.expr(node["pvis"]) if node["pvis"] >= 0 else ("" if node["prompt"] else None),
                "help": entry.menu_help.get(mid, ""),
                "menu_path": self._menu_path(entry, mid),
            })
        return out

    @staticmethod
    def _menu_path(entry: _ModelEntry, mid: int) -> list[str]:
        """Prompts of the enclosing menus (menuconfig's "Location:")."""
        nodes = entry.model["nodes"]
        path: list[str] = []
        parent = nodes[mid]["parent_id"]
        while parent > 0:
            node = nodes[parent]
            if node["prompt"]:
                path.append(node["prompt"])
            parent = node["parent_id"]
        return list(reversed(path))

    def _other_arch_definitions(self, rev: str, name: str) -> list[dict[str, Any]]:
        out = self._git(["grep", "-n", "-E", rf"^\s*(menu)?config\s+{re.escape(name)}\s*$", rev, "--", "*Kconfig*"])
        defs = []
        for line in out.splitlines()[:50]:
            parts = line.split(":", 3)
            if len(parts) == 4 and parts[2].isdigit():
                defs.append({"file_path": parts[1], "line_s": int(parts[2]), "line_e": int(parts[2])})
        return defs

    def get_symbol_detail(self, version_name: str, name: str, arch: str = DEFAULT_ARCH, toolchain: Any = None) -> dict[str, Any]:
        """Arch-scoped definition(s), dependencies, reverse dependencies, help and compiled files of a symbol."""
        clean = (name or "").strip()
        if clean.startswith("CONFIG_"):
            clean = clean[7:]
        entry = self.get_model(version_name, arch, toolchain)
        sym = entry.model["symbols"].get(clean)
        rev = entry.arch["rev"]
        if sym is None or sym.get("is_choice"):
            others = self._other_arch_definitions(rev, clean)
            if not others:
                raise HTTPException(status_code=404, detail=f"KConfig symbol '{clean}' not found in {version_name}.")
            first = others[0]
            return {
                "version": version_name, "arch": entry.arch["arch"], "name": clean, "symbol_name": clean,
                "defined_for_arch": False, "type": 0, "type_name": "unknown", "prompt": "", "help": "",
                "file_path": first["file_path"], "line": first["line_s"], "line_s": first["line_s"], "line_e": first["line_e"],
                "definition": {**first, "file": first["file_path"], "line": first["line_s"]},
                "declarations": others, "definitions": [], "depends_on": [], "selects": [], "selected_by": [],
                "implies": [], "implied_by": [], "defaults": [], "compiled_files": [], "kbuild": [],
                "kconfig_refs": [], "vid_s": None, "vid_e": 0, "vname_s": version_name, "vname_e": "Active",
                "added_version": version_name, "is_active": True, "lifecycle_status": "Active", "def_val": "",
            }
        definitions = self._definitions(entry, clean)
        primary = definitions[0] if definitions else {"file_path": "", "line_s": 1, "line_e": 1}
        prompts = [d["prompt"] for d in definitions if d["prompt"]]
        defaults = [{"value": entry.expr(v), "cond": entry.expr(c) if c >= 0 else None} for v, c in sym["defaults"]]
        kbuild = self.kbuild_usage(version_name, clean, entry.arch["arch"])
        detail = {
            "version": version_name,
            "arch": entry.arch["arch"],
            "srcarch": entry.arch["srcarch"],
            "name": clean,
            "symbol_name": clean,
            "defined_for_arch": True,
            "type": TYPE_CODES.get(sym["type"], 0),
            "type_name": sym["type"],
            "prompt": prompts[0] if prompts else "",
            "prompts": prompts,
            "help": entry.help.get(clean, ""),
            "file_path": primary["file_path"],
            "line": primary["line_s"],
            "line_s": primary["line_s"],
            "line_e": primary["line_e"],
            "definition": {**primary, "file": primary["file_path"], "line": primary["line_s"]},
            "declarations": [{k: d[k] for k in ("file_path", "line_s", "line_e")} for d in definitions],
            "definitions": definitions,
            "dir_dep_expr": entry.expr(sym["dir_dep"]) if sym["dir_dep"] >= 0 else "",
            "rev_dep_expr": entry.expr(sym["rev_dep"]) if sym["rev_dep"] >= 0 else "",
            "implied_expr": entry.expr(sym["implied"]) if sym["implied"] >= 0 else "",
            "depends_on": entry.own_depends.get(clean, []),
            "selects": [t for t, _ in sym["selects"]],
            "select_conditions": [{"target": t, "cond": entry.expr(v)} for t, v in sym["selects"]],
            "implies": [t for t, _ in sym["implies"]],
            "selected_by": sorted({n for n, _ in entry.selected_by.get(clean, [])}),
            "implied_by": sorted({n for n, _ in entry.implied_by.get(clean, [])}),
            "defaults": defaults,
            "def_val": defaults[0]["value"] if defaults else "",
            "ranges": [{"min": entry.expr(lo), "max": entry.expr(hi), "cond": entry.expr(c) if c >= 0 else None} for lo, hi, c in sym["ranges"]],
            "choice": sym.get("choice"),
            "choice_members": entry.model["symbols"][sym["choice"]]["members"] if sym.get("choice") else [],
            "env": sym.get("env"),
            "kconfig_refs": entry.builder.references.get(clean, [])[:_USAGE_LIMIT],
            "kbuild": kbuild,
            "compiled_files": self._compiled_files(kbuild, clean, version_name),
            # Lifecycle fields kept for API compatibility (the model is per revision)
            "vid_s": None,
            "vid_e": 0,
            "vname_s": version_name,
            "vname_e": "Active",
            "added_version": version_name,
            "is_active": True,
            "lifecycle_status": "Active",
        }
        return detail

    # ------------------------------------------------------------------ usage (git)
    def kbuild_usage(self, version_name: str, name: str, arch: str = DEFAULT_ARCH) -> list[dict[str, Any]]:
        """Makefile/Kbuild rules governed by CONFIG_<name>: objects and directories it builds."""
        name = self._safe_symbol(name)
        info = self.resolve_arch(version_name, arch)
        rev, srcarch = info["rev"], info["srcarch"]
        key = ("kbuild", rev, name)
        rules = _USAGE_CACHE.get(key)
        if rules is None:
            # -F: the symbol name is a literal, not a pattern (without it git grep reads it as a
            # basic regex, where `.` and `*` would still match beyond the name)
            out = self._git(["grep", "-l", "-w", "-F", "-e", f"CONFIG_{name}", rev, "--", "*Makefile*", "*Kbuild*"])
            makefiles = sorted({line.split(":", 1)[1] for line in out.splitlines() if ":" in line})
            paths = self._all_paths(rev)
            parser = KbuildParser()
            rules = []
            for mk in makefiles[:200]:
                content = self._read(rev, mk)
                if not content:
                    continue
                for b in parser.parse_makefile_content(content, dir_path=os.path.dirname(mk)):
                    if b.symbol_name != name and name not in b.cond_symbols:
                        continue
                    target = b.source_file_rel
                    exists = target in paths
                    if not exists and target.endswith(".c") and target[:-2] + ".S" in paths:
                        target, exists = target[:-2] + ".S", True
                    if b.is_directory:
                        target = target[: -len("/Makefile")] + "/" if target.endswith("/Makefile") else target
                        exists = any(p.startswith(target) for p in paths) if not exists else True
                    rules.append({
                        "makefile": mk,
                        "line": b.source_line,
                        "rule": b.details,
                        "target_obj": b.target_obj,
                        "path": target,
                        "exists": exists,
                        "kind": "directory" if b.is_directory else ("composite" if b.is_composite else "object"),
                        "via": "condition" if b.symbol_name != name else "rule",
                        "compile_mode": {1: "built-in", 2: "module", 3: "conditional"}.get(b.compile_mode, "conditional"),
                    })
            _USAGE_CACHE[key] = rules
        trees = self._arch_trees(rev, info["arch"])
        return [dict(r, other_arch=is_other_arch_path(r["path"], srcarch, trees)) for r in rules]

    def _compiled_files(self, kbuild: list[dict[str, Any]], name: str, version_name: str) -> list[dict[str, Any]]:
        files: dict[str, dict[str, Any]] = {}
        for rule in kbuild:
            if rule["kind"] == "directory" or not rule["exists"]:
                continue
            files.setdefault(rule["path"], {
                "file_path": rule["path"], "target_obj": rule["target_obj"], "compile_mode": rule["compile_mode"],
                "makefile": rule["makefile"], "line": rule["line"], "other_arch": rule["other_arch"],
            })
        # Rows indexed by the parsing pipeline (m_kconfig_kbuild) complement the Git scan
        try:
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT fn.fname, kb.target_obj
                    FROM m_kconfig_kbuild kb
                    JOIN m_kconfig_symbol s ON s.kcid = kb.kcid
                    JOIN m_file_name fn ON kb.target_fnid = fn.fnid
                    WHERE s.name = %s
                    LIMIT 500;
                    """,
                    (name,),
                )
                for row in cursor.fetchall():
                    path = safe_decode(row["fname"])
                    files.setdefault(path, {"file_path": path, "target_obj": safe_decode(row["target_obj"]), "compile_mode": "conditional"})
        except Exception as exc:
            logger.debug("m_kconfig_kbuild lookup skipped: %s", exc)
        return sorted(files.values(), key=lambda f: f["file_path"])

    # ------------------------------------------------------------------ build conditions (git)
    def _included_sources(self, rev: str) -> set[str]:
        """Basenames of .c/.S files that another file #includes (compiled inside it, not on their own)."""
        key = ("incsrc", rev)
        # Read into a local: the cache evicts, so re-reading after the write could miss
        sources = _USAGE_CACHE.get(key)
        if sources is None:
            out = self._git(["grep", "-h", "-o", "-E", _INCLUDED_SOURCE_RE, rev, "--", "*.c", "*.h", "*.S"], timeout=60)
            sources = {re.split(r'[/"<]', line.strip().rstrip('">'))[-1] for line in out.splitlines() if line.strip()}
            _USAGE_CACHE[key] = sources
        return sources

    @staticmethod
    def _arch_trees(rev: str, arch: str) -> set[str] | None:
        """Architecture trees of the ARCH's build layout (None: fall back to own_arch_dirs)."""
        from webapp.backend.services.arch_layout_service import arch_layout_service

        layout = arch_layout_service.layout(rev, arch)
        return layout.arch_trees() if layout else None

    def _build_resolver(self, version_name: str, arch: str) -> tuple[KbuildConditionResolver, dict[str, Any]]:
        info = self.resolve_arch(version_name, arch)
        rev = info["rev"]
        key = (rev, info["arch"])
        with _BUILD_LOCK:
            entry = _BUILD_RESOLVERS.get(key)
            if entry is None:
                paths = self._all_paths(rev)
                entry = KbuildConditionResolver(
                    lambda p: self._read(rev, p) if p in paths else None, paths.__contains__,
                    info["arch"], info["srcarch"], self._included_sources(rev), self._arch_trees(rev, info["arch"]),
                )
                if len(_BUILD_RESOLVERS) >= _BUILD_RESOLVER_CACHE_SIZE:
                    dict.pop(_BUILD_RESOLVERS, next(iter(_BUILD_RESOLVERS)))  # oldest (keys are repo-scoped)
                _BUILD_RESOLVERS[key] = entry
        return entry, info

    def build_conditions(self, version_name: str, paths: list[str], arch: str = DEFAULT_ARCH) -> dict[str, Any]:
        """Kbuild condition of each file: under which configuration `make ARCH=<arch>` compiles it.

        Each entry is `{status, expr, rules, reason}` (see parser/kbuild_conditions.py): `expr` is a
        tristate expression over CONFIG_* symbols the client evaluates against its configuration.
        """
        resolver, info = self._build_resolver(version_name, arch)
        files: dict[str, Any] = {}
        with _BUILD_LOCK:
            for path in dict.fromkeys(p.strip().strip("/") for p in paths[:_BUILD_PATHS_LIMIT] if p and p.strip()):
                files[path] = resolver.file_condition(path)
        return {"version": version_name, "arch": info["arch"], "srcarch": info["srcarch"], "files": files}

    def get_symbol_usage(self, version_name: str, name: str, arch: str = DEFAULT_ARCH, limit: int = _USAGE_LIMIT, toolchain: Any = None) -> dict[str, Any]:
        """Where CONFIG_<name> is used: Kbuild rules, C/assembly/linker sources, and other Kconfig entries."""
        clean = self._safe_symbol(name)
        limit = max(1, min(int(limit or _USAGE_LIMIT), _USAGE_REF_LIMIT))
        entry = self.get_model(version_name, arch, toolchain)
        rev, srcarch = entry.arch["rev"], entry.arch["srcarch"]
        key = ("src", rev, clean)
        refs = _USAGE_CACHE.get(key)
        if refs is None:
            out = self._git(["grep", "-n", "-w", "-E", rf"CONFIG_{re.escape(clean)}(_MODULE)?", rev, "--",
                             "*.c", "*.h", "*.S", "*.lds", "*.lds.S", "*.dts", "*.dtsi"])
            refs = []
            for line in out.splitlines():
                parts = line.split(":", 3)
                if len(parts) == 4 and parts[2].isdigit():
                    refs.append({"file_path": parts[1], "line": int(parts[2]), "text": parts[3].strip()[:200]})
                # A symbol with more references than this is reported truncated rather than cached whole
                if len(refs) >= _USAGE_REF_LIMIT:
                    break
            _USAGE_CACHE[key] = refs
        trees = self._arch_trees(rev, entry.arch["arch"])
        sources = [dict(r, other_arch=is_other_arch_path(r["file_path"], srcarch, trees)) for r in refs]
        return {
            "version": version_name,
            "arch": entry.arch["arch"],
            "name": clean,
            "kbuild": self.kbuild_usage(version_name, clean, entry.arch["arch"]),
            "source_refs": sources[:limit],
            "source_ref_count": len(sources),
            "source_refs_truncated": len(refs) >= _USAGE_REF_LIMIT,
            "source_file_count": len({r["file_path"] for r in sources}),
            "kconfig_refs": entry.builder.references.get(clean, [])[:limit],
        }

    def get_graph(self, version_name: str, symbol_or_arch: str = "x86", depth: int = 2, arch: str = DEFAULT_ARCH, toolchain: Any = None) -> dict[str, Any]:
        """Direct relations of a symbol: its own dependencies (left) and what it selects/implies (right)."""
        target = (symbol_or_arch or "").strip()
        if target.startswith("CONFIG_"):
            target = target[7:]
        entry = self.get_model(version_name, arch, toolchain)
        nodes = [{"id": target, "label": target, "is_root": True, "type": "symbol"}]
        edges: list[dict[str, Any]] = []
        sym = entry.model["symbols"].get(target)
        if sym is not None:
            for dep in entry.own_depends.get(target, []):
                nodes.append({"id": dep, "label": dep, "is_root": False, "type": "dependency"})
                edges.append({"from": target, "to": dep, "type": "depends_on"})
            for kind, items in (("selects", sym["selects"]), ("implies", sym["implies"])):
                for t, _ in items:
                    nodes.append({"id": t, "label": t, "is_root": False, "type": kind[:-1]})
                    edges.append({"from": target, "to": t, "type": kind})
            for n, _ in entry.selected_by.get(target, []):
                nodes.append({"id": n, "label": n, "is_root": False, "type": "selected_by"})
                edges.append({"from": n, "to": target, "type": "selects"})
        return {"version": version_name, "arch": entry.arch["arch"], "symbol": target, "nodes": nodes, "edges": edges}

    def search_symbols(self, version_name: str, q: str = "", limit: int = 50, offset: int = 0, arch: str | None = None, toolchain: Any = None) -> dict[str, Any]:
        """Search symbol names and prompts (CONFIG_ prefix optional) in the arch model."""
        clean_q = q.strip()
        if clean_q.startswith("CONFIG_"):
            clean_q = clean_q[7:]
        needle = clean_q.lower()
        entry = self.get_model(version_name, arch, toolchain)
        nodes = entry.model["nodes"]
        hits = []
        for sym_name, sym in entry.model["symbols"].items():
            if sym.get("is_choice"):
                continue
            prompt = next((nodes[m]["prompt"] for m in sym["menus"] if nodes[m]["prompt"]), "") or ""
            if needle in sym_name.lower() or needle in prompt.lower():
                first = nodes[sym["menus"][0]] if sym["menus"] else None
                hits.append({
                    "name": sym_name,
                    "type": TYPE_CODES.get(sym["type"], 0),
                    "type_name": sym["type"],
                    "prompt": prompt,
                    "help": entry.help.get(sym_name, ""),
                    "file_path": first["file_path"] if first else "",
                    "line_s": first["line_s"] if first else 0,
                })
        hits.sort(key=lambda h: (h["name"].lower() != needle, not h["name"].lower().startswith(needle), h["name"]))
        page = hits[offset:offset + limit]
        return {"version": version_name, "arch": entry.arch["arch"], "query": q, "count": len(page), "total": len(hits), "symbols": page}

    # ------------------------------------------------------------------ configurations
    @staticmethod
    def _assignments_to_config(symbols: dict[str, Any]) -> str:
        """Render {NAME: value} as .config lines, so it is loaded with exact conf_read semantics."""
        lines = []
        for raw_name, val in symbols.items():
            name = raw_name[7:] if raw_name.startswith("CONFIG_") else raw_name
            text = "" if val is None else str(val)
            if text in ("n", "N", "false", "False"):
                lines.append(f"# CONFIG_{name} is not set")
            elif text in ("y", "Y", "true", "True"):
                lines.append(f"CONFIG_{name}=y")
            elif text in ("m", "M"):
                lines.append(f"CONFIG_{name}=m")
            else:
                lines.append(f'CONFIG_{name}="{escape_string(text)}"' if not re.fullmatch(r"-?\d+|0[xX][0-9a-fA-F]+", text) else f"CONFIG_{name}={text}")
        return "\n".join(lines) + "\n"

    def _load(self, version_name: str, arch: str | None, payload: dict[str, Any]) -> tuple[_ModelEntry, KconfigEvaluator, dict[str, Any]]:
        entry = self.get_model(version_name, arch, payload.get("toolchain"))
        ev = entry.evaluator()
        content = payload.get("content")
        if content is None:
            symbols = payload.get("symbols") or payload.get("current_values") or payload.get("assignments") or {}
            # String-typed symbols must be quoted in .config syntax; types come from the model
            lines = []
            for raw_name, val in symbols.items():
                bare = raw_name[7:] if raw_name.startswith("CONFIG_") else raw_name
                t = ev.syms.get(bare, {}).get("type")
                if t == "string":
                    lines.append(f'CONFIG_{bare}="{escape_string(str(val))}"')
                else:
                    lines.append(self._assignments_to_config({bare: val}).strip())
            content = "\n".join(lines) + "\n"
        report = ev.load_config(content)
        return entry, ev, report

    def export_file(self, version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """.config text for assignments. With `arch`, the assignments are resolved exactly like
        `make olddefconfig` would (`format`: "config" full file, "defconfig" minimal savedefconfig)."""
        symbols = payload.get("symbols") or payload.get("assignments") or {}
        arch = payload.get("arch")
        if not arch:
            body = self._assignments_to_config(dict(sorted(symbols.items())))
            content = "#\n# Automatically generated by KernelInfo-Parser Web Menuconfig\n" f"# Linux Kernel Version {version_name} Configuration\n#\n" + body
            return {"version": version_name, "content": content, "symbol_count": len(symbols), "resolved": False}
        entry, ev, report = self._load(version_name, arch, {"symbols": symbols, "toolchain": payload.get("toolchain")})
        ev.set_all_new_symbols("default")
        if payload.get("format") == "defconfig":
            content = ev.write_defconfig()
        else:
            content = ev.write_config()
        return {
            "version": version_name,
            "arch": entry.arch["arch"],
            "content": content,
            "symbol_count": len(symbols),
            "resolved": True,
            "unknown_symbols": report["unknown"],
            "invalid_values": report["invalid"],
        }

    def import_file(self, version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Parse .config text into assignments (with `arch`: also report unknown/invalid entries)."""
        content = payload.get("content", "") or ""
        symbols: dict[str, str] = {}
        for line in content.splitlines():
            stripped = line.strip()
            m = re.match(r"^# CONFIG_(\S+) is not set", stripped)
            if m:
                symbols[m.group(1)] = "n"
                continue
            if stripped.startswith("CONFIG_") and "=" in stripped:
                name, val = stripped[7:].split("=", 1)
                if val.startswith('"'):
                    out, i = [], 1
                    while i < len(val) and val[i] != '"':
                        if val[i] == "\\" and i + 1 < len(val):
                            i += 1
                        out.append(val[i])
                        i += 1
                    val = "".join(out)
                symbols[name.strip()] = val
        res: dict[str, Any] = {"version": version_name, "symbol_count": len(symbols), "symbols": symbols}
        if payload.get("arch"):
            _, _, report = self._load(version_name, payload["arch"], {"content": content, "toolchain": payload.get("toolchain")})
            res["unknown_symbols"] = report["unknown"]
            res["invalid_values"] = report["invalid"]
            res["warnings"] = report["warnings"]
        return res

    def validate_assignments(self, version_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve assignments through kconfig and report every value kconfig would not keep."""
        symbols = payload.get("symbols") or {}
        entry, ev, report = self._load(version_name, payload.get("arch"), {"symbols": symbols, "toolchain": payload.get("toolchain")})
        forced: dict[str, str] = {}
        adjusted: dict[str, str] = {}
        details: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for raw_name, val in symbols.items():
            name = raw_name[7:] if raw_name.startswith("CONFIG_") else raw_name
            if name not in ev.syms or ev.syms[name].get("auto") or ev.syms[name]["type"] == "unknown":
                continue
            want = str(val)
            got = ev.value(name)
            if ev.syms[name]["type"] in ("bool", "tristate"):
                want = {"true": "y", "false": "n"}.get(want.lower(), want.lower()[:1])
            if want == got:
                continue
            if ev.rev_dep(name) > 0 and got in ("y", "m") and want in ("n", "m"):
                reason = "forced by select"
                forced[name] = got
            elif ev.visibility(name) == 0:
                prompts = " || ".join(ev.expr_str(p, with_values=True) for p in ev.syms[name]["prompts"])
                reason = f"prompt not visible: {prompts}" if prompts else "no prompt (value comes from defaults)"
                adjusted[name] = got
            else:
                reason = "limited by dependencies or range"
                adjusted[name] = got
            details.append({"symbol": name, "requested": want, "value": got, "reason": reason,
                            "depends_on": ev.expr_str(ev.syms[name]["dir_dep"], with_values=True)})
        for name, s in ev.syms.items():
            if s["type"] in ("bool", "tristate") and not s.get("choice") and ev.rev_dep(name) > 0 and ev.dir_dep(name) == 0:
                conflicts.append({
                    "symbol": name,
                    "message": f"{name} is selected while its direct dependencies are unmet",
                    "selected_by": ev.expr_str(s["rev_dep"], with_values=True),
                    "depends_on": ev.expr_str(s["dir_dep"], with_values=True),
                })
        return {
            "version": version_name,
            "arch": entry.arch["arch"],
            "forced_symbols": forced,
            "adjusted_symbols": adjusted,
            "details": details,
            "conflicts": conflicts,
            "unknown_symbols": report["unknown"],
            "invalid_values": report["invalid"],
        }

    def autosolve(self, version_name: str, req: Any) -> dict[str, Any]:
        """Legal user changes that make a symbol reach the requested value from the current config."""
        data = req if isinstance(req, dict) else {
            "target_symbol": getattr(req, "target_symbol", ""),
            "current_values": getattr(req, "current_values", {}) or {},
            "arch": getattr(req, "arch", None),
            "value": getattr(req, "value", None),
            "toolchain": getattr(req, "toolchain", None),
        }
        sym_name = (data.get("target_symbol") or data.get("symbol") or "").strip()
        if sym_name.startswith("CONFIG_"):
            sym_name = sym_name[7:]
        entry, ev, _ = self._load(version_name, data.get("arch"), {"symbols": data.get("current_values") or {}, "toolchain": data.get("toolchain")})
        want = 1 if str(data.get("value") or "y").lower() == "m" else 2
        result = solve(ev, sym_name, want)
        result.update({"version": version_name, "arch": entry.arch["arch"], "target": sym_name, "target_symbol": sym_name})
        return result

    def diff_configurations(self, version_name: str, req: Any) -> dict[str, Any]:
        """Compare two KConfig configuration dictionaries."""
        if isinstance(req, dict):
            cfg1 = req.get("active_config", {})
            cfg2 = req.get("custom_config", {})
        else:
            cfg1 = getattr(req, "active_config", {})
            cfg2 = getattr(req, "custom_config", {})

        all_keys = set(cfg1.keys()) | set(cfg2.keys())
        matching = 0
        mismatched = 0
        diffs = []
        for k in sorted(all_keys):
            v1 = cfg1.get(k)
            v2 = cfg2.get(k)
            if v1 == v2:
                matching += 1
            else:
                mismatched += 1
                diffs.append({"symbol": k, "val1": v1, "val2": v2})

        return {
            "version": version_name,
            "matching_symbols": matching,
            "mismatched_symbols": mismatched,
            "differences": diffs,
        }
