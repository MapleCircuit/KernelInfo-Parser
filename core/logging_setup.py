"""core/logging_setup.py - File logging, stdio capture, and request filtering for parser & webapp."""

from __future__ import annotations

from datetime import datetime
import io
import logging
import os
from pathlib import Path
import sys
import threading
from typing import Any, TextIO

from core.config import (
    REPO_ROOT,
    _coerce_bool,
    get_logging_config,
    normalize_log_level,
    normalize_webapp_request_mode,
)
from core.globalstuff import FileContextFormatter, is_test_environment, strip_ansi

ACTIVE_TARGET_ENV = "KIP_LOG_ACTIVE_TARGET"
FILE_LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

_LOCK = threading.RLock()
_ACTIVE_STATE: dict[str, Any] = {
    "target": None,
    "root_prev_level": None,
    "bumped_stream_handlers": [],
    "stdout_tee": None,
    "stderr_tee": None,
    "orig_stdout": None,
    "orig_stderr": None,
    "orig_excepthook": None,
    "orig_threading_excepthook": None,
    "excepthook_installed": False,
}


def resolve_log_path(
    dir_val: Any,
    file_val: Any,
    default_filename: str,
    base_root: Path | None = None,
) -> Path | None:
    """Resolve a log directory and filename into an absolute file path (None when both are empty)."""
    root = base_root or REPO_ROOT
    raw_dir = str(dir_val or "").strip()
    raw_file = str(file_val or "").strip()
    if not raw_dir and not raw_file:
        return None

    dir_path: Path | None = None
    if raw_dir:
        dir_path = Path(os.path.expanduser(raw_dir))
        if not dir_path.is_absolute():
            dir_path = root / dir_path

    if not raw_file:
        assert dir_path is not None
        return (dir_path / default_filename).resolve()

    exp_file = os.path.expanduser(raw_file)
    is_explicit_dir = exp_file.endswith(("/", "\\"))
    file_path = Path(exp_file)
    if file_path.is_absolute():
        target = file_path
    elif dir_path is not None:
        target = dir_path / file_path
    else:
        target = root / file_path

    if is_explicit_dir or (target.exists() and target.is_dir()):
        target = target / default_filename
    return target.resolve()


def resolve_log_targets(
    section_cfg: dict[str, Any] | None,
    target: str,
    base_root: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Resolve `(main_path, error_path)` for `'parser'` or `'webapp'` (both None when disabled)."""
    cfg = section_cfg or {}
    if not _coerce_bool(cfg.get("enabled"), False):
        return None, None

    default_main = f"{target}.log"
    default_err = f"{target}_errors.log"
    main_path = resolve_log_path(cfg.get("dir"), cfg.get("file"), default_main, base_root=base_root)
    error_path = resolve_log_path(cfg.get("error_dir"), cfg.get("error_file"), default_err, base_root=base_root)

    if main_path is None and error_path is None:
        main_path = ((base_root or REPO_ROOT) / default_main).resolve()
    if error_path is not None and main_path is not None and error_path == main_path:
        error_path = None
    return main_path, error_path


def _extract_http_status(record: logging.LogRecord) -> int | None:
    """Return HTTP status code from a request log record when available."""
    explicit = getattr(record, "status_code", None)
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) >= 5:
        try:
            return int(record.args[4])
        except (TypeError, ValueError):
            return None
    return None


def _is_request_record(record: logging.LogRecord) -> bool:
    """True for HTTP access/request log records."""
    return record.name in ("uvicorn.access", "webapp.request") or hasattr(record, "status_code")


class ParserMainLogFilter(logging.Filter):
    """Filter parser log records by minimum configured level."""

    def __init__(self, level_no: int) -> None:
        super().__init__()
        self.level_no = level_no

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.level_no


class WebappMainLogFilter(logging.Filter):
    """Filter webapp records by `request_mode` (`'all'` vs `'errors'`) and minimum severity level."""

    def __init__(self, level_no: int, request_mode: str) -> None:
        super().__init__()
        self.level_no = level_no
        self.request_mode = normalize_webapp_request_mode(request_mode)

    def filter(self, record: logging.LogRecord) -> bool:
        if _is_request_record(record):
            status = _extract_http_status(record)
            if self.request_mode == "errors":
                return (status is not None and status >= 400) or record.levelno >= logging.ERROR
            return True
        if self.request_mode == "errors":
            return record.levelno >= max(self.level_no, logging.WARNING)
        return record.levelno >= self.level_no


class ErrorOnlyLogFilter(logging.Filter):
    """Allow only ERROR/CRITICAL logs and failed HTTP requests (status >= 400)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _is_request_record(record):
            status = _extract_http_status(record)
            return (status is not None and status >= 400) or record.levelno >= logging.ERROR
        return record.levelno >= logging.ERROR


class _ManagedFileHandler(logging.FileHandler):
    """FileHandler tagged with its KernelInfo logging target and role."""

    def __init__(self, filename: Path, target: str, role: str) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(str(filename), mode="a", encoding="utf-8", delay=False)
        self._kip_log_target = target
        self._kip_log_role = role
        self.setLevel(logging.NOTSET)
        self.setFormatter(FileContextFormatter(FILE_LOG_FORMAT, strip_ansi_codes=True))


class _TeeStream:
    """Text stream wrapper that forwards writes to the original stream and appends clean lines to log files."""

    def __init__(
        self,
        orig_stream: TextIO,
        *,
        is_stderr: bool,
        main_path: Path | None,
        error_path: Path | None,
        capture_stdout_to_main: bool,
    ) -> None:
        self.orig_stream = orig_stream
        self.is_stderr = is_stderr
        self.main_path = main_path if (is_stderr or capture_stdout_to_main) else None
        self.error_path = error_path if is_stderr else None
        self._lock = threading.Lock()
        self._buf = ""
        self._files: list[TextIO] = []
        self._open_files()

    def _open_files(self) -> None:
        seen: set[Path] = set()
        for path in (self.main_path, self.error_path):
            if path is None or path in seen:
                continue
            seen.add(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._files.append(open(path, "a", encoding="utf-8"))

    @property
    def encoding(self) -> str:
        return getattr(self.orig_stream, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str | None:
        return getattr(self.orig_stream, "errors", None)

    def isatty(self) -> bool:
        try:
            return bool(self.orig_stream.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self.orig_stream.fileno()

    def writable(self) -> bool:
        return True

    def _write_log_line(self, line: str) -> None:
        if not self._files:
            return
        clean = strip_ansi(line)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        lvl = "ERROR" if self.is_stderr else "INFO"
        tag = "stderr" if self.is_stderr else "stdout"
        formatted = f"{stamp} - {lvl} - {tag} - {clean}\n"
        for fh in self._files:
            try:
                fh.write(formatted)
                fh.flush()
            except OSError:
                pass

    def write(self, data: str) -> int:
        if not data:
            return 0
        res = self.orig_stream.write(data)
        if self._files:
            with self._lock:
                self._buf += data
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    if line.endswith("\r"):
                        line = line[:-1]
                    if line:
                        self._write_log_line(line)
        return res

    def flush(self) -> None:
        try:
            self.orig_stream.flush()
        except Exception:
            pass
        if self._files:
            with self._lock:
                for fh in self._files:
                    try:
                        fh.flush()
                    except OSError:
                        pass

    def close(self) -> None:
        with self._lock:
            if self._buf:
                leftover = self._buf.rstrip("\r")
                self._buf = ""
                if leftover:
                    self._write_log_line(leftover)
            for fh in self._files:
                try:
                    fh.close()
                except OSError:
                    pass
            self._files.clear()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.orig_stream, name)


def _unwrap_stream(stream: Any) -> Any:
    while isinstance(stream, _TeeStream):
        stream = stream.orig_stream
    return stream


def _all_loggers() -> list[logging.Logger]:
    loggers: list[logging.Logger] = [logging.getLogger()]
    for obj in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(obj, logging.Logger):
            loggers.append(obj)
    return loggers


def _detach_managed_handlers(target: str | None = None) -> None:
    for lg in _all_loggers():
        for handler in list(lg.handlers):
            h_target = getattr(handler, "_kip_log_target", None)
            if h_target is not None and (target is None or h_target == target):
                lg.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass


def _unwrap_terminal_handlers(orig_stdout: Any, orig_stderr: Any) -> None:
    """Ensure terminal StreamHandlers write directly to the unwrapped streams, avoiding double-logging."""
    for lg in _all_loggers():
        for handler in lg.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                stream = getattr(handler, "stream", None)
                if isinstance(stream, _TeeStream):
                    handler.stream = stream.orig_stream
                elif stream is sys.stdout and orig_stdout is not None:
                    handler.stream = orig_stdout
                elif stream is sys.stderr and orig_stderr is not None:
                    handler.stream = orig_stderr


def _install_excepthooks() -> None:
    if _ACTIVE_STATE["excepthook_installed"]:
        return
    _ACTIVE_STATE["orig_excepthook"] = sys.excepthook
    _ACTIVE_STATE["orig_threading_excepthook"] = getattr(threading, "excepthook", None)

    def _kip_excepthook(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any) -> Any:
        if issubclass(exc_type, KeyboardInterrupt):
            logging.getLogger("kip.exception").warning("Interrupted by KeyboardInterrupt")
        else:
            logging.getLogger("kip.exception").critical(
                "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
            )
        orig = _ACTIVE_STATE["orig_excepthook"]
        if callable(orig):
            return orig(exc_type, exc_value, exc_tb)
        return None

    def _kip_threading_excepthook(args: Any) -> Any:
        exc_type = getattr(args, "exc_type", Exception)
        exc_value = getattr(args, "exc_value", None)
        exc_tb = getattr(args, "exc_traceback", None)
        thread = getattr(args, "thread", None)
        t_name = getattr(thread, "name", "unknown")
        if not issubclass(exc_type, SystemExit):
            logging.getLogger("kip.exception").critical(
                "Uncaught exception in thread %s", t_name, exc_info=(exc_type, exc_value, exc_tb)
            )
        orig = _ACTIVE_STATE["orig_threading_excepthook"]
        if callable(orig):
            return orig(args)
        return None

    sys.excepthook = _kip_excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _kip_threading_excepthook  # type: ignore[assignment]
    _ACTIVE_STATE["excepthook_installed"] = True


def _uninstall_excepthooks() -> None:
    if not _ACTIVE_STATE["excepthook_installed"]:
        return
    if _ACTIVE_STATE["orig_excepthook"] is not None:
        sys.excepthook = _ACTIVE_STATE["orig_excepthook"]
    if _ACTIVE_STATE["orig_threading_excepthook"] is not None and hasattr(threading, "excepthook"):
        threading.excepthook = _ACTIVE_STATE["orig_threading_excepthook"]  # type: ignore[assignment]
    _ACTIVE_STATE["orig_excepthook"] = None
    _ACTIVE_STATE["orig_threading_excepthook"] = None
    _ACTIVE_STATE["excepthook_installed"] = False


def _teardown_stdio_tees() -> None:
    for key, sys_attr, orig_key in (
        ("stdout_tee", "stdout", "orig_stdout"),
        ("stderr_tee", "stderr", "orig_stderr"),
    ):
        tee = _ACTIVE_STATE.get(key)
        orig = _ACTIVE_STATE.get(orig_key)
        if isinstance(tee, _TeeStream):
            if getattr(sys, sys_attr, None) is tee and orig is not None:
                setattr(sys, sys_attr, orig)
            tee.close()
        _ACTIVE_STATE[key] = None
        _ACTIVE_STATE[orig_key] = None


def teardown_logging(target: str | None = None) -> None:
    """Remove managed file handlers and restore streams/excepthooks (primarily for tests & reconfiguration)."""
    with _LOCK:
        _detach_managed_handlers(target)
        if target is None or _ACTIVE_STATE.get("target") == target:
            _teardown_stdio_tees()
            _uninstall_excepthooks()
            for handler, prev_level in _ACTIVE_STATE.get("bumped_stream_handlers", []):
                try:
                    handler.setLevel(prev_level)
                except Exception:
                    pass
            _ACTIVE_STATE["bumped_stream_handlers"] = []
            prev_root = _ACTIVE_STATE.get("root_prev_level")
            if prev_root is not None:
                logging.getLogger().setLevel(prev_root)
                _ACTIVE_STATE["root_prev_level"] = None
            _ACTIVE_STATE["target"] = None
            os.environ.pop(ACTIVE_TARGET_ENV, None)


def _attach_webapp_non_propagating(handlers: list[logging.Handler], request_mode: str, level_no: int) -> None:
    """Attach managed handlers directly to uvicorn loggers that do not propagate to root."""
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        if name == "uvicorn.error" and lg.propagate:
            continue
        if name == "uvicorn.access":
            # Access records are emitted at INFO; keep logger permissive so our filter inspects status >= 400
            if lg.level == logging.NOTSET or lg.level > logging.INFO:
                lg.setLevel(logging.INFO)
        elif lg.level == logging.NOTSET or lg.level > level_no:
            lg.setLevel(level_no)
        if not lg.propagate or name in ("uvicorn", "uvicorn.access"):
            for h in handlers:
                if h not in lg.handlers:
                    lg.addHandler(h)


def setup_logging(
    target: str,
    cfg: dict[str, Any] | None = None,
    *,
    capture_stdio: bool = True,
    install_excepthook: bool = True,
    force: bool = False,
    base_root: Path | None = None,
) -> dict[str, Any]:
    """Configure file logging for `'parser'` or `'webapp'` from config.

    Captures all Python `logging` records (including third-party/uvicorn non-propagating loggers),
    `stdout`/`stderr` output (`print()`, warnings, C-extension stderr), and uncaught exceptions.
    """
    if target not in ("parser", "webapp"):
        raise ValueError(f"Unknown logging target: {target!r}")

    if not force and is_test_environment():
        return {
            "enabled": False,
            "target": target,
            "main_path": None,
            "error_path": None,
            "level": "INFO",
            "request_mode": "all" if target == "webapp" else None,
        }

    section_cfg = cfg if cfg is not None else get_logging_config(target)
    level_name = normalize_log_level(section_cfg.get("level"))
    level_no = getattr(logging, level_name, logging.INFO)
    request_mode = normalize_webapp_request_mode(section_cfg.get("request_mode")) if target == "webapp" else None

    main_path, error_path = resolve_log_targets(section_cfg, target, base_root=base_root)

    with _LOCK:
        teardown_logging(None)
        if main_path is None and error_path is None:
            return {
                "enabled": False,
                "target": target,
                "main_path": None,
                "error_path": None,
                "level": level_name,
                "request_mode": request_mode,
            }

        root = logging.getLogger()
        orig_stdout = _unwrap_stream(sys.stdout)
        orig_stderr = _unwrap_stream(sys.stderr)
        _unwrap_terminal_handlers(orig_stdout, orig_stderr)

        # Ensure root passes records down to file handlers without flooding existing console handlers
        old_root_level = root.level
        effective_root = level_no
        if target == "webapp":
            effective_root = min(effective_root, logging.INFO)
        if old_root_level == logging.NOTSET:
            old_effective = logging.INFO
        else:
            old_effective = old_root_level
        if effective_root < old_effective:
            bumped: list[tuple[logging.Handler, int]] = []
            for h in root.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    if h.level == logging.NOTSET or h.level < old_effective:
                        bumped.append((h, h.level))
                        h.setLevel(old_effective)
            _ACTIVE_STATE["bumped_stream_handlers"] = bumped
            _ACTIVE_STATE["root_prev_level"] = old_root_level
            root.setLevel(effective_root)
        elif old_root_level == logging.NOTSET:
            _ACTIVE_STATE["root_prev_level"] = old_root_level
            root.setLevel(logging.INFO)

        handlers: list[logging.Handler] = []
        if main_path is not None:
            main_handler = _ManagedFileHandler(main_path, target, "main")
            if target == "webapp":
                main_handler.addFilter(WebappMainLogFilter(level_no, request_mode or "all"))
            else:
                main_handler.addFilter(ParserMainLogFilter(level_no))
            root.addHandler(main_handler)
            handlers.append(main_handler)

        if error_path is not None:
            err_handler = _ManagedFileHandler(error_path, target, "error")
            err_handler.addFilter(ErrorOnlyLogFilter())
            root.addHandler(err_handler)
            handlers.append(err_handler)

        if target == "webapp":
            _attach_webapp_non_propagating(handlers, request_mode or "all", level_no)
        else:
            # Attach to any non-propagating loggers used by parser modules
            for lg in _all_loggers():
                if lg is not root and not lg.propagate:
                    for h in handlers:
                        if h not in lg.handlers:
                            lg.addHandler(h)

        if capture_stdio:
            capture_stdout_to_main = not (
                target == "webapp" and (request_mode == "errors" or level_no > logging.INFO)
            )
            _ACTIVE_STATE["orig_stdout"] = orig_stdout
            _ACTIVE_STATE["orig_stderr"] = orig_stderr
            stdout_tee = _TeeStream(
                orig_stdout,
                is_stderr=False,
                main_path=main_path,
                error_path=None,
                capture_stdout_to_main=capture_stdout_to_main,
            )
            stderr_tee = _TeeStream(
                orig_stderr,
                is_stderr=True,
                main_path=main_path,
                error_path=error_path,
                capture_stdout_to_main=True,
            )
            _ACTIVE_STATE["stdout_tee"] = stdout_tee
            _ACTIVE_STATE["stderr_tee"] = stderr_tee
            sys.stdout = stdout_tee  # type: ignore[assignment]
            sys.stderr = stderr_tee  # type: ignore[assignment]

        if install_excepthook:
            _install_excepthooks()

        _ACTIVE_STATE["target"] = target
        os.environ[ACTIVE_TARGET_ENV] = target

        return {
            "enabled": True,
            "target": target,
            "main_path": main_path,
            "error_path": error_path,
            "level": level_name,
            "request_mode": request_mode,
        }


def setup_parser_logging(
    cfg: dict[str, Any] | None = None,
    *,
    capture_stdio: bool = True,
    install_excepthook: bool = True,
    force: bool = False,
    base_root: Path | None = None,
) -> dict[str, Any]:
    """Activate file logging for the parser pipeline."""
    return setup_logging(
        "parser",
        cfg=cfg,
        capture_stdio=capture_stdio,
        install_excepthook=install_excepthook,
        force=force,
        base_root=base_root,
    )


def setup_webapp_logging(
    cfg: dict[str, Any] | None = None,
    *,
    capture_stdio: bool = True,
    install_excepthook: bool = True,
    force: bool = False,
    base_root: Path | None = None,
) -> dict[str, Any]:
    """Activate file logging for the webapp backend."""
    return setup_logging(
        "webapp",
        cfg=cfg,
        capture_stdio=capture_stdio,
        install_excepthook=install_excepthook,
        force=force,
        base_root=base_root,
    )


def configure_worker_logging_from_env() -> dict[str, Any] | None:
    """Attach logging in a spawned worker process if `KIP_LOG_ACTIVE_TARGET` is active."""
    target = os.environ.get(ACTIVE_TARGET_ENV, "").strip()
    if target not in ("parser", "webapp"):
        return None
    if _ACTIVE_STATE.get("target") == target:
        return None
    return setup_logging(target)
