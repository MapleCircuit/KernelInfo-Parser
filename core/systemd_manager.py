"""core/systemd_manager.py - Systemd unit generation and tmux service lifecycle management.

Generates, validates, parses, installs, controls, and queries systemd `.service` units
for the KernelInfo-Parser daemon (`main.py --role daemon`) and web application
(`webapp/main.py`) in either direct (`Type=simple`) or `tmux` (`Type=forking`) mode.
"""
from __future__ import annotations

import grp
import inspect
import json
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

DAEMON_SERVICE_NAME = "kernelinfo-daemon"
DAEMON_UNIT_NAME = "kernelinfo-daemon.service"
WEBAPP_SERVICE_NAME = "kernelinfo-webapp"
WEBAPP_UNIT_NAME = "kernelinfo-webapp.service"
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")

DAEMON_TMUX_SOCKET = "kernelinfo-daemon"
DAEMON_TMUX_SESSION = "kernelinfo-daemon"
WEBAPP_TMUX_SOCKET = "kernelinfo-webapp"
WEBAPP_TMUX_SESSION = "kernelinfo-webapp"

VALID_SERVICES: tuple[Literal["daemon", "webapp"], ...] = ("daemon", "webapp")
VALID_ACTIONS: tuple[str, ...] = ("start", "stop", "restart", "enable", "disable")

RunnerFn = Callable[..., Any]


@dataclass(frozen=True)
class _ServiceSpec:
    service: Literal["daemon", "webapp"]
    service_name: str
    unit_name: str
    description: str
    tmux_socket: str
    tmux_session: str


_SERVICE_SPECS: dict[str, _ServiceSpec] = {
    "daemon": _ServiceSpec(
        service="daemon",
        service_name=DAEMON_SERVICE_NAME,
        unit_name=DAEMON_UNIT_NAME,
        description="KernelInfo-Parser Daemon Service",
        tmux_socket=DAEMON_TMUX_SOCKET,
        tmux_session=DAEMON_TMUX_SESSION,
    ),
    "webapp": _ServiceSpec(
        service="webapp",
        service_name=WEBAPP_SERVICE_NAME,
        unit_name=WEBAPP_UNIT_NAME,
        description="KernelInfo-Parser Webapp Service",
        tmux_socket=WEBAPP_TMUX_SOCKET,
        tmux_session=WEBAPP_TMUX_SESSION,
    ),
}


@dataclass
class ServiceUnitConfig:
    """Parameters used to render and install a systemd unit for a service."""

    service: Literal["daemon", "webapp"]
    user: str
    group: str
    use_tmux: bool
    project_root: Path
    config_path: Path
    python_executable: str = field(default_factory=lambda: sys.executable)

    def __post_init__(self) -> None:
        _get_spec(self.service)
        self.project_root = Path(self.project_root)
        self.config_path = Path(self.config_path)


@dataclass
class ServiceStatus:
    """Observed installation and runtime state of a managed systemd unit."""

    service: str
    unit_name: str
    unit_path: Path
    installed: bool
    enabled: str
    active: str
    sub_state: str
    mode: str
    user: str
    group: str
    attach_command: str | None = None
    main_pid: int | None = None
    error: str | None = None


@dataclass
class CommandResult:
    """Normalized outcome of an external command invocation."""

    ok: bool
    returncode: int
    stdout: str
    stderr: str
    command: list[str]


def _get_spec(service: str) -> _ServiceSpec:
    spec = _SERVICE_SPECS.get(service)
    if spec is None:
        raise ValueError(f"Unknown service {service!r}; expected one of {VALID_SERVICES}")
    return spec


def default_user_group() -> tuple[str, str]:
    """Return the current OS username and primary group name."""
    username = pwd.getpwuid(os.getuid()).pw_name
    groupname = grp.getgrgid(os.getgid()).gr_name
    return username, groupname


def find_tmux_binary() -> str | None:
    """Locate the `tmux` executable on PATH, or return None if unavailable."""
    return shutil.which("tmux")


def get_attach_command(service: str, user: str, current_user: str | None = None) -> str:
    """Build the interactive `tmux attach` command for `service` and target `user`."""
    spec = _get_spec(service)
    if current_user is None:
        current_user = default_user_group()[0]
    base = f"tmux -L {shlex.quote(spec.tmux_socket)} attach -t {shlex.quote(spec.tmux_session)}"
    if user and user != current_user:
        return f"sudo -u {shlex.quote(user)} {base}"
    return base


def _can_posix_access(
    st: os.stat_result,
    uid: int,
    gids: set[int],
    *,
    need_read: bool,
    need_exec: bool,
) -> bool:
    """Evaluate POSIX DAC read/execute access for `(uid, gids)` against `st`."""
    if uid == 0:
        return True
    mode = stat.S_IMODE(st.st_mode)
    if uid == st.st_uid:
        triad = (mode >> 6) & 0o7
    elif st.st_gid in gids:
        triad = (mode >> 3) & 0o7
    else:
        triad = mode & 0o7
    if need_read and not (triad & 0o4):
        return False
    if need_exec and not (triad & 0o1):
        return False
    return True


def _check_path_access(
    target: Path,
    *,
    uid: int,
    gids: set[int],
    user: str,
    group: str,
    label: str,
    need_read: bool,
    need_exec: bool,
    warn_missing: bool,
) -> list[str]:
    """Return human-readable permission warnings for `target` and its parent chain."""
    warnings: list[str] = []
    if not target.exists():
        if warn_missing:
            warnings.append(f"{label} path does not exist: {target}")
        return warnings

    if uid == 0:
        return warnings

    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        return [f"Could not resolve {label} path '{target}': {exc}"]

    for parent in reversed(resolved.parents):
        try:
            pst = os.stat(parent)
        except OSError as exc:
            warnings.append(f"Could not inspect parent directory '{parent}' for {label}: {exc}")
            return warnings
        if not _can_posix_access(pst, uid, gids, need_read=False, need_exec=True):
            warnings.append(
                f"Parent directory '{parent}' is not traversable (mode {oct(stat.S_IMODE(pst.st_mode))}) "
                f"by user '{user}' / group '{group}'; {label} '{target}' may be inaccessible."
            )
            return warnings

    try:
        tst = os.stat(resolved)
    except OSError as exc:
        return [f"Could not inspect {label} '{target}': {exc}"]

    if not _can_posix_access(tst, uid, gids, need_read=need_read, need_exec=need_exec):
        warnings.append(
            f"{label} '{target}' (mode {oct(stat.S_IMODE(tst.st_mode))}) may not be readable/accessible "
            f"by user '{user}' / group '{group}'."
        )
    return warnings


def validate_user_group(
    user: str,
    group: str,
    project_root: Path,
    config_path: Path,
) -> tuple[bool, list[str], list[str]]:
    """Validate that `user` and `group` exist and can traverse/read `project_root` and `config_path`."""
    errors: list[str] = []
    warnings: list[str] = []

    clean_user = (user or "").strip()
    clean_group = (group or "").strip()

    pw: pwd.struct_passwd | None = None
    gr: grp.struct_group | None = None

    if not clean_user:
        errors.append("Service user must not be empty.")
    else:
        try:
            pw = pwd.getpwnam(clean_user)
        except KeyError:
            errors.append(f"System user '{clean_user}' does not exist.")

    if not clean_group:
        errors.append("Service group must not be empty.")
    else:
        try:
            gr = grp.getgrnam(clean_group)
        except KeyError:
            errors.append(f"System group '{clean_group}' does not exist.")

    if errors or pw is None or gr is None:
        return False, errors, warnings

    gids: set[int] = {pw.pw_gid, gr.gr_gid}
    try:
        gids.update(os.getgrouplist(pw.pw_name, pw.pw_gid))
    except OSError:
        pass

    root_path = Path(project_root)
    cfg_path = Path(config_path)

    warnings.extend(
        _check_path_access(
            root_path,
            uid=pw.pw_uid,
            gids=gids,
            user=clean_user,
            group=clean_group,
            label="project_root",
            need_read=True,
            need_exec=True,
            warn_missing=True,
        )
    )
    warnings.extend(
        _check_path_access(
            cfg_path,
            uid=pw.pw_uid,
            gids=gids,
            user=clean_user,
            group=clean_group,
            label="config_path",
            need_read=True,
            need_exec=cfg_path.is_dir(),
            warn_missing=False,
        )
    )
    return True, errors, warnings


def _needs_systemd_quotes(value: str) -> bool:
    return value == "" or any(ch in value for ch in (" ", "\t", "\n", '"', "'", "\\"))


def _quote_systemd_double(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _quote_systemd_arg(value: str) -> str:
    return _quote_systemd_double(value) if _needs_systemd_quotes(value) else value


def _sh_double_quote_or_plain(value: str) -> str:
    if value == "" or any(ch in value for ch in (" ", "\t", "\n", '"', "'", "\\", "$", "`")):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        return f'"{escaped}"'
    return value


def _build_exec_args(cfg: ServiceUnitConfig) -> list[str]:
    python_exe = str(cfg.python_executable or sys.executable)
    config_str = str(cfg.config_path)
    if cfg.service == "daemon":
        return [python_exe, str(cfg.project_root / "main.py"), "--config", config_str, "--role", "daemon"]
    return [python_exe, str(cfg.project_root / "webapp" / "main.py"), "--config", config_str]


def render_unit_file(cfg: ServiceUnitConfig, *, tmux_bin: str | None = None) -> str:
    """Render the systemd `.service` unit file contents for `cfg`."""
    spec = _get_spec(cfg.service)
    exec_args = _build_exec_args(cfg)
    workdir_raw = str(cfg.project_root)
    workdir_arg = _quote_systemd_arg(workdir_raw)
    env_cfg_raw = f"KERNELINFO_CONFIG={cfg.config_path}"
    env_cfg_line = f"Environment={_quote_systemd_double(env_cfg_raw)}" if _needs_systemd_quotes(str(cfg.config_path)) else f"Environment={env_cfg_raw}"

    if not cfg.use_tmux:
        exec_start = " ".join(_quote_systemd_arg(part) for part in exec_args)
        lines = [
            "[Unit]",
            f"Description={spec.description}",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={cfg.user}",
            f"Group={cfg.group}",
            f"WorkingDirectory={workdir_raw}",
            "Environment=PYTHONUNBUFFERED=1",
            env_cfg_line,
            f"ExecStart={exec_start}",
            "Restart=on-failure",
            "RestartSec=10",
            "KillSignal=SIGTERM",
            "TimeoutStopSec=30",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
        return "\n".join(lines)

    resolved_tmux = tmux_bin or find_tmux_binary() or "/usr/bin/tmux"
    inner_cmd = " ".join(shlex.quote(part) for part in exec_args)
    tmux_arg = _quote_systemd_arg(resolved_tmux)
    socket_arg = _quote_systemd_arg(spec.tmux_socket)
    session_arg = _quote_systemd_arg(spec.tmux_session)
    exec_start = (
        f"{tmux_arg} -L {socket_arg} new-session -d -s {session_arg} "
        f"-c {workdir_arg} {_quote_systemd_double(inner_cmd)}"
    )
    exec_start_post = f"-{tmux_arg} -L {socket_arg} set-option -t {session_arg} remain-on-exit off"

    # Escape `$` as `$$` so systemd passes shell variables literally to `/bin/sh -c`.
    tmux_sh = _sh_double_quote_or_plain(resolved_tmux)
    socket_sh = _sh_double_quote_or_plain(spec.tmux_socket)
    session_sh = _sh_double_quote_or_plain(spec.tmux_session)
    stop_script = (
        f'pid=$$({tmux_sh} -L {socket_sh} list-panes -t {session_sh} -F "#{{pane_pid}}" 2>/dev/null | head -n 1); '
        'if [ -n "$$pid" ]; then '
        'kill -TERM "$$pid" 2>/dev/null || true; '
        'i=0; '
        'while [ "$$i" -lt 20 ] && kill -0 "$$pid" 2>/dev/null; do '
        'sleep 0.5; '
        'i=$$((i + 1)); '
        'done; '
        'fi; '
        f'{tmux_sh} -L {socket_sh} kill-session -t {session_sh} 2>/dev/null || true'
    )

    lines = [
        "[Unit]",
        f"Description={spec.description} (tmux)",
        "After=network-online.target",
        "Wants=network-online.target",
        "StartLimitIntervalSec=60",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=forking",
        f"User={cfg.user}",
        f"Group={cfg.group}",
        f"WorkingDirectory={workdir_raw}",
        "Environment=PYTHONUNBUFFERED=1",
        env_cfg_line,
        f"ExecStart={exec_start}",
        f"ExecStartPost={exec_start_post}",
        f"ExecStop=/bin/sh -c '{stop_script}'",
        "Restart=always",
        "RestartSec=10",
        "KillSignal=SIGTERM",
        "TimeoutStopSec=30",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def _unquote_systemd_value(raw: str) -> str:
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        try:
            parts = shlex.split(val)
            if len(parts) == 1:
                return parts[0]
        except ValueError:
            pass
        return val[1:-1]
    return val


def _extract_config_flag(tokens: Sequence[str]) -> str | None:
    for idx, tok in enumerate(tokens):
        if tok in ("--config", "-c") and idx + 1 < len(tokens):
            return tokens[idx + 1]
        if tok.startswith("--config="):
            return tok.split("=", 1)[1]
    return None


def parse_unit_file(content: str, service: str) -> dict[str, Any]:
    """Parse `User=`, `Group=`, `mode` (`tmux` or `direct`), and `config_path` from unit text."""
    spec = _get_spec(service)
    section = ""
    user = ""
    group = ""
    working_directory = ""
    exec_start = ""
    env_vars: dict[str, str] = {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "service" or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "User":
            user = _unquote_systemd_value(val)
        elif key == "Group":
            group = _unquote_systemd_value(val)
        elif key == "WorkingDirectory":
            working_directory = _unquote_systemd_value(val)
        elif key == "ExecStart":
            exec_start = val
        elif key == "Environment":
            try:
                env_tokens = shlex.split(val)
            except ValueError:
                env_tokens = [val]
            for token in env_tokens:
                if "=" in token:
                    ek, ev = token.split("=", 1)
                    env_vars[ek] = ev

    mode = "direct"
    config_path: str | None = None

    if exec_start:
        try:
            tokens = shlex.split(exec_start)
        except ValueError:
            tokens = exec_start.split()

        is_tmux = bool(
            tokens
            and Path(tokens[0]).name == "tmux"
            and ("new-session" in tokens or "-L" in tokens)
        )
        if is_tmux:
            mode = "tmux"
            if len(tokens) > 1:
                inner_cmd = tokens[-1]
                try:
                    inner_tokens = shlex.split(inner_cmd)
                except ValueError:
                    inner_tokens = inner_cmd.split()
                config_path = _extract_config_flag(inner_tokens)
        else:
            config_path = _extract_config_flag(tokens)

    if not config_path:
        config_path = env_vars.get("KERNELINFO_CONFIG") or env_vars.get("CONFIG_FILE")

    return {
        "service": spec.service,
        "user": user,
        "group": group,
        "mode": mode,
        "config_path": config_path,
        "working_directory": working_directory,
        "exec_start": exec_start,
    }


def _normalize_command_result(raw: Any, command: list[str]) -> CommandResult:
    if isinstance(raw, CommandResult):
        return raw
    rc = int(getattr(raw, "returncode", 1))
    stdout = getattr(raw, "stdout", "") or ""
    stderr = getattr(raw, "stderr", "") or ""
    return CommandResult(ok=(rc == 0), returncode=rc, stdout=stdout, stderr=stderr, command=list(command))


def _invoke_runner(runner: RunnerFn | None, command: list[str], *, input_text: str | None = None) -> CommandResult:
    cmd = list(command)
    try:
        if runner is None or runner is subprocess.run:
            proc = subprocess.run(cmd, input=input_text, capture_output=True, text=True, check=False)
            return _normalize_command_result(proc, cmd)

        sig = inspect.signature(runner)
        params = sig.parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        positional_params = [
            p for p in params.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values())

        if input_text is not None:
            if "input_text" in params or accepts_kwargs:
                res = runner(cmd, input_text=input_text)
            elif "input" in params:
                res = runner(cmd, input=input_text)
            elif len(positional_params) >= 2 or has_varargs:
                res = runner(cmd, input_text)
            else:
                res = runner(cmd)
        else:
            if "input_text" in params:
                res = runner(cmd, input_text=None)
            else:
                res = runner(cmd)
        return _normalize_command_result(res, cmd)
    except OSError as exc:
        return CommandResult(ok=False, returncode=127, stdout="", stderr=str(exc), command=cmd)


def _with_sudo(command: list[str], *, use_runner: bool) -> list[str]:
    if command and command[0] == "sudo":
        return list(command)
    if not use_runner and hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("sudo") is None:
        return list(command)
    return ["sudo", *command]


def _normalize_validation_result(raw: Any) -> tuple[bool, list[str], list[str]]:
    if isinstance(raw, tuple):
        if len(raw) == 3:
            ok = bool(raw[0])
            errs = list(raw[1]) if raw[1] else []
            third = raw[2]
            warns = list(third) if isinstance(third, (list, tuple)) else []
            return ok, errs, warns
        if len(raw) == 2:
            ok = bool(raw[0])
            second = raw[1]
            if isinstance(second, str):
                return ok, ([] if ok else [second]), []
            return ok, (list(second) if second else []), []
    if isinstance(raw, list):
        return len(raw) == 0, list(raw), []
    if isinstance(raw, bool):
        return raw, ([] if raw else ["Configuration validation failed."]), []
    return True, [], []


def _validate_config_fallback(config_path: Path, service: str) -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return False, [f"Invalid JSON in configuration file '{config_path}': {exc}"], warnings
    except OSError as exc:
        return False, [f"Failed to read configuration file '{config_path}': {exc}"], warnings

    if not isinstance(data, dict):
        return False, [f"Configuration root in '{config_path}' must be a JSON object."], warnings

    for section in ("database", "webapp", "parser"):
        if section in data and not isinstance(data[section], dict):
            errors.append(f"Configuration section '{section}' must be a JSON object.")

    if "database" not in data:
        warnings.append("Configuration has no 'database' section; built-in defaults will be used.")
    if service == "webapp" and "webapp" not in data:
        warnings.append("Configuration has no 'webapp' section; built-in defaults will be used.")

    return len(errors) == 0, errors, warnings


def preflight_service_install(
    cfg: ServiceUnitConfig,
    validate_config_fn: Callable[..., Any] | None = None,
) -> tuple[bool, list[str], list[str]]:
    """Run pre-flight checks (config validity, user/group permissions, tmux availability) before install."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        _get_spec(cfg.service)
    except ValueError as exc:
        return False, [str(exc)], warnings

    if not cfg.config_path.is_file():
        errors.append(f"Configuration file not found: {cfg.config_path}")
    else:
        validator = validate_config_fn
        if validator is None:
            try:
                from core import config as core_config
                validator = getattr(core_config, "validate_service_config", None)
            except Exception:
                validator = None

        if validator is not None:
            try:
                sig = inspect.signature(validator)
                param_names = list(sig.parameters.keys())
                if len(param_names) >= 2:
                    if param_names[0] in ("service", "svc", "role") or param_names[1] in (
                        "config_path",
                        "path",
                        "cfg_path",
                    ):
                        raw_res = validator(cfg.service, cfg.config_path)
                    else:
                        raw_res = validator(cfg.config_path, cfg.service)
                else:
                    raw_res = validator(cfg.config_path)
                c_ok, c_errs, c_warns = _normalize_validation_result(raw_res)
                if not c_ok and not c_errs:
                    c_errs = [f"Configuration validation failed for '{cfg.config_path}'."]
                errors.extend(c_errs)
                warnings.extend(c_warns)
            except Exception as exc:
                errors.append(f"Configuration validation error for '{cfg.config_path}': {exc}")
        else:
            c_ok, c_errs, c_warns = _validate_config_fallback(cfg.config_path, cfg.service)
            errors.extend(c_errs)
            warnings.extend(c_warns)

    ug_ok, ug_errs, ug_warns = validate_user_group(cfg.user, cfg.group, cfg.project_root, cfg.config_path)
    if not ug_ok:
        errors.extend(ug_errs)
    warnings.extend(ug_warns)

    entrypoint = cfg.project_root / ("main.py" if cfg.service == "daemon" else Path("webapp") / "main.py")
    if cfg.project_root.is_dir() and not entrypoint.is_file():
        warnings.append(f"Service entrypoint script not found at '{entrypoint}'.")

    if cfg.use_tmux and find_tmux_binary() is None:
        errors.append("tmux executable not found in PATH; install tmux or disable tmux mode.")

    return len(errors) == 0, errors, warnings


def _parse_show_properties(stdout: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            props[key.strip()] = val.strip()
    return props


def get_service_status(
    service: str,
    *,
    unit_dir: Path = SYSTEMD_UNIT_DIR,
    runner: RunnerFn | None = None,
) -> ServiceStatus:
    """Inspect unit installation and runtime state via `systemctl` and the unit file."""
    spec = _get_spec(service)
    unit_path = Path(unit_dir) / spec.unit_name
    installed = unit_path.is_file()

    mode = "direct"
    user = ""
    group = ""
    if installed:
        try:
            parsed = parse_unit_file(unit_path.read_text(encoding="utf-8"), spec.service)
            mode = parsed.get("mode") or "direct"
            user = parsed.get("user") or ""
            group = parsed.get("group") or ""
        except OSError:
            pass

    show_cmd = [
        "systemctl",
        "show",
        spec.unit_name,
        "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID,User,Group",
        "--no-pager",
    ]
    show_res = _invoke_runner(runner, show_cmd)

    enabled = "unknown"
    active = "unknown"
    sub_state = "unknown"
    main_pid: int | None = None
    error: str | None = None

    if show_res.ok:
        props = _parse_show_properties(show_res.stdout)
        unit_file_state = props.get("UnitFileState", "")
        active_state = props.get("ActiveState", "")
        sub_val = props.get("SubState", "")
        pid_raw = props.get("MainPID", "")

        if unit_file_state:
            enabled = unit_file_state
        if active_state:
            active = active_state
        if sub_val:
            sub_state = sub_val
        if pid_raw.isdigit() and int(pid_raw) > 0:
            main_pid = int(pid_raw)
        if not user and props.get("User"):
            user = props["User"]
        if not group and props.get("Group"):
            group = props["Group"]

        if enabled == "unknown" and installed:
            en_res = _invoke_runner(runner, ["systemctl", "is-enabled", spec.unit_name])
            en_text = en_res.stdout.strip()
            if en_text:
                enabled = en_text
        if active == "unknown" and installed:
            act_res = _invoke_runner(runner, ["systemctl", "is-active", spec.unit_name])
            act_text = act_res.stdout.strip()
            if act_text:
                active = act_text
    else:
        en_res = _invoke_runner(runner, ["systemctl", "is-enabled", spec.unit_name])
        act_res = _invoke_runner(runner, ["systemctl", "is-active", spec.unit_name])
        if en_res.stdout.strip():
            enabled = en_res.stdout.strip()
        if act_res.stdout.strip():
            active = act_res.stdout.strip()
        err_msg = show_res.stderr.strip() or en_res.stderr.strip() or act_res.stderr.strip()
        if err_msg:
            error = err_msg
        elif show_res.returncode != 0:
            error = f"systemctl show failed with exit code {show_res.returncode}"

    attach_command: str | None = None
    if mode == "tmux" and (installed or active == "active"):
        attach_user = user or default_user_group()[0]
        attach_command = get_attach_command(spec.service, attach_user)

    return ServiceStatus(
        service=spec.service,
        unit_name=spec.unit_name,
        unit_path=unit_path,
        installed=installed,
        enabled=enabled,
        active=active,
        sub_state=sub_state,
        mode=mode,
        user=user,
        group=group,
        attach_command=attach_command,
        main_pid=main_pid,
        error=error,
    )


def _format_cmd_failure(prefix: str, res: CommandResult) -> str:
    detail = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
    return f"{prefix}: {detail}"


def install_service(
    cfg: ServiceUnitConfig,
    *,
    unit_dir: Path = SYSTEMD_UNIT_DIR,
    enable: bool = False,
    start: bool = False,
    privileged_runner: RunnerFn | None = None,
    validate_config_fn: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    """Validate, render, and install the systemd unit for `cfg`, optionally enabling/starting it."""
    ok, errors, warnings = preflight_service_install(cfg, validate_config_fn=validate_config_fn)
    if not ok:
        return False, "; ".join(errors)

    spec = _get_spec(cfg.service)
    unit_path = Path(unit_dir) / spec.unit_name
    unit_content = render_unit_file(cfg)
    use_priv = privileged_runner is not None

    tee_res = _invoke_runner(
        privileged_runner,
        _with_sudo(["tee", str(unit_path)], use_runner=use_priv),
        input_text=unit_content,
    )
    if not tee_res.ok:
        return False, _format_cmd_failure(f"Failed to write unit file '{unit_path}'", tee_res)

    chmod_res = _invoke_runner(
        privileged_runner,
        _with_sudo(["chmod", "644", str(unit_path)], use_runner=use_priv),
    )
    if not chmod_res.ok:
        return False, _format_cmd_failure(f"Failed to set permissions on '{unit_path}'", chmod_res)

    reload_res = _invoke_runner(
        privileged_runner,
        _with_sudo(["systemctl", "daemon-reload"], use_runner=use_priv),
    )
    if not reload_res.ok:
        return False, _format_cmd_failure("Failed to reload systemd daemon", reload_res)

    actions_done = [f"Installed {spec.unit_name} at {unit_path}"]
    if enable:
        en_res = _invoke_runner(
            privileged_runner,
            _with_sudo(["systemctl", "enable", spec.unit_name], use_runner=use_priv),
        )
        if not en_res.ok:
            return False, _format_cmd_failure(f"Installed {spec.unit_name} but failed to enable", en_res)
        actions_done.append("enabled")

    if start:
        st_res = _invoke_runner(
            privileged_runner,
            _with_sudo(["systemctl", "start", spec.unit_name], use_runner=use_priv),
        )
        if not st_res.ok:
            return False, _format_cmd_failure(f"Installed {spec.unit_name} but failed to start", st_res)
        actions_done.append("started")

    message = ", ".join(actions_done) + "."
    if warnings:
        message += " Warnings: " + "; ".join(warnings)
    return True, message


def uninstall_service(
    service: str,
    *,
    unit_dir: Path = SYSTEMD_UNIT_DIR,
    privileged_runner: RunnerFn | None = None,
    runner: RunnerFn | None = None,
) -> tuple[bool, str]:
    """Stop (if active), disable (if enabled), remove the unit file, and reload systemd."""
    spec = _get_spec(service)
    unit_path = Path(unit_dir) / spec.unit_name
    status = get_service_status(spec.service, unit_dir=unit_dir, runner=runner)
    use_priv = privileged_runner is not None

    if status.active in ("active", "activating", "reloading"):
        stop_res = _invoke_runner(
            privileged_runner,
            _with_sudo(["systemctl", "stop", spec.unit_name], use_runner=use_priv),
        )
        if not stop_res.ok:
            return False, _format_cmd_failure(f"Failed to stop {spec.unit_name}", stop_res)

    if status.enabled in ("enabled", "enabled-runtime"):
        dis_res = _invoke_runner(
            privileged_runner,
            _with_sudo(["systemctl", "disable", spec.unit_name], use_runner=use_priv),
        )
        if not dis_res.ok:
            return False, _format_cmd_failure(f"Failed to disable {spec.unit_name}", dis_res)

    rm_res = _invoke_runner(
        privileged_runner,
        _with_sudo(["rm", "-f", str(unit_path)], use_runner=use_priv),
    )
    if not rm_res.ok:
        return False, _format_cmd_failure(f"Failed to remove unit file '{unit_path}'", rm_res)

    reload_res = _invoke_runner(
        privileged_runner,
        _with_sudo(["systemctl", "daemon-reload"], use_runner=use_priv),
    )
    if not reload_res.ok:
        return False, _format_cmd_failure("Removed unit file but failed to reload systemd daemon", reload_res)

    return True, f"Uninstalled {spec.unit_name} from {unit_path}."


def control_service(
    service: str,
    action: str,
    *,
    privileged_runner: RunnerFn | None = None,
) -> CommandResult:
    """Run a privileged `systemctl <action> <unit>` command for `service`."""
    spec = _get_spec(service)
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid service action {action!r}; expected one of {VALID_ACTIONS}")
    use_priv = privileged_runner is not None
    cmd = _with_sudo(["systemctl", action, spec.unit_name], use_runner=use_priv)
    return _invoke_runner(privileged_runner, cmd)


def read_service_logs(
    service: str,
    *,
    lines: int = 100,
    runner: RunnerFn | None = None,
) -> CommandResult:
    """Read recent journald logs for `service` via `journalctl`."""
    spec = _get_spec(service)
    count = int(lines)
    if count <= 0:
        raise ValueError(f"lines must be a positive integer, got {lines!r}")
    cmd = ["journalctl", "-u", spec.unit_name, "-n", str(count), "--no-pager"]
    return _invoke_runner(runner, cmd)
