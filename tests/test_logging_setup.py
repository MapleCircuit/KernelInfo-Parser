"""tests/test_logging_setup.py - Unit tests for parser/webapp file logging configuration & runtime."""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.config as config_mod
import core.logging_setup as log_setup
from core.globalstuff import COLOR, FileContextFormatter, G, strip_ansi
from webapp.backend.app import create_app
from webapp.backend.config import get_backend_logging_config
from webapp.backend.security.middleware import RequestLoggingMiddleware


class _AnsiListHandler(logging.Handler):
    """Capture formatted log lines into a list for multi-handler formatter checks."""

    def __init__(self, fmt: logging.Formatter) -> None:
        super().__init__()
        self.setFormatter(fmt)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class TestLoggingConfig(unittest.TestCase):
    """Verify logging defaults, normalization, and KIP_* env overrides."""

    def setUp(self) -> None:
        self._orig_cfg = copy.deepcopy(config_mod._ACTIVE_CONFIG)
        self._orig_cfg_path = config_mod._LOADED_CONFIG_PATH
        self._orig_env = dict(os.environ)
        config_mod.reset_config()

    def tearDown(self) -> None:
        config_mod.reset_config()
        os.environ.clear()
        os.environ.update(self._orig_env)
        config_mod._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        config_mod._LOADED_CONFIG_PATH = self._orig_cfg_path

    def test_defaults_and_normalizers(self) -> None:
        self.assertIn("logging", config_mod.DEFAULT_CONFIG)
        p_def = config_mod.DEFAULT_CONFIG["logging"]["parser"]
        w_def = config_mod.DEFAULT_CONFIG["logging"]["webapp"]
        self.assertFalse(p_def["enabled"])
        self.assertEqual(p_def["file"], "parser.log")
        self.assertEqual(p_def["level"], "INFO")
        self.assertFalse(w_def["enabled"])
        self.assertEqual(w_def["file"], "webapp.log")
        self.assertEqual(w_def["request_mode"], "all")
        self.assertEqual(w_def["level"], "INFO")

        self.assertEqual(config_mod.normalize_log_level("debug"), "DEBUG")
        self.assertEqual(config_mod.normalize_log_level("warn"), "WARNING")
        self.assertEqual(config_mod.normalize_log_level("invalid"), "INFO")

        self.assertEqual(config_mod.normalize_webapp_request_mode("errors"), "errors")
        self.assertEqual(config_mod.normalize_webapp_request_mode("errors_only"), "errors")
        self.assertEqual(config_mod.normalize_webapp_request_mode("unknown"), "all")

    def test_env_overrides_precedence(self) -> None:
        custom = {
            "logging": {
                "parser": {"enabled": False, "dir": "/tmp/old_p", "file": "old.log", "level": "INFO"},
                "webapp": {
                    "enabled": False,
                    "dir": "/tmp/old_w",
                    "file": "old_w.log",
                    "request_mode": "all",
                    "level": "INFO",
                },
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump(custom, tf)
            cfg_path = tf.name

        try:
            os.environ["KIP_PARSER_LOG_ENABLED"] = "true"
            os.environ["KIP_PARSER_LOG_DIR"] = "/tmp/env_p"
            os.environ["KIP_PARSER_LOG_FILE"] = "env_p.log"
            os.environ["KIP_PARSER_LOG_ERROR_DIR"] = "/tmp/env_p_err"
            os.environ["KIP_PARSER_LOG_ERROR_FILE"] = "env_p_err.log"
            os.environ["KIP_PARSER_LOG_LEVEL"] = "debug"

            os.environ["KIP_WEBAPP_LOG_ENABLED"] = "1"
            os.environ["KIP_WEBAPP_LOG_DIR"] = "/tmp/env_w"
            os.environ["KIP_WEBAPP_LOG_FILE"] = "env_w.log"
            os.environ["KIP_WEBAPP_LOG_ERROR_DIR"] = "/tmp/env_w_err"
            os.environ["KIP_WEBAPP_LOG_ERROR_FILE"] = "env_w_err.log"
            os.environ["KIP_WEBAPP_LOG_REQUEST_MODE"] = "errors"
            os.environ["KIP_WEBAPP_LOG_LEVEL"] = "warning"

            config_mod.load_config(cfg_path)
            p_cfg = config_mod.get_logging_config("parser")
            w_cfg = config_mod.get_logging_config("webapp")
            full_cfg = config_mod.get_logging_config()

            self.assertEqual(full_cfg["parser"], p_cfg)
            self.assertEqual(full_cfg["webapp"], w_cfg)

            self.assertTrue(p_cfg["enabled"])
            self.assertEqual(p_cfg["dir"], "/tmp/env_p")
            self.assertEqual(p_cfg["file"], "env_p.log")
            self.assertEqual(p_cfg["error_dir"], "/tmp/env_p_err")
            self.assertEqual(p_cfg["error_file"], "env_p_err.log")
            self.assertEqual(p_cfg["level"], "DEBUG")

            self.assertTrue(w_cfg["enabled"])
            self.assertEqual(w_cfg["dir"], "/tmp/env_w")
            self.assertEqual(w_cfg["file"], "env_w.log")
            self.assertEqual(w_cfg["error_dir"], "/tmp/env_w_err")
            self.assertEqual(w_cfg["error_file"], "env_w_err.log")
            self.assertEqual(w_cfg["request_mode"], "errors")
            self.assertEqual(w_cfg["level"], "WARNING")
            self.assertEqual(get_backend_logging_config(), w_cfg)
        finally:
            os.unlink(cfg_path)


class TestFileContextFormatter(unittest.TestCase):
    """Ensure FileContextFormatter does not mutate record.msg across handlers and strips ANSI optionally."""

    def setUp(self) -> None:
        self._orig_parsing_file = G.CURRENT_PARSING_FILE

    def tearDown(self) -> None:
        G.CURRENT_PARSING_FILE = self._orig_parsing_file

    def test_strip_ansi_and_non_mutating_format(self) -> None:
        raw = COLOR.green("Stage OK")
        self.assertEqual(strip_ansi(raw), "Stage OK")

        G.CURRENT_PARSING_FILE = "kernel/sched/core.c"
        console_h = _AnsiListHandler(FileContextFormatter("%(levelname)s %(message)s", strip_ansi_codes=False))
        file_h = _AnsiListHandler(FileContextFormatter("%(levelname)s %(message)s", strip_ansi_codes=True))

        test_logger = logging.getLogger("tests.formatter_non_mutate")
        test_logger.propagate = False
        orig_level = test_logger.level
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(console_h)
        test_logger.addHandler(file_h)
        try:
            test_logger.info(raw)
        finally:
            test_logger.removeHandler(console_h)
            test_logger.removeHandler(file_h)
            test_logger.setLevel(orig_level)

        self.assertEqual(console_h.lines[0], f"INFO [kernel/sched/core.c] {raw}")
        self.assertEqual(file_h.lines[0], "INFO [kernel/sched/core.c] Stage OK")


class TestLoggingSetupRuntime(unittest.TestCase):
    """Exercise path resolution, parser logging, webapp request filtering, uvicorn, and teardown."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_cfg = copy.deepcopy(config_mod._ACTIVE_CONFIG)
        self._orig_cfg_path = config_mod._LOADED_CONFIG_PATH
        self._orig_env = dict(os.environ)
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        self._orig_excepthook = sys.excepthook
        self._orig_thread_hook = getattr(threading_mod(), "excepthook", None)
        self._orig_parsing_file = G.CURRENT_PARSING_FILE
        self._orig_root_level = logging.getLogger().level
        log_setup.teardown_logging(None)

    def tearDown(self) -> None:
        log_setup.teardown_logging(None)
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "webapp.request", "parser.submod", "core.submod", "nonprop.mod"):
            lg = logging.getLogger(name)
            lg.handlers.clear()
            lg.setLevel(logging.NOTSET)
            lg.propagate = True
        logging.getLogger().setLevel(self._orig_root_level)
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        sys.excepthook = self._orig_excepthook
        t_mod = threading_mod()
        if self._orig_thread_hook is not None and hasattr(t_mod, "excepthook"):
            t_mod.excepthook = self._orig_thread_hook
        G.CURRENT_PARSING_FILE = self._orig_parsing_file
        os.environ.clear()
        os.environ.update(self._orig_env)
        config_mod._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        config_mod._LOADED_CONFIG_PATH = self._orig_cfg_path
        self.tmp.cleanup()

    def test_resolve_log_path_and_targets(self) -> None:
        self.assertIsNone(log_setup.resolve_log_path("", "", "parser.log", base_root=self.root))
        self.assertEqual(
            log_setup.resolve_log_path("logs/p", "", "parser.log", base_root=self.root),
            (self.root / "logs/p/parser.log").resolve(),
        )
        self.assertEqual(
            log_setup.resolve_log_path("logs/p", "custom.log", "parser.log", base_root=self.root),
            (self.root / "logs/p/custom.log").resolve(),
        )
        abs_file = str((self.root / "abs/out.log").resolve())
        self.assertEqual(
            log_setup.resolve_log_path("ignored", abs_file, "parser.log", base_root=self.root),
            Path(abs_file),
        )

        # Disabled returns (None, None)
        self.assertEqual(log_setup.resolve_log_targets({"enabled": False}, "parser", base_root=self.root), (None, None))

        # Identical main and error paths deduplicate error_path to None
        main_p, err_p = log_setup.resolve_log_targets(
            {"enabled": True, "dir": "logs", "file": "same.log", "error_dir": "logs", "error_file": "same.log"},
            "parser",
            base_root=self.root,
        )
        self.assertEqual(main_p, (self.root / "logs/same.log").resolve())
        self.assertIsNone(err_p)

    def test_test_env_guard_skips_when_not_forced(self) -> None:
        res = log_setup.setup_parser_logging({"enabled": True, "dir": str(self.root), "file": "noop.log"})
        self.assertFalse(res["enabled"])
        self.assertFalse((self.root / "noop.log").exists())

    def test_parser_logging_captures_modules_stdio_excepthook_and_worker(self) -> None:
        cfg = {
            "enabled": True,
            "dir": str(self.root / "p_main"),
            "file": "parser.log",
            "error_dir": str(self.root / "p_err"),
            "error_file": "parser_err.log",
            "level": "INFO",
        }
        # Wrap sys.stdout/sys.stderr with StringIO buffers before setup so tests don't spam console
        fake_out, fake_err = io.StringIO(), io.StringIO()
        sys.stdout = fake_out
        sys.stderr = fake_err

        res = log_setup.setup_parser_logging(cfg, force=True, base_root=self.root)
        self.assertTrue(res["enabled"])
        self.assertEqual(os.environ.get(log_setup.ACTIVE_TARGET_ENV), "parser")

        G.CURRENT_PARSING_FILE = "drivers/net/dummy.c"
        logging.getLogger("parser.submod").debug("hidden debug message")
        logging.getLogger("parser.submod").info(COLOR.cyan("parsing translation unit"))
        logging.getLogger("core.submod").error("fatal type error")

        non_prop = logging.getLogger("nonprop.mod")
        non_prop.propagate = False
        log_setup.setup_parser_logging(cfg, force=True, base_root=self.root)
        # Re-apply StreamIO wrappers since teardown restored orig streams? Actually setup preserves _unwrap_stream!
        print("stdout helper output")
        sys.stderr.write("stderr traceback detail\n")

        non_prop.warning("non-propagating module warning")

        # Simulate uncaught exception through installed sys.excepthook without invoking pytest/unittest crash
        try:
            raise ValueError("uncaught test crash")
        except ValueError as exc:
            sys.excepthook(ValueError, exc, exc.__traceback__)

        log_setup.teardown_logging("parser")
        self.assertIs(sys.stdout, fake_out)
        self.assertIs(sys.stderr, fake_err)

        main_text = (self.root / "p_main/parser.log").read_text(encoding="utf-8")
        err_text = (self.root / "p_err/parser_err.log").read_text(encoding="utf-8")

        self.assertNotIn("hidden debug message", main_text)
        self.assertIn("[drivers/net/dummy.c] parsing translation unit", main_text)
        self.assertNotIn("\x1b[", main_text)
        self.assertIn("fatal type error", main_text)
        self.assertIn("stdout helper output", main_text)
        self.assertIn("stderr traceback detail", main_text)
        self.assertIn("non-propagating module warning", main_text)
        self.assertIn("ValueError: uncaught test crash", main_text)

        self.assertNotIn("stdout helper output", err_text)
        self.assertNotIn("non-propagating module warning", err_text)
        self.assertIn("fatal type error", err_text)
        self.assertIn("stderr traceback detail", err_text)
        self.assertIn("ValueError: uncaught test crash", err_text)

        # Simulate child worker startup attaching parser logging from ACTIVE_TARGET_ENV
        os.environ[log_setup.ACTIVE_TARGET_ENV] = "parser"
        config_mod._ACTIVE_CONFIG["logging"]["parser"] = cfg
        with patch.object(log_setup, "is_test_environment", return_value=False):
            worker_res = log_setup.configure_worker_logging_from_env()
        self.assertIsNotNone(worker_res)
        assert worker_res is not None
        self.assertTrue(worker_res["enabled"])
        logging.getLogger("parser.worker_child").error("worker subprocess error")
        log_setup.teardown_logging("parser")
        self.assertIn(
            "worker subprocess error",
            (self.root / "p_err/parser_err.log").read_text(encoding="utf-8"),
        )

    def test_webapp_logging_all_vs_errors_requests_and_uvicorn(self) -> None:
        # 1) request_mode="all" with separate request dir and error dir
        all_cfg = {
            "enabled": True,
            "dir": str(self.root / "web_all"),
            "file": "requests.log",
            "request_mode": "all",
            "error_dir": str(self.root / "web_all_err"),
            "error_file": "errors.log",
            "level": "INFO",
        }
        log_setup.setup_webapp_logging(all_cfg, force=True, base_root=self.root)

        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)

        @app.get("/ok")
        def ok_route():
            logging.getLogger("webapp.backend.routers.ok").info("handler ok log")
            return {"ok": True}

        @app.get("/boom")
        def boom_route():
            raise RuntimeError("boom error")

        client = TestClient(app, raise_server_exceptions=False)
        self.assertEqual(client.get("/ok").status_code, 200)
        self.assertEqual(client.get("/missing").status_code, 404)
        self.assertEqual(client.get("/boom").status_code, 500)

        # Simulated uvicorn non-propagating loggers emitting 200 and 502 at INFO
        uv_acc = logging.getLogger("uvicorn.access")
        uv_acc.propagate = False
        uv_err = logging.getLogger("uvicorn")
        uv_err.propagate = False
        log_setup.setup_webapp_logging(all_cfg, force=True, base_root=self.root)
        uv_acc.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/uv-ok", "1.1", 200)
        uv_acc.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/uv-bad", "1.1", 502)
        uv_err.error("uvicorn worker error")

        log_setup.teardown_logging("webapp")

        all_main = (self.root / "web_all/requests.log").read_text(encoding="utf-8")
        all_err = (self.root / "web_all_err/errors.log").read_text(encoding="utf-8")
        self.assertIn("GET /ok -> 200", all_main)
        self.assertIn("handler ok log", all_main)
        self.assertIn("GET /missing -> 404", all_main)
        self.assertIn("GET /boom -> 500", all_main)
        self.assertIn("GET /uv-ok", all_main)
        self.assertIn("GET /uv-bad", all_main)
        self.assertIn("uvicorn worker error", all_main)

        self.assertNotIn("GET /ok -> 200", all_err)
        self.assertNotIn("handler ok log", all_err)
        self.assertNotIn("GET /uv-ok", all_err)
        self.assertIn("GET /missing -> 404", all_err)
        self.assertIn("GET /boom -> 500", all_err)
        self.assertIn("GET /uv-bad", all_err)
        self.assertIn("uvicorn worker error", all_err)

        # 2) request_mode="errors" filters out 200 requests & INFO logs even in main log
        err_only_cfg = {
            "enabled": True,
            "dir": str(self.root / "web_err_mode"),
            "file": "webapp.log",
            "request_mode": "errors",
            "error_dir": str(self.root / "web_err_split"),
            "error_file": "only_err.log",
            "level": "INFO",
        }
        fake_out, fake_err = io.StringIO(), io.StringIO()
        sys.stdout = fake_out
        sys.stderr = fake_err
        log_setup.setup_webapp_logging(err_only_cfg, force=True, base_root=self.root)

        self.assertEqual(client.get("/ok").status_code, 200)
        self.assertEqual(client.get("/missing").status_code, 404)
        self.assertEqual(client.get("/boom").status_code, 500)
        uv_acc.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/uv-200", "1.1", 200)
        uv_acc.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/uv-404", "1.1", 404)
        print("ignored stdout line in errors mode")
        log_setup.teardown_logging("webapp")

        mode_main = (self.root / "web_err_mode/webapp.log").read_text(encoding="utf-8")
        mode_err = (self.root / "web_err_split/only_err.log").read_text(encoding="utf-8")
        self.assertNotIn("GET /ok -> 200", mode_main)
        self.assertNotIn("handler ok log", mode_main)
        self.assertNotIn("GET /uv-200", mode_main)
        self.assertNotIn("ignored stdout line", mode_main)
        self.assertIn("GET /missing -> 404", mode_main)
        self.assertIn("GET /boom -> 500", mode_main)
        self.assertIn("GET /uv-404", mode_main)
        self.assertIn("GET /missing -> 404", mode_err)
        self.assertIn("GET /boom -> 500", mode_err)
        self.assertIn("GET /uv-404", mode_err)

    def test_create_app_wires_setup_webapp_logging(self) -> None:
        config_mod._ACTIVE_CONFIG["logging"]["webapp"] = {
            "enabled": True,
            "dir": str(self.root / "create_app_logs"),
            "file": "app.log",
            "request_mode": "errors",
            "error_dir": "",
            "error_file": "",
            "level": "INFO",
        }
        with patch.object(log_setup, "is_test_environment", return_value=False):
            app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        self.assertEqual(client.get("/openapi.json").status_code, 200)
        self.assertEqual(client.get("/nonexistent-route-xyz").status_code, 404)
        log_setup.teardown_logging("webapp")

        text = (self.root / "create_app_logs/app.log").read_text(encoding="utf-8")
        self.assertNotIn("/openapi.json -> 200", text)
        self.assertIn("/nonexistent-route-xyz -> 404", text)


def threading_mod():
    import threading
    return threading


if __name__ == "__main__":
    unittest.main()
