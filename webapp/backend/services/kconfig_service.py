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
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from parser.kbuild_conditions import KbuildConditionResolver, is_other_arch_path
from parser.kbuild_parser import KbuildParser
from parser import kbuild_symbols
from parser.kconfig_ast.kconfig_eval import KconfigEvaluator, escape_string, solve
from parser.kconfig_ast.kconfig_preprocess import KconfigPreprocessor
from parser.kconfig_ast.kconfig_toolchain import ToolchainProfile, ToolchainShell
from parser.kconfig_ast.kconfig_model import (
    MODEL_FORMAT,
    ExprTable,
    KconfigEnv,
    KconfigModelBuilder,
    arch_aliases,
    compact_model,
    kernel_version_string,
    resolve_kbuild_defconfig,
)
from webapp.backend.config import RepoScopedCache, get_active_repo_name, get_repo_dir
from webapp.backend.database.helpers import get_version_info
from db_engine.spec import Join
from webapp.backend.database.pool import get_reader
from webapp.backend.security.git import safe_rev
from webapp.backend.services.tree_resolver import git_tree_resolver
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.disk_cache import DiskCache, module_fingerprint

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

    def __init__(self, model: dict[str, Any], arch: dict[str, Any], extras: dict[str, Any]) -> None:
        self.model = model
        self.arch = arch
        self.compact = compact_model(model)
        self.help: dict[str, str] = extras["help"]
        self.menu_help: dict[int, str] = {int(k): v for k, v in extras["menu_help"].items()}
        # Direct references made by entries' own properties (KconfigModelBuilder.references)
        self.references: dict[str, list[dict[str, Any]]] = extras["references"]
        self.exprs = ExprTable()
        self.exprs.nodes = [tuple(node) for node in model["exprs"]]
        self.selected_by: dict[str, list[tuple[str, int]]] = {}
        # Symbols named by each entry's own `depends on` lines (not inherited from menus/ifs)
        self.own_depends: dict[str, list[str]] = {}
        for target, refs in self.references.items():
            for by in {r["by"] for r in refs if r["kind"] == "depends_on"}:
                self.own_depends.setdefault(by, []).append(target)
        for deps in self.own_depends.values():
            deps.sort()
        for name, sym in model["symbols"].items():
            for target, vis in sym["selects"]:
                self.selected_by.setdefault(target, []).append((name, vis))

    def expr(self, eid: int) -> str:
        return self.exprs.to_string(None if eid < 0 else eid)

    def evaluator(self) -> KconfigEvaluator:
        return KconfigEvaluator(self.model)


# Built models persist across restarts as gzipped JSON, keyed by commit SHA, ARCH, toolchain and the
# model code (a cold build of a modern tree costs ~2 s); KERNELINFO_CACHE_DIR overrides the location
# (services/disk_cache.py, webapp/API.md section 2.14)
_MODEL_DISK = DiskCache("kconfig-models", max_bytes=512 << 20, max_entries=32)
# Basenames of the .c/.S files other files #include, per commit SHA (immutable)
_INCLUDED_DISK = DiskCache("included-sources", max_bytes=16 << 20, max_entries=256)
_MODEL_CODE_FILES = ("kconfig_lexer.py", "kconfig_parser.py", "kconfig_model.py", "kconfig_preprocess.py", "kconfig_toolchain.py")


def _code_fingerprint() -> str:
    """Hash of the model-building code: a cached model is only reused by the code that built it."""
    return module_fingerprint(*(f"parser/kconfig_ast/{name}" for name in _MODEL_CODE_FILES), salt=str(MODEL_FORMAT))


def _disk_load(key: str) -> dict[str, Any] | None:
    data = _MODEL_DISK.get(key)
    return data if isinstance(data, dict) else None


def _disk_store(key: str, data: dict[str, Any]) -> None:
    _MODEL_DISK.put(key, data)


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
# Commit SHA -> {(blob_id, fnid): path} of its Makefile/Kbuild files when all are in m_kbuild_symbol ({}: not
# all indexed, final); content addressed per commit
_KBUILD_COVERAGE: "RepoScopedCache" = RepoScopedCache(maxsize=16)
# Makefiles one kbuild_usage() answer reads at most (both paths)
KBUILD_USAGE_MAKEFILES = 200


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else ("" if value is None else str(value))
_BUILD_RESOLVERS: "RepoScopedCache" = RepoScopedCache()
# resolve_arch() answers per (commit SHA, ARCH): bounded, any revision a caller names adds one
_ARCH_INFO_CACHE: "RepoScopedCache" = RepoScopedCache(maxsize=256)
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
                with get_reader() as reader:
                    _, vname = get_version_info(reader, ver)
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
    def _git(args: list[str], timeout: float = 20.0) -> str | None:
        """stdout of git (exit status 1 is `git grep` finding nothing), or None when git failed or timed
        out: callers must not cache that as an empty answer."""
        try:
            proc = subprocess.run(["git", *args], cwd=str(get_repo_dir()), capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            logger.debug("git %s failed: %s", args[:2], exc)
            return None
        return proc.stdout if proc.returncode in (0, 1) else None

    def _tree_listing(self, rev: str, path: str, recursive: bool = False) -> list[str]:
        key = ("ls", rev, path, recursive)
        # Read once into a local: both caches evict, so contains-then-get could race a miss
        listing = _LISTING_CACHE.get(key)
        if listing is None:
            args = ["ls-tree", "--name-only"] + (["-r"] if recursive else [])
            out = self._git(args + ([f"{rev}:{path}"] if path else [rev]))
            if out is None:
                return []
            listing = [line.strip() for line in out.splitlines() if line.strip()]
            _LISTING_CACHE[key] = listing
        return listing

    def _all_paths(self, rev: str) -> set[str]:
        """Every path in the revision's tree. Kept in its own small cache: one entry is the whole
        file list (36,783 paths / ~4.5 MiB at v3.0, more in later releases), and any revision a
        caller names gets one, so only a few are retained."""
        paths = _PATHS_CACHE.get(rev)
        if paths is None:
            out = self._git(["ls-tree", "-r", "--name-only", rev], timeout=60)
            if out is None:
                return set()
            paths = set(out.splitlines())
            _PATHS_CACHE[rev] = paths
        return paths

    # ------------------------------------------------------------------ architectures
    def resolve_arch(self, version_name: str, arch: str | None) -> dict[str, Any]:
        """ARCH/SRCARCH/KERNELVERSION for an ARCH value, following the top Makefile.

        Every symbol, xref, include and history request ranks definitions for an ARCH through this, so
        the answer is cached per (commit, ARCH) instead of re-reading and parsing the Makefile.
        """
        rev = self._rev(version_name)
        arch_id = (arch or DEFAULT_ARCH).strip().lower()
        arch_id = _ARCH_SYNONYMS.get(arch_id, arch_id)
        sha = git_tree_resolver.resolve_commit_sha(rev)
        cached = _ARCH_INFO_CACHE.get((sha, arch_id)) if sha else None
        if cached is not None:
            return {**cached, "rev": rev}
        makefile = self._read(rev, "Makefile") or ""
        aliases = arch_aliases(makefile)
        arch_dirs = set(self._tree_listing(rev, "arch"))
        srcarch = aliases.get(arch_id, arch_id)
        if srcarch not in arch_dirs:
            raise HTTPException(status_code=404, detail=f"Architecture '{arch_id}' does not exist in {rev}.")
        info = {
            "rev": rev,
            "arch": arch_id,
            "srcarch": srcarch,
            "kernelversion": kernel_version_string(makefile),
        }
        if sha:
            _ARCH_INFO_CACHE[(sha, arch_id)] = info
        return info

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
        """Parse and finalize one revision's Kconfig model (or load it from the disk cache).

        Runs without the model cache lock held.
        """
        commit = (self._git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], timeout=10) or "").strip()
        key = None
        if re.fullmatch(r"[0-9a-f]{40,64}", commit):
            spec = json.dumps([get_active_repo_name(), commit, info["arch"], profile.to_dict(), _code_fingerprint()], sort_keys=True)
            key = hashlib.sha1(spec.encode()).hexdigest()
            cached = _disk_load(key)
            if cached is not None:
                cached["model"]["version"] = version_name
                return _ModelEntry(cached["model"], info, cached)
        model, extras = self._parse_model(rev, info, profile)
        if key is not None:
            _disk_store(key, {"model": model, **extras})
        model["version"] = version_name
        return _ModelEntry(model, info, extras)

    def _parse_model(self, rev: str, info: dict[str, Any], profile: ToolchainProfile) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build one revision's model from Git: (model, {help, menu_help, references})."""
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
        model["arch"] = info["arch"]
        model["srcarch"] = info["srcarch"]
        model["toolchain"] = self._toolchain_report(model, profile, env_vars, macros, pp, shell)
        return model, {"help": builder.help_texts(), "menu_help": builder.menu_help(), "references": builder.references}

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
            "env_used": [{"name": k, "value": v} for k, v in sorted(used.items())],
            "probes": sorted(probes, key=lambda p: (p["kind"], p["key"])),
            "messages": list(pp.messages) if pp is not None else [],
        }

    def get_env_presets(self, version_name: str) -> dict[str, Any]:
        """Every ARCH value kconfig accepts at this revision, with its environment."""
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
                label = arch_id if arch_id == srcarch else f"{arch_id} ({srcarch})"
                if arch_id == srcarch and len(variants) > 1:
                    label = f"{arch_id} (generic: bitness chosen in menuconfig)"
                presets.append({
                    "id": arch_id,
                    "label": label,
                    "arch": arch_id,
                    "srcarch": srcarch,
                    "bits": 64 if "64" in arch_id else None,
                    "env": {"ARCH": arch_id, "SRCARCH": srcarch, "KERNELVERSION": kernelversion},
                })
        presets.sort(key=lambda p: (0 if p["id"] == DEFAULT_ARCH else 1, p["srcarch"]))
        return {
            "version": version_name,
            "default_arch": DEFAULT_ARCH if any(p["id"] == DEFAULT_ARCH for p in presets) else (presets[0]["id"] if presets else None),
            "targets": presets,
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
        canonical_seen = False
        for item in items:
            item["fid"] = 0
            item["is_canonical"] = item["name"] == default and not canonical_seen
            canonical_seen = canonical_seen or item["is_canonical"]
        return {
            "version": version_name,
            "arch": info["arch"],
            "defconfigs": items,
        }

    def list_fragments(self, version_name: str, arch: str = DEFAULT_ARCH) -> dict[str, Any]:
        """Configuration fragments `merge_config.sh` merges (K2): `kernel/configs/*.config` and
        `arch/$SRCARCH/configs/*.config` (none before v3.17)."""
        info = self.resolve_arch(version_name, arch)
        rev, srcarch = info["rev"], info["srcarch"]
        items = []
        for base in ("kernel/configs", f"arch/{srcarch}/configs"):
            for name in sorted(self._tree_listing(rev, base)):
                if name.endswith(".config"):
                    items.append({"name": name, "path": f"{base}/{name}", "scope": "common" if base.startswith("kernel/") else srcarch})
        return {"version": version_name, "arch": info["arch"], "fragments": items}

    def get_defconfig_content(self, version_name: str, name_or_path: str = "", arch: str = DEFAULT_ARCH, toolchain: Any = None) -> dict[str, Any]:
        """Raw defconfig text plus its literal assignments (nothing injected: kconfig derives the rest)."""
        info = self.resolve_arch(version_name, arch)
        rev, srcarch = info["rev"], info["srcarch"]
        clean = (name_or_path or "").strip().lstrip("/")
        if not clean:
            clean = self._default_defconfig(rev, info["arch"], srcarch) or ""
        if ".." in clean.split("/") or "\\" in clean:
            raise HTTPException(status_code=400, detail="Invalid defconfig path.")
        if clean.startswith("kernel/configs/"):
            # Configuration fragments (K2): `kernel/configs/*.config` only
            if not re.fullmatch(r"kernel/configs/[\w.+-]+\.config", clean):
                raise HTTPException(status_code=400, detail="Fragments are kernel/configs/<name>.config files.")
            candidates = [clean]
        elif clean.startswith("arch/"):
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
        values = self._config_assignments(content)
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
            "values": values,
            "content": content,
        }

    # ------------------------------------------------------------------ tree / model
    def get_tree(self, version_name: str, arch: str = DEFAULT_ARCH, toolchain: Any = None) -> dict[str, Any]:
        """The finalized configuration model (compact transport form) for the client-side engine."""
        entry = self.get_model(version_name, arch, toolchain)
        res = dict(entry.compact)
        res["version"] = version_name
        return res

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
                "help": entry.menu_help.get(mid, ""),
            })
        return out

    def _other_arch_definitions(self, rev: str, name: str) -> list[dict[str, Any]]:
        """Definitions of a symbol the ARCH's Kconfig tree does not reach (at most 50, by path then line).

        From the Kconfig entry tags when the revision is a finished version (every Kconfig blob of its tree
        is parsed), otherwise `git grep` over the `*Kconfig*` files."""
        try:
            stored = self._other_arch_definitions_db(rev, name)
        except Exception as exc:  # noqa: BLE001 - git grep answers
            logger.debug("Kconfig definitions of %s from tags failed: %s", name, exc)
            stored = None
        if stored is not None:
            return stored
        out = self._git(["grep", "-n", "-E", rf"^\s*(menu)?config\s+{re.escape(name)}\s*$", rev, "--", "*Kconfig*"]) or ""
        defs = []
        for line in out.splitlines()[:50]:
            parts = line.split(":", 3)
            if len(parts) == 4 and parts[2].isdigit():
                defs.append({"file_path": parts[1], "line_s": int(parts[2]), "line_e": int(parts[2])})
        return defs

    @staticmethod
    def _other_arch_definitions_db(rev: str, name: str) -> list[dict[str, Any]] | None:
        """The tag form of _other_arch_definitions(), or None when the revision is not a finished version."""
        from webapp.backend.services.history_service import KCONFIG_TYPES
        sha = git_tree_resolver.resolve_commit_sha(rev)
        if sha is None or not is_valid_identifier(name):
            return None
        with get_reader() as reader:
            if not git_tree_resolver._is_finished_version(reader, sha):
                return None
            blob_paths = git_tree_resolver.blob_paths(reader, sha)
            if blob_paths is None:
                return None
            # The revision's Kconfig blobs only (the name alone would also walk every older definition)
            kconfig_blobs = sorted(b for b, paths in blob_paths.items() if any("Kconfig" in p for p in paths))
            rows = []
            for i in range(0, len(kconfig_blobs), 2000):
                chunk = kconfig_blobs[i : i + 2000]
                rows.extend(
                    reader.select_rows(
                        ("m_bridge_tag", "bt"),
                        {"blob_id": "bt.blob_id", "line_s": "bt.line_s", "line_e": "bt.line_e"},
                        {"bt.blob_id": chunk, "a.name": name, "a.type_id": list(KCONFIG_TYPES)},
                        joins=[
                            Join(("m_tag", "t"), {"t.tag_id": "bt.tag_id"}),
                            Join(("m_ast", "a"), {"a.ast_id": "t.ast_id"}),
                        ],
                        as_dict=True,
                    )
                )
        found = {
            (path, int(r["line_s"]), int(r["line_e"]))
            for r in rows
            for path in blob_paths.get(r["blob_id"], ())
            if "Kconfig" in path
        }
        return [{"file_path": p, "line_s": ls, "line_e": le} for p, ls, le in sorted(found)[:50]]

    def get_symbol_detail(self, version_name: str, name: str, arch: str = DEFAULT_ARCH, toolchain: Any = None) -> dict[str, Any]:
        """Arch-scoped definition(s), dependencies, help and compiled files of a symbol."""
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
                "declarations": others, "definitions": [], "depends_on": [], "selects": [],
                "implies": [], "defaults": [], "compiled_files": [], "kbuild": [],
                "kconfig_refs": [], "def_val": "",
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
            "depends_on": entry.own_depends.get(clean, []),
            "selects": [t for t, _ in sym["selects"]],
            "implies": [t for t, _ in sym["implies"]],
            "defaults": defaults,
            "def_val": defaults[0]["value"] if defaults else "",
            "ranges": [{"min": entry.expr(lo), "max": entry.expr(hi), "cond": entry.expr(c) if c >= 0 else None} for lo, hi, c in sym["ranges"]],
            "choice": sym.get("choice"),
            "env": sym.get("env"),
            "kconfig_refs": entry.references.get(clean, [])[:_USAGE_LIMIT],
            "kbuild": kbuild,
            "compiled_files": self._compiled_files(kbuild),
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
            try:
                rules = self._kbuild_usage_db(rev, name)
            except Exception as exc:  # noqa: BLE001 - the Git path answers
                logger.debug("Kbuild usage of %s from m_kbuild_symbol failed: %s", name, exc)
                rules = None
            if rules is None:
                rules = self._kbuild_usage_git(rev, name)
                if rules is None:
                    return []
            _USAGE_CACHE[key] = rules
        trees = self._arch_trees(rev, info["arch"])
        return [dict(r, other_arch=is_other_arch_path(r["path"], srcarch, trees)) for r in rules]

    def _kbuild_usage_git(self, rev: str, name: str) -> list[dict[str, Any]] | None:
        """kbuild_usage() rules from Git: the Makefiles naming CONFIG_<name>, parsed (at most 200)."""
        # -F: the symbol name is a literal, not a pattern (without it git grep reads it as a
        # basic regex, where `.` and `*` would still match beyond the name)
        out = self._git(["grep", "-l", "-w", "-F", "-e", f"CONFIG_{name}", rev, "--", "*Makefile*", "*Kbuild*"])
        if out is None:
            return None
        makefiles = sorted({line.split(":", 1)[1] for line in out.splitlines() if ":" in line})
        paths = self._all_paths(rev)
        parser = KbuildParser()
        rules: list[dict[str, Any]] = []
        for mk in makefiles[:KBUILD_USAGE_MAKEFILES]:
            content = self._read(rev, mk)
            if content:
                rules.extend(kbuild_symbols.bindings_usage(mk, parser.parse_makefile_content(content, dir_path=os.path.dirname(mk)), name, paths))
        return rules

    def _kbuild_usage_db(self, rev: str, name: str) -> list[dict[str, Any]] | None:
        """kbuild_usage() rules from m_kbuild_symbol (parser/kbuild_symbols.py), or None when the revision's
        Makefile/Kbuild (path, blob) pairs are not all stored (an older database, a backfill not run, a
        revision whose Makefiles were never parsed)."""
        sha = git_tree_resolver.resolve_commit_sha(rev)
        if sha is None:
            return None
        with get_reader() as reader:
            pairs = self._kbuild_pairs_covered(reader, sha)
            if pairs is None:
                return None
            rows = [
                r
                for r in reader.select_rows(
                    "m_kbuild_symbol",
                    ["source_blob_id", "source_fnid", "idx", "via", "line_no", "compile_mode", "kind",
                     "target_obj", "target_path", "rule"],
                    {"symbol": name, "via": [1, 2]},
                    as_dict=True,
                )
                if (int(r["source_blob_id"]), int(r["source_fnid"])) in pairs
            ]
        paths = self._all_paths(rev)
        # One entry per binding (a binding naming the symbol in its rule and a condition is a rule)
        best: dict[tuple[str, int], dict[str, Any]] = {}
        for r in rows:
            mk = pairs[(int(r["source_blob_id"]), int(r["source_fnid"]))]
            k = (mk, int(r["idx"]))
            if k not in best or int(r["via"]) < int(best[k]["via"]):
                best[k] = r
        makefiles = sorted({mk for mk, _idx in best})[:KBUILD_USAGE_MAKEFILES]
        keep = set(makefiles)
        return [
            kbuild_symbols.usage_rule(mk, int(r["line_no"]), _text(r["rule"]), _text(r["target_obj"]), _text(r["target_path"]),
                                      int(r["kind"]), int(r["via"]), int(r["compile_mode"]), paths)
            for (mk, _idx), r in sorted(best.items()) if mk in keep
        ]

    @staticmethod
    def _kbuild_pairs_covered(reader: Any, sha: str) -> dict[tuple[int, int], str] | None:
        """(blob_id, fnid) -> path of the revision's Makefile/Kbuild files when every one of them has
        m_kbuild_symbol rows (marker rows included), else None. Cached per commit."""
        hit = _KBUILD_COVERAGE.get(sha)
        if hit is not None:
            return hit or None
        tree = git_tree_resolver.list_tree(sha)
        if tree is None:
            return None
        files = [p for p in tree if kbuild_symbols.is_kbuild_file(p)]
        meta = git_tree_resolver.map_blob_hashes(reader, [tree[p] for p in files])
        if any(tree[p] not in meta for p in files):
            _KBUILD_COVERAGE[sha] = {}
            return None
        fnid_of: dict[str, int] = {}
        for i in range(0, len(files), 1000):
            chunk = files[i:i + 1000]
            fnid_of.update(
                {_text(r["fname"]): int(r["fnid"]) for r in reader.select_rows("m_file_name", ["fnid", "fname"], {"fname": chunk}, as_dict=True)}
            )
        pairs: dict[tuple[int, int], str] = {}
        for p in files:
            if p not in fnid_of:
                _KBUILD_COVERAGE[sha] = {}
                return None
            pairs[(int(meta[tree[p]][0]), fnid_of[p])] = p
        blobs = sorted({b for b, _f in pairs})
        stored: set[tuple[int, int]] = set()
        for i in range(0, len(blobs), 1000):
            chunk = blobs[i:i + 1000]
            stored.update(
                (int(r["source_blob_id"]), int(r["source_fnid"]))
                for r in reader.select_rows(
                    "m_kbuild_symbol", ["source_blob_id", "source_fnid"], {"source_blob_id": chunk}, distinct=True, as_dict=True
                )
            )
        if any(pair not in stored for pair in pairs):
            # Not cached as final: a running backfill or parse fills the rest
            return None
        _KBUILD_COVERAGE[sha] = pairs
        return pairs

    @staticmethod
    def _compiled_files(kbuild: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Existing source files the symbol's Kbuild rules build at this revision (from the Git scan)."""
        files: dict[str, dict[str, Any]] = {}
        for rule in kbuild:
            if rule["kind"] == "directory" or not rule["exists"]:
                continue
            files.setdefault(rule["path"], {
                "file_path": rule["path"], "target_obj": rule["target_obj"], "compile_mode": rule["compile_mode"],
                "makefile": rule["makefile"], "line": rule["line"], "other_arch": rule["other_arch"],
            })
        return sorted(files.values(), key=lambda f: f["file_path"])

    # ------------------------------------------------------------------ build conditions (git)
    def _included_sources(self, rev: str) -> set[str]:
        """Basenames of .c/.S files that another file #includes (compiled inside it, not on their own)."""
        key = ("incsrc", rev)
        # Read into a local: the cache evicts, so re-reading after the write could miss
        sources = _USAGE_CACHE.get(key)
        if sources is not None:
            return sources
        sha = git_tree_resolver.resolve_commit_sha(rev)
        hit = _INCLUDED_DISK.get(sha) if sha else None
        if hit is not None:
            sources = set(hit)
        else:
            sources = self._included_sources_db(sha) if sha else None
            if sources is None:
                out = self._git(["grep", "-h", "-o", "-E", _INCLUDED_SOURCE_RE, rev, "--", "*.c", "*.h", "*.S"], timeout=60)
                if out is None:
                    return set()
                sources = {re.split(r'[/"<]', line.strip().rstrip('">'))[-1] for line in out.splitlines() if line.strip()}
            if sha:
                _INCLUDED_DISK.put(sha, sorted(sources))
        _USAGE_CACHE[key] = sources
        return sources

    @staticmethod
    def _included_sources_db(sha: str) -> set[str] | None:
        """The same set from the parser's stored `#include` directives (m_blob_facts family 3) when every
        `.c` / `.h` / `.S` blob of the commit has them; None otherwise. Directives start a line, so an
        `#include "x.c"` inside a comment, which `git grep` also matched, is not counted."""
        from parser import include_facts
        from webapp.backend.services.source_index import db_facts
        tree = git_tree_resolver.list_tree(sha) or {}
        blobs = sorted({b for p, b in tree.items() if include_facts.applies(p)})
        if not blobs:
            return None
        stored = db_facts(blobs, include_facts.FAMILY, include_facts.FORMAT, include_facts.decode)
        if len(stored) != len(blobs):
            return None
        out: set[str] = set()
        for directives in stored.values():
            out |= include_facts.included_sources(directives)
        return out

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
            if out is None:
                raise HTTPException(status_code=503, detail="git grep failed or timed out; try again.")
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
            "kconfig_refs": entry.references.get(clean, [])[:limit],
        }

    def get_graph(self, version_name: str, symbol: str, arch: str = DEFAULT_ARCH, toolchain: Any = None) -> dict[str, Any]:
        """Direct relations of a symbol: its own dependencies (left) and what it selects/implies (right)."""
        target = (symbol or "").strip()
        if target.startswith("CONFIG_"):
            target = target[7:]
        entry = self.get_model(version_name, arch, toolchain)
        edges: list[dict[str, Any]] = []
        sym = entry.model["symbols"].get(target)
        if sym is not None:
            for dep in entry.own_depends.get(target, []):
                edges.append({"from": target, "to": dep, "type": "depends_on"})
            for kind, items in (("selects", sym["selects"]), ("implies", sym["implies"])):
                for t, _ in items:
                    edges.append({"from": target, "to": t, "type": kind})
            for n, _ in entry.selected_by.get(target, []):
                edges.append({"from": n, "to": target, "type": "selects"})
        return {"version": version_name, "arch": entry.arch["arch"], "symbol": target, "edges": edges}

    def search_symbols(self, version_name: str, q: str = "", limit: int = 50, arch: str | None = None, toolchain: Any = None) -> dict[str, Any]:
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
        page = hits[:limit]
        return {"version": version_name, "arch": entry.arch["arch"], "query": q, "count": len(page), "total": len(hits), "symbols": page}

    # ------------------------------------------------------------------ configurations
    @staticmethod
    def _config_assignments(content: str) -> dict[str, str]:
        """The literal assignments of .config / defconfig text ({NAME: value}, strings unescaped,
        `# CONFIG_X is not set` as "n")."""
        symbols: dict[str, str] = {}
        for line in (content or "").splitlines():
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
        return symbols

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

    def autosolve(self, version_name: str, data: dict[str, Any]) -> dict[str, Any]:
        """Legal user changes that make a symbol reach the requested value from the current config.

        `data`: {target_symbol, current_values, arch, value ("y" or "m"), toolchain}.
        """
        sym_name = (data.get("target_symbol") or data.get("symbol") or "").strip()
        if sym_name.startswith("CONFIG_"):
            sym_name = sym_name[7:]
        entry, ev, _ = self._load(version_name, data.get("arch"), {"symbols": data.get("current_values") or {}, "toolchain": data.get("toolchain")})
        want = 1 if str(data.get("value") or "y").lower() == "m" else 2
        result = solve(ev, sym_name, want)
        result.update({"version": version_name, "arch": entry.arch["arch"], "target": sym_name, "target_symbol": sym_name})
        return result
