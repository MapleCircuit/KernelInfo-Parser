"""tests/test_systemd_manager.py - Unit tests for core/systemd_manager.py. No live systemd or DB required."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import systemd_manager as sm


class TestConstantsAndTypes(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(sm.DAEMON_SERVICE_NAME, "kernelinfo-daemon")
        self.assertEqual(sm.DAEMON_UNIT_NAME, "kernelinfo-daemon.service")
        self.assertEqual(sm.WEBAPP_SERVICE_NAME, "kernelinfo-webapp")
        self.assertEqual(sm.WEBAPP_UNIT_NAME, "kernelinfo-webapp.service")
        self.assertEqual(sm.SYSTEMD_UNIT_DIR, Path("/etc/systemd/system"))
        self.assertEqual(sm.DAEMON_TMUX_SOCKET, "kernelinfo-daemon")
        self.assertEqual(sm.DAEMON_TMUX_SESSION, "kernelinfo-daemon")
        self.assertEqual(sm.WEBAPP_TMUX_SOCKET, "kernelinfo-webapp")
        self.assertEqual(sm.WEBAPP_TMUX_SESSION, "kernelinfo-webapp")
        self.assertEqual(sm.API_SERVICE_NAME, "kernelinfo-api")
        self.assertEqual(sm.API_UNIT_NAME, "kernelinfo-api.service")
        self.assertEqual(sm.API_TMUX_SOCKET, "kernelinfo-api")
        self.assertEqual(sm.API_TMUX_SESSION, "kernelinfo-api")

    def test_valid_services_include_api(self) -> None:
        self.assertEqual(sm.VALID_SERVICES, ("daemon", "webapp", "api"))

    def test_service_unit_config_validation(self) -> None:
        cfg = sm.ServiceUnitConfig(
            service="daemon",
            user="alice",
            group="staff",
            use_tmux=False,
            project_root=Path("/opt/kip"),
            config_path=Path("/opt/kip/config.json"),
            python_executable="/usr/bin/python3",
        )
        self.assertEqual(cfg.service, "daemon")
        self.assertIsInstance(cfg.project_root, Path)

        with self.assertRaises(ValueError):
            sm.ServiceUnitConfig(
                service="unknown",  # type: ignore[arg-type]
                user="alice",
                group="staff",
                use_tmux=False,
                project_root=Path("/opt/kip"),
                config_path=Path("/opt/kip/config.json"),
                python_executable="/usr/bin/python3",
            )


class TestUnitRenderingAndParsing(unittest.TestCase):
    def test_render_direct_daemon(self) -> None:
        cfg = sm.ServiceUnitConfig(
            service="daemon",
            user="kip",
            group="kipgrp",
            use_tmux=False,
            project_root=Path("/opt/kip"),
            config_path=Path("/opt/kip/config.json"),
            python_executable="/usr/bin/python3",
        )
        unit = sm.render_unit_file(cfg)
        self.assertIn("Description=KernelInfo-Parser Daemon Service", unit)
        self.assertIn("After=network-online.target", unit)
        self.assertIn("Wants=network-online.target", unit)
        self.assertIn("Type=simple", unit)
        self.assertIn("User=kip", unit)
        self.assertIn("Group=kipgrp", unit)
        self.assertIn("WorkingDirectory=/opt/kip", unit)
        self.assertIn("Environment=PYTHONUNBUFFERED=1", unit)
        self.assertIn("Environment=KERNELINFO_CONFIG=/opt/kip/config.json", unit)
        self.assertIn(
            "ExecStart=/usr/bin/python3 /opt/kip/main.py --config /opt/kip/config.json --role daemon",
            unit,
        )
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=10", unit)
        self.assertIn("KillSignal=SIGTERM", unit)
        self.assertIn("TimeoutStopSec=30", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

        parsed = sm.parse_unit_file(unit, "daemon")
        self.assertEqual(parsed["service"], "daemon")
        self.assertEqual(parsed["user"], "kip")
        self.assertEqual(parsed["group"], "kipgrp")
        self.assertEqual(parsed["mode"], "direct")
        self.assertEqual(parsed["config_path"], "/opt/kip/config.json")

    def test_render_direct_webapp(self) -> None:
        cfg = sm.ServiceUnitConfig(
            service="webapp",
            user="www",
            group="www-data",
            use_tmux=False,
            project_root=Path("/opt/kip"),
            config_path=Path("/etc/kip/prod.json"),
            python_executable="/opt/venv/bin/python",
        )
        unit = sm.render_unit_file(cfg)
        self.assertIn("Description=KernelInfo-Parser Webapp Service", unit)
        self.assertIn("Type=simple", unit)
        self.assertIn(
            "ExecStart=/opt/venv/bin/python /opt/kip/webapp/main.py --config /etc/kip/prod.json",
            unit,
        )
        self.assertIn("Environment=KERNELINFO_CONFIG=/etc/kip/prod.json", unit)
        self.assertNotIn("--role daemon", unit)

        parsed = sm.parse_unit_file(unit, "webapp")
        self.assertEqual(parsed["user"], "www")
        self.assertEqual(parsed["group"], "www-data")
        self.assertEqual(parsed["mode"], "direct")
        self.assertEqual(parsed["config_path"], "/etc/kip/prod.json")

    def test_render_direct_api(self) -> None:
        cfg = sm.ServiceUnitConfig(
            service="api",
            user="kip",
            group="kip",
            use_tmux=False,
            project_root=Path("/opt/kip"),
            config_path=Path("/opt/kip/config_api.json"),
            python_executable="/opt/venv/bin/python",
        )
        unit = sm.render_unit_file(cfg)
        self.assertIn("Description=KernelInfo-Parser API Service", unit)
        self.assertIn("Type=simple", unit)
        self.assertIn(
            "ExecStart=/opt/venv/bin/python /opt/kip/api/main.py --config /opt/kip/config_api.json",
            unit,
        )
        self.assertNotIn("--role daemon", unit)
        parsed = sm.parse_unit_file(unit, "api")
        self.assertEqual(parsed["user"], "kip")
        self.assertEqual(parsed["mode"], "direct")
        self.assertEqual(parsed["config_path"], "/opt/kip/config_api.json")

    def test_render_tmux_api(self) -> None:
        cfg = sm.ServiceUnitConfig(
            service="api",
            user="alice",
            group="alice",
            use_tmux=True,
            project_root=Path("/opt/kip"),
            config_path=Path("/opt/kip/config_api.json"),
            python_executable="/usr/bin/python3",
        )
        unit = sm.render_unit_file(cfg, tmux_bin="/usr/bin/tmux")
        self.assertIn("Type=forking", unit)
        self.assertIn("api/main.py", unit)
        self.assertIn("kernelinfo-api", unit)
        self.assertEqual(sm.parse_unit_file(unit, "api")["mode"], "tmux")
        self.assertEqual(
            sm.get_attach_command("api", "kipuser", current_user="alice"),
            "sudo -u kipuser tmux -L kernelinfo-api attach -t kernelinfo-api",
        )

    def test_render_tmux_daemon_and_webapp(self) -> None:
        for svc, socket, session, expected_tail in (
            ("daemon", "kernelinfo-daemon", "kernelinfo-daemon", "--role daemon"),
            ("webapp", "kernelinfo-webapp", "kernelinfo-webapp", "webapp/main.py"),
        ):
            cfg = sm.ServiceUnitConfig(
                service=svc,  # type: ignore[arg-type]
                user="alice",
                group="alice",
                use_tmux=True,
                project_root=Path("/opt/kip"),
                config_path=Path("/opt/kip/config.json"),
                python_executable="/usr/bin/python3",
            )
            unit = sm.render_unit_file(cfg, tmux_bin="/usr/bin/tmux")
            self.assertIn("(tmux)", unit)
            self.assertIn("StartLimitIntervalSec=60", unit)
            self.assertIn("StartLimitBurst=5", unit)
            self.assertIn("Type=forking", unit)
            self.assertIn("Restart=always", unit)
            self.assertIn("RestartSec=10", unit)
            self.assertIn("KillSignal=SIGTERM", unit)
            self.assertIn("TimeoutStopSec=30", unit)
            self.assertIn(f"-L {socket} new-session -d -s {session} -c /opt/kip", unit)
            self.assertIn(expected_tail, unit)
            self.assertIn("ExecStop=/bin/sh -c", unit)
            self.assertIn("list-panes", unit)
            self.assertIn("#{pane_pid}", unit)
            self.assertIn("kill -TERM", unit)
            self.assertIn("kill -0", unit)
            self.assertIn(f"kill-session -t {session}", unit)
            self.assertNotIn("remain-on-exit on", unit)
            self.assertIn("remain-on-exit off", unit)

            parsed = sm.parse_unit_file(unit, svc)
            self.assertEqual(parsed["user"], "alice")
            self.assertEqual(parsed["group"], "alice")
            self.assertEqual(parsed["mode"], "tmux")
            self.assertEqual(parsed["config_path"], "/opt/kip/config.json")

    def test_render_and_parse_with_spaces_in_paths(self) -> None:
        for use_tmux in (False, True):
            cfg = sm.ServiceUnitConfig(
                service="daemon",
                user="alice",
                group="staff",
                use_tmux=use_tmux,
                project_root=Path("/opt/my kip"),
                config_path=Path("/opt/my kip/prod config.json"),
                python_executable="/opt/py 3/bin/python",
            )
            unit = sm.render_unit_file(cfg, tmux_bin="/usr/bin/tmux")
            self.assertIn('Environment="KERNELINFO_CONFIG=/opt/my kip/prod config.json"', unit)
            self.assertIn("WorkingDirectory=/opt/my kip", unit)
            parsed = sm.parse_unit_file(unit, "daemon")
            self.assertEqual(parsed["mode"], "tmux" if use_tmux else "direct")
            self.assertEqual(parsed["config_path"], "/opt/my kip/prod config.json")
            self.assertEqual(parsed["working_directory"], "/opt/my kip")

    def test_parse_unit_file_env_fallback(self) -> None:
        unit = "\n".join(
            [
                "[Service]",
                "User=bob",
                "Group=users",
                "Environment=KERNELINFO_CONFIG=/etc/kip/fallback.json",
                "ExecStart=/usr/bin/python3 /opt/kip/main.py --role daemon",
            ]
        )
        parsed = sm.parse_unit_file(unit, "daemon")
        self.assertEqual(parsed["user"], "bob")
        self.assertEqual(parsed["group"], "users")
        self.assertEqual(parsed["mode"], "direct")
        self.assertEqual(parsed["config_path"], "/etc/kip/fallback.json")


class TestOwnershipAndEnvironment(unittest.TestCase):
    def test_default_user_group(self) -> None:
        user, group = sm.default_user_group()
        self.assertTrue(user)
        self.assertTrue(group)

    def test_get_attach_command(self) -> None:
        self.assertEqual(
            sm.get_attach_command("daemon", "alice", current_user="alice"),
            "tmux -L kernelinfo-daemon attach -t kernelinfo-daemon",
        )
        self.assertEqual(
            sm.get_attach_command("webapp", "kipuser", current_user="alice"),
            "sudo -u kipuser tmux -L kernelinfo-webapp attach -t kernelinfo-webapp",
        )
        with self.assertRaises(ValueError):
            sm.get_attach_command("invalid", "alice", current_user="alice")

    def test_validate_user_group_valid_and_missing(self) -> None:
        user, group = sm.default_user_group()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir(mode=0o755)
            cfg = root / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            os.chmod(cfg, 0o644)

            ok, errors, warnings = sm.validate_user_group(user, group, root, cfg)
            self.assertTrue(ok)
            self.assertEqual(errors, [])
            # If /tmp or parent is 0755 and owned by current user, there should be no permission warning
            self.assertEqual(warnings, [])

            ok_bad_u, errs_u, _ = sm.validate_user_group("nonexistent_kip_user_xyz", group, root, cfg)
            self.assertFalse(ok_bad_u)
            self.assertTrue(any("does not exist" in e for e in errs_u))

            ok_bad_g, errs_g, _ = sm.validate_user_group(user, "nonexistent_kip_group_xyz", root, cfg)
            self.assertFalse(ok_bad_g)
            self.assertTrue(any("does not exist" in e for e in errs_g))

            ok_empty, errs_e, _ = sm.validate_user_group("", "", root, cfg)
            self.assertFalse(ok_empty)
            self.assertEqual(len(errs_e), 2)

    def test_validate_user_group_unreadable_path_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private_project"
            root.mkdir(mode=0o700)
            cfg = root / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            os.chmod(cfg, 0o600)

            fake_pw = mock.MagicMock(pw_name="svcuser", pw_uid=99999, pw_gid=99999)
            fake_gr = mock.MagicMock(gr_name="svcgrp", gr_gid=99999)
            with (
                mock.patch("core.systemd_manager.pwd.getpwnam", return_value=fake_pw),
                mock.patch("core.systemd_manager.grp.getgrnam", return_value=fake_gr),
                mock.patch("core.systemd_manager.os.getgrouplist", return_value=[99999]),
            ):
                ok, errors, warnings = sm.validate_user_group("svcuser", "svcgrp", root, cfg)
            self.assertTrue(ok)
            self.assertEqual(errors, [])
            self.assertTrue(len(warnings) >= 1)
            joined = " ".join(warnings)
            self.assertIn("svcuser", joined)


class TestPreflightAndLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir(mode=0o755)
        (self.root / "main.py").write_text("# daemon", encoding="utf-8")
        (self.root / "webapp").mkdir(mode=0o755)
        (self.root / "webapp" / "main.py").write_text("# webapp", encoding="utf-8")
        self.cfg_path = self.root / "config.json"
        self.cfg_path.write_text(json.dumps({"database": {"engine": "sqlite"}}), encoding="utf-8")
        self.user, self.group = sm.default_user_group()

    def _make_cfg(self, *, service: str = "daemon", use_tmux: bool = False) -> sm.ServiceUnitConfig:
        return sm.ServiceUnitConfig(
            service=service,  # type: ignore[arg-type]
            user=self.user,
            group=self.group,
            use_tmux=use_tmux,
            project_root=self.root,
            config_path=self.cfg_path,
            python_executable="/usr/bin/python3",
        )

    def test_preflight_missing_and_invalid_config(self) -> None:
        missing_cfg = sm.ServiceUnitConfig(
            service="daemon",
            user=self.user,
            group=self.group,
            use_tmux=False,
            project_root=self.root,
            config_path=self.root / "does_not_exist.json",
            python_executable="/usr/bin/python3",
        )
        ok, errs, _ = sm.preflight_service_install(missing_cfg)
        self.assertFalse(ok)
        self.assertTrue(any("not found" in e for e in errs))

        bad_json = self.root / "bad.json"
        bad_json.write_text("{not valid json", encoding="utf-8")
        bad_cfg = sm.ServiceUnitConfig(
            service="daemon",
            user=self.user,
            group=self.group,
            use_tmux=False,
            project_root=self.root,
            config_path=bad_json,
            python_executable="/usr/bin/python3",
        )
        ok_bad, errs_bad, _ = sm.preflight_service_install(bad_cfg)
        self.assertFalse(ok_bad)
        self.assertTrue(any("JSON" in e for e in errs_bad), errs_bad)

        # Also verify fallback path when no external validator is present
        ok_fb, errs_fb, _ = sm._validate_config_fallback(bad_json, "daemon")
        self.assertFalse(ok_fb)
        self.assertTrue(any("Invalid JSON" in e for e in errs_fb))

    def test_preflight_missing_tmux_and_custom_validator(self) -> None:
        cfg = self._make_cfg(use_tmux=True)
        with mock.patch("core.systemd_manager.find_tmux_binary", return_value=None):
            ok, errs, _ = sm.preflight_service_install(cfg)
        self.assertFalse(ok)
        self.assertTrue(any("tmux" in e.lower() for e in errs))

        with mock.patch("core.systemd_manager.find_tmux_binary", return_value="/usr/bin/tmux"):
            ok_v, errs_v, _ = sm.preflight_service_install(
                cfg,
                validate_config_fn=lambda path, service: (False, [f"bad {service} config"], []),
            )
            self.assertFalse(ok_v)
            self.assertIn("bad daemon config", errs_v)

            ok_good, errs_good, _ = sm.preflight_service_install(
                cfg,
                validate_config_fn=lambda path, service: (True, [], []),
            )
            self.assertTrue(ok_good)
            self.assertEqual(errs_good, [])

            # Also test with default core.config.validate_service_config
            ok_real, errs_real, _ = sm.preflight_service_install(cfg)
            self.assertTrue(ok_real, errs_real)
            self.assertEqual(errs_real, [])

    def test_get_service_status_installed_tmux_and_missing_systemctl(self) -> None:
        unit_dir = Path(self.tmp.name) / "systemd"
        unit_dir.mkdir()
        cfg = self._make_cfg(service="daemon", use_tmux=True)
        (unit_dir / sm.DAEMON_UNIT_NAME).write_text(
            sm.render_unit_file(cfg, tmux_bin="/usr/bin/tmux"),
            encoding="utf-8",
        )

        def fake_runner(cmd: list[str]) -> sm.CommandResult:
            if cmd[:2] == ["systemctl", "show"]:
                stdout = "\n".join(
                    [
                        "LoadState=loaded",
                        "UnitFileState=enabled",
                        "ActiveState=active",
                        "SubState=running",
                        "MainPID=4321",
                        f"User={self.user}",
                        f"Group={self.group}",
                    ]
                )
                return sm.CommandResult(ok=True, returncode=0, stdout=stdout, stderr="", command=cmd)
            raise AssertionError(f"Unexpected command: {cmd}")

        status = sm.get_service_status("daemon", unit_dir=unit_dir, runner=fake_runner)
        self.assertTrue(status.installed)
        self.assertEqual(status.enabled, "enabled")
        self.assertEqual(status.active, "active")
        self.assertEqual(status.sub_state, "running")
        self.assertEqual(status.mode, "tmux")
        self.assertEqual(status.main_pid, 4321)
        self.assertEqual(status.attach_command, "tmux -L kernelinfo-daemon attach -t kernelinfo-daemon")
        self.assertIsNone(status.error)

        def missing_systemctl(cmd: list[str]) -> sm.CommandResult:
            raise FileNotFoundError("systemctl not found")

        status_missing = sm.get_service_status("webapp", unit_dir=unit_dir, runner=missing_systemctl)
        self.assertFalse(status_missing.installed)
        self.assertEqual(status_missing.active, "unknown")
        self.assertEqual(status_missing.enabled, "unknown")
        self.assertIsNotNone(status_missing.error)

    def test_install_service(self) -> None:
        unit_dir = Path(self.tmp.name) / "systemd_install"
        unit_dir.mkdir()
        cfg = self._make_cfg(service="webapp", use_tmux=False)
        calls: list[tuple[list[str], str | None]] = []

        def fake_priv(cmd: list[str], input_text: str | None = None) -> sm.CommandResult:
            calls.append((cmd, input_text))
            return sm.CommandResult(ok=True, returncode=0, stdout="", stderr="", command=cmd)

        ok, msg = sm.install_service(
            cfg,
            unit_dir=unit_dir,
            enable=True,
            start=True,
            privileged_runner=fake_priv,
            validate_config_fn=lambda path, service: (True, [], []),
        )
        self.assertTrue(ok, msg)
        self.assertIn("Installed kernelinfo-webapp.service", msg)
        self.assertEqual(
            [c[0] for c in calls],
            [
                ["sudo", "tee", str(unit_dir / sm.WEBAPP_UNIT_NAME)],
                ["sudo", "chmod", "644", str(unit_dir / sm.WEBAPP_UNIT_NAME)],
                ["sudo", "systemctl", "daemon-reload"],
                ["sudo", "systemctl", "enable", sm.WEBAPP_UNIT_NAME],
                ["sudo", "systemctl", "start", sm.WEBAPP_UNIT_NAME],
            ],
        )
        self.assertIsNotNone(calls[0][1])
        self.assertIn("webapp/main.py", calls[0][1] or "")

    def test_install_service_failure_stops_early(self) -> None:
        unit_dir = Path(self.tmp.name) / "systemd_fail"
        unit_dir.mkdir()
        cfg = self._make_cfg(service="daemon", use_tmux=False)

        def fail_chmod(cmd: list[str], input_text: str | None = None) -> sm.CommandResult:
            if "chmod" in cmd:
                return sm.CommandResult(ok=False, returncode=1, stdout="", stderr="permission denied", command=cmd)
            return sm.CommandResult(ok=True, returncode=0, stdout="", stderr="", command=cmd)

        ok, msg = sm.install_service(
            cfg,
            unit_dir=unit_dir,
            privileged_runner=fail_chmod,
            validate_config_fn=lambda path, service: (True, [], []),
        )
        self.assertFalse(ok)
        self.assertIn("permission denied", msg)

    def test_uninstall_service_active_and_inactive(self) -> None:
        unit_dir = Path(self.tmp.name) / "systemd_uninst"
        unit_dir.mkdir()
        priv_calls: list[list[str]] = []

        def fake_priv(cmd: list[str], input_text: str | None = None) -> sm.CommandResult:
            priv_calls.append(cmd)
            return sm.CommandResult(ok=True, returncode=0, stdout="", stderr="", command=cmd)

        def active_runner(cmd: list[str]) -> sm.CommandResult:
            return sm.CommandResult(
                ok=True,
                returncode=0,
                stdout="LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\nMainPID=101\n",
                stderr="",
                command=cmd,
            )

        ok, msg = sm.uninstall_service(
            "daemon",
            unit_dir=unit_dir,
            privileged_runner=fake_priv,
            runner=active_runner,
        )
        self.assertTrue(ok, msg)
        self.assertEqual(
            priv_calls,
            [
                ["sudo", "systemctl", "stop", sm.DAEMON_UNIT_NAME],
                ["sudo", "systemctl", "disable", sm.DAEMON_UNIT_NAME],
                ["sudo", "rm", "-f", str(unit_dir / sm.DAEMON_UNIT_NAME)],
                ["sudo", "systemctl", "daemon-reload"],
            ],
        )

        priv_calls.clear()

        def inactive_runner(cmd: list[str]) -> sm.CommandResult:
            return sm.CommandResult(
                ok=True,
                returncode=0,
                stdout="LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\nMainPID=0\n",
                stderr="",
                command=cmd,
            )

        ok2, _ = sm.uninstall_service(
            "webapp",
            unit_dir=unit_dir,
            privileged_runner=fake_priv,
            runner=inactive_runner,
        )
        self.assertTrue(ok2)
        self.assertEqual(
            priv_calls,
            [
                ["sudo", "rm", "-f", str(unit_dir / sm.WEBAPP_UNIT_NAME)],
                ["sudo", "systemctl", "daemon-reload"],
            ],
        )

    def test_control_service_and_read_logs(self) -> None:
        recorded: list[list[str]] = []

        def fake_priv(cmd: list[str], input_text: str | None = None) -> sm.CommandResult:
            recorded.append(cmd)
            return sm.CommandResult(ok=True, returncode=0, stdout="ok", stderr="", command=cmd)

        for action in ("start", "stop", "restart", "enable", "disable"):
            res = sm.control_service("daemon", action, privileged_runner=fake_priv)
            self.assertTrue(res.ok)
            self.assertEqual(res.command, ["sudo", "systemctl", action, sm.DAEMON_UNIT_NAME])

        with self.assertRaises(ValueError):
            sm.control_service("daemon", "reload-or-explode", privileged_runner=fake_priv)

        def fake_log_runner(cmd: list[str]) -> sm.CommandResult:
            return sm.CommandResult(ok=True, returncode=0, stdout="line1\nline2\n", stderr="", command=cmd)

        log_res = sm.read_service_logs("webapp", lines=50, runner=fake_log_runner)
        self.assertTrue(log_res.ok)
        self.assertEqual(
            log_res.command,
            ["journalctl", "-u", sm.WEBAPP_UNIT_NAME, "-n", "50", "--no-pager"],
        )
        self.assertIn("line1", log_res.stdout)

        with self.assertRaises(ValueError):
            sm.read_service_logs("webapp", lines=0, runner=fake_log_runner)


if __name__ == "__main__":
    unittest.main()
