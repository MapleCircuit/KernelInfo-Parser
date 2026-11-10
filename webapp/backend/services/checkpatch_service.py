"""webapp/backend/services/checkpatch_service.py - Run the kernel's checkpatch.pl on a patch (P4, webapp/API.md sections 2.18, 4.7).

This runs a script taken from the repository, so every run is fenced:
  - off unless `webapp.tools.checkpatch.enabled` is true (no process is spawned otherwise);
  - the script comes only from `webapp.tools.checkpatch.ref`: "latest-release" (the newest final mainline
    tag) or an explicit release tag — never a branch, a SHA or a revision chosen by the caller;
  - checkpatch.pl and its data files are copied from Git into a fresh temporary directory, which is the
    working directory and $HOME (so no user `.checkpatch.conf` is read) and is removed afterwards;
  - `--no-tree` (no repository access), a minimal environment, a 30 s timeout, CPU / memory / file limits,
    one run at a time, patches of at most 2 MB, and an optional sandbox wrapper (`webapp.tools.sandbox`,
    `bwrap` gets no network and a read-only system).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any

from fastapi import HTTPException

MAX_PATCH = 2 << 20
TIMEOUT_S = 30
DATA_FILES = ("scripts/spelling.txt", "scripts/const_structs.checkpatch")
_FINAL_TAG_RE = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?$")
_TERSE_RE = re.compile(r"^(?P<file>[^:\n]*):(?P<line>\d+): (?P<level>ERROR|WARNING|CHECK):(?:(?P<type>[A-Z0-9_]+):)? ?(?P<msg>.*)$")
_LOCK = threading.Lock()


def tools_config() -> dict[str, Any]:
    from core.config import get_webapp_config
    tools = get_webapp_config().get("tools") or {}
    cp = tools.get("checkpatch") or {}
    return {"enabled": bool(cp.get("enabled", False)), "ref": str(cp.get("ref") or "latest-release"), "sandbox": str(tools.get("sandbox") or "")}


def resolve_script_ref(ref: str, tags: list[str]) -> str:
    """The release tag the script is taken from. Only final mainline tags (vX.Y, vX.Y.Z) are accepted."""
    if ref == "latest-release":
        finals = [t for t in tags if _FINAL_TAG_RE.match(t) and not _FINAL_TAG_RE.match(t).group(3)]
        if not finals:
            raise HTTPException(status_code=503, detail="No release tag to take checkpatch.pl from")
        return max(finals, key=lambda t: tuple(int(x) for x in _FINAL_TAG_RE.match(t).groups()[:2]))
    if not _FINAL_TAG_RE.match(ref) or ref not in tags:
        raise HTTPException(status_code=500, detail=f"webapp.tools.checkpatch.ref must be a release tag or latest-release, not {ref[:40]!r}")
    return ref


def parse_terse(output: str) -> tuple[list[dict[str, Any]], list[str]]:
    """`<file>:<line>: <LEVEL>:<TYPE>: <message>` lines; anything else is returned as raw lines."""
    results, raw = [], []
    for line in output.splitlines():
        m = _TERSE_RE.match(line.strip())
        if m:
            results.append({"level": m.group("level"), "type": m.group("type") or "", "message": m.group("msg").strip(),
                            "file": m.group("file"), "line": int(m.group("line"))})
        elif line.strip():
            raw.append(line)
    return results, raw


def _limits() -> None:  # runs in the child before exec
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S, TIMEOUT_S))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.setsid()


def sandbox_prefix(sandbox: str, workdir: str) -> list[str]:
    if not sandbox:
        return []
    name = os.path.basename(sandbox.split()[0])
    if name == "bwrap":
        args = [sandbox.split()[0], "--unshare-all", "--die-with-parent", "--new-session"]
        for ro in ("/usr", "/lib", "/lib64", "/bin", "/etc"):
            if os.path.exists(ro):
                args += ["--ro-bind", ro, ro]
        return args + ["--proc", "/proc", "--dev", "/dev", "--bind", workdir, workdir, "--chdir", workdir, "--"]
    return sandbox.split()


def run_checkpatch(script_files: dict[str, str], patch: str, *, sandbox: str = "", timeout: float = TIMEOUT_S, perl: str = "perl") -> dict[str, Any]:
    """Run checkpatch.pl (`script_files`: repository path -> text) on `patch` in a throwaway directory."""
    work = tempfile.mkdtemp(prefix="kip-checkpatch-")
    try:
        for rel, text in script_files.items():
            dest = os.path.join(work, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write(text)
        patch_path = os.path.join(work, "input.patch")
        with open(patch_path, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(patch)
        script = os.path.join(work, "scripts", "checkpatch.pl")
        flags = ["--no-tree", "--terse", "--no-summary"]
        text = script_files.get("scripts/checkpatch.pl", "")
        if "'show-types!'" in text:
            flags.append("--show-types")
        if "'color=s'" in text:
            flags.append("--color=never")
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": work}
        cmd = sandbox_prefix(sandbox, work) + [perl, script, *flags, patch_path]
        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=timeout, preexec_fn=_limits)
            out, err, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err, code = "checkpatch timed out", -1
        results, raw = parse_terse(out.replace(work + "/", ""))
        counts = {lvl: sum(1 for r in results if r["level"] == lvl) for lvl in ("ERROR", "WARNING", "CHECK")}
        return {"results": results, "counts": counts, "raw": "\n".join(raw + ([err.strip()] if err.strip() else [])).replace(work + "/", ""),
                "exit_code": code, "timed_out": timed_out, "elapsed_ms": int((time.monotonic() - started) * 1000)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_patch(patch: str) -> dict[str, Any]:
    """`POST /api/tools/{v}/checkpatch`: {enabled: false} when off (no process), else the parsed run."""
    cfg = tools_config()
    if not cfg["enabled"]:
        return {"enabled": False, "config_key": "webapp.tools.checkpatch.enabled",
                "reason": "checkpatch.pl is a script from the repository; running it is off unless the operator enables it"}
    if not isinstance(patch, str) or not patch.strip():
        raise HTTPException(status_code=400, detail="patch is empty")
    if len(patch.encode("utf-8", errors="replace")) > MAX_PATCH:
        raise HTTPException(status_code=413, detail="At most 2 MB of patch text")
    if not shutil.which("perl"):
        raise HTTPException(status_code=503, detail="perl is not installed on the server")
    from webapp.backend.config import get_repo_dir
    from webapp.backend.services.git_reader import git_reader
    tags = subprocess.run(["git", "-C", str(get_repo_dir()), "tag", "-l", "v*"], capture_output=True, text=True, timeout=30).stdout.split()
    ref = resolve_script_ref(cfg["ref"], tags)
    files = {}
    for rel in ("scripts/checkpatch.pl", *DATA_FILES):
        data, is_dir, _ = git_reader.read_file(f"refs/tags/{ref}", rel)
        if data is not None and not is_dir:
            files[rel] = data.decode("utf-8", errors="surrogateescape")
    if "scripts/checkpatch.pl" not in files:
        raise HTTPException(status_code=503, detail=f"{ref} has no scripts/checkpatch.pl")
    if not _LOCK.acquire(timeout=5):
        raise HTTPException(status_code=503, detail="checkpatch busy, retry")
    try:
        res = run_checkpatch(files, patch, sandbox=cfg["sandbox"])
    finally:
        _LOCK.release()
    return {"enabled": True, "script_ref": ref, **res}
