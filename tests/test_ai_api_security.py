"""tests/test_ai_api_security.py - Authentication, lockout, proxy IP extraction, and log redaction tests for api/."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.security import (
    ApiAuthMiddleware,
    extract_client_ip,
    parse_trusted_proxies,
    verify_api_key_constant_time,
)
from core import config as core_config


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _run_asgi(app, *, path: str = "/api/v1/capabilities", client=("198.51.100.10", 12345), headers=None):
    captured_status = 0
    captured_headers: dict[bytes, bytes] = {}
    body_chunks: list[bytes] = []

    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": raw_headers,
        "client": client,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        nonlocal captured_status
        if msg["type"] == "http.response.start":
            captured_status = int(msg["status"])
            captured_headers.update({k.lower(): v for k, v in msg.get("headers", ())})
        elif msg["type"] == "http.response.body":
            body_chunks.append(msg.get("body", b""))

    await app(scope, receive, send)
    body = b"".join(body_chunks)
    return captured_status, captured_headers, json.loads(body.decode("utf-8")) if body else {}


class TestAiApiSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_cfg = copy.deepcopy(core_config._ACTIVE_CONFIG)
        self._orig_cfg_path = core_config._LOADED_CONFIG_PATH
        self._orig_env = dict(os.environ)
        core_config.reset_config()
        self._tmp = tempfile.TemporaryDirectory()
        self._repo_dir = Path(self._tmp.name) / "linux"
        self._repo_dir.mkdir()
        self._cfg_path = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        core_config.reset_config()
        os.environ.clear()
        os.environ.update(self._orig_env)
        core_config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core_config._LOADED_CONFIG_PATH = self._orig_cfg_path
        self._tmp.cleanup()

    def _write_cfg(self, api_section: dict) -> None:
        self._cfg_path.write_text(
            json.dumps(
                {
                    "workspace": {"auto_detect_repos": False},
                    "repositories": [{"name": "linux", "path": str(self._repo_dir), "default": True}],
                    "api": api_section,
                }
            ),
            encoding="utf-8",
        )
        core_config.init_config(self._cfg_path)

    def test_bearer_and_x_api_key_auth_list_and_mapping(self) -> None:
        for keys_cfg in (["secret-alpha", "secret-beta"], {"agent_a": "secret-alpha", "agent_b": "secret-beta"}):
            self._write_cfg({"keys": keys_cfg})
            client = TestClient(create_app())

            r_bearer = client.get("/api/v1/capabilities", headers={"Authorization": "Bearer secret-alpha"})
            self.assertEqual(r_bearer.status_code, 200)
            self.assertEqual(r_bearer.headers.get("X-Content-Type-Options"), "nosniff")

            r_header = client.get("/api/v1/capabilities", headers={"X-API-Key": "secret-beta"})
            self.assertEqual(r_header.status_code, 200)

            r_openapi = client.get("/api/openapi.json", headers={"Authorization": "Bearer secret-alpha"})
            self.assertEqual(r_openapi.status_code, 200)
            self.assertIn("/api/v1/capabilities", r_openapi.json().get("paths", {}))

    def test_fail_closed_when_keys_empty_or_missing(self) -> None:
        self._write_cfg({"keys": []})
        client = TestClient(create_app())
        for headers in ({}, {"Authorization": "Bearer anything"}, {"X-API-Key": "anything"}):
            r = client.get("/api/v1/capabilities", headers=headers)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json(), {"detail": "Invalid or missing API key"})

    def test_unauthenticated_surface_and_disabled_docs_and_no_cors(self) -> None:
        self._write_cfg({"keys": ["valid-key"]})
        client = TestClient(create_app())

        # Unauthenticated requests to openapi.json, capabilities, and disabled docs paths are rejected
        for path in ("/api/openapi.json", "/api/v1/capabilities", "/docs", "/redoc"):
            r = client.get(path)
            self.assertEqual(r.status_code, 401, path)

        # Authenticated requests confirm /docs and /redoc are disabled
        for doc_path in ("/docs", "/redoc"):
            r_auth = client.get(doc_path, headers={"X-API-Key": "valid-key"})
            self.assertEqual(r_auth.status_code, 404, doc_path)

        # No wildcard CORS headers even when Origin is sent
        r_cors = client.get(
            "/api/v1/capabilities",
            headers={"Authorization": "Bearer valid-key", "Origin": "https://evil.example"},
        )
        self.assertEqual(r_cors.status_code, 200)
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in r_cors.headers})

        r_preflight = client.options(
            "/api/v1/capabilities",
            headers={"Authorization": "Bearer valid-key", "Origin": "https://evil.example"},
        )
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in r_preflight.headers})

    def test_constant_time_verification_scans_all_keys(self) -> None:
        configured = [("k1", "alpha"), ("k2", "beta"), ("k3", "gamma")]
        with patch("api.security.hmac.compare_digest", side_effect=[False, True, False]) as cmp_mock:
            matched = verify_api_key_constant_time("beta", configured)
        self.assertEqual(matched, "k2")
        self.assertEqual(cmp_mock.call_count, 3)

        self.assertIsNone(verify_api_key_constant_time("alpha", []))

    def test_progressive_lockout_blocks_valid_keys_and_resets_on_valid_auth(self) -> None:
        clock = _FakeClock()

        async def ok_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"ok":true}'})

        mw = ApiAuthMiddleware(
            ok_app,
            keys={"agent": "good-key"},
            rate_limit={"max_requests": 100, "window_seconds": 60},
            auth_lockout={
                "max_failures": 3,
                "failure_window_seconds": 60,
                "base_lockout_seconds": 30,
                "max_lockout_seconds": 90,
            },
            clock=clock,
        )

        # 2 failures (one missing key, one invalid key), then valid auth resets the streak
        s1, _, _ = asyncio.run(_run_asgi(mw, client=("203.0.113.10", 1000)))
        s2, _, _ = asyncio.run(_run_asgi(mw, client=("203.0.113.10", 1000), headers={"X-API-Key": "wrong"}))
        self.assertEqual((s1, s2), (401, 401))

        s_ok, _, _ = asyncio.run(_run_asgi(mw, client=("203.0.113.10", 1000), headers={"X-API-Key": "good-key"}))
        self.assertEqual(s_ok, 200)

        # Now 3 consecutive failures trigger tier-1 lockout (30s)
        for idx in range(3):
            hdrs = {} if idx == 0 else {"Authorization": "Bearer bad"}
            st, _, body = asyncio.run(_run_asgi(mw, client=("203.0.113.10", 1000), headers=hdrs))
            self.assertEqual(st, 401)
            self.assertEqual(body, {"detail": "Invalid or missing API key"})

        # Pre-auth lockout blocks even a valid key with 429 + Retry-After
        st_locked, hdrs_locked, _ = asyncio.run(
            _run_asgi(mw, client=("203.0.113.10", 1000), headers={"Authorization": "Bearer good-key"})
        )
        self.assertEqual(st_locked, 429)
        self.assertEqual(int(hdrs_locked[b"retry-after"]), 30)

        # Other IPs are unaffected
        st_other, _, _ = asyncio.run(
            _run_asgi(mw, client=("203.0.113.20", 1000), headers={"Authorization": "Bearer good-key"})
        )
        self.assertEqual(st_other, 200)

        # After 30s cooldown expires, 3 more failures escalate to tier-2 (60s)
        clock.advance(31)
        for _ in range(3):
            st, _, _ = asyncio.run(_run_asgi(mw, client=("203.0.113.10", 1000), headers={"X-API-Key": "bad"}))
            self.assertEqual(st, 401)
        st_tier2, hdrs_tier2, _ = asyncio.run(
            _run_asgi(mw, client=("203.0.113.10", 1000), headers={"X-API-Key": "good-key"})
        )
        self.assertEqual(st_tier2, 429)
        self.assertEqual(int(hdrs_tier2[b"retry-after"]), 60)

        # Tier-3 caps at max_lockout_seconds (90s, not 120s)
        clock.advance(61)
        for _ in range(3):
            asyncio.run(_run_asgi(mw, client=("203.0.113.10", 1000), headers={"X-API-Key": "bad"}))
        st_tier3, hdrs_tier3, _ = asyncio.run(
            _run_asgi(mw, client=("203.0.113.10", 1000), headers={"X-API-Key": "good-key"})
        )
        self.assertEqual(st_tier3, 429)
        self.assertEqual(int(hdrs_tier3[b"retry-after"]), 90)

    def test_trusted_proxy_ip_extraction_and_spoofing_rejection(self) -> None:
        nets = parse_trusted_proxies(["127.0.0.1", "::1", "10.0.0.0/8"])

        # Untrusted direct peer cannot spoof X-Forwarded-For / X-Real-IP / CF-Connecting-IP
        for hdr in ("X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP"):
            scope = {
                "type": "http",
                "client": ("198.51.100.99", 4444),
                "headers": [(hdr.lower().encode(), b"203.0.113.5")],
            }
            self.assertEqual(extract_client_ip(scope, nets, hdr), "198.51.100.99")

        # Trusted peer walks X-Forwarded-For right-to-left skipping trusted hops
        scope_xff = {
            "type": "http",
            "client": ("127.0.0.1", 4444),
            "headers": [(b"x-forwarded-for", b"1.1.1.1, 198.51.100.77, 10.2.3.4")],
        }
        self.assertEqual(extract_client_ip(scope_xff, nets, "X-Forwarded-For"), "198.51.100.77")

        # Malformed rightmost untrusted hop falls back to peer IP
        scope_bad_xff = {
            "type": "http",
            "client": ("127.0.0.1", 4444),
            "headers": [(b"x-forwarded-for", b"198.51.100.77, not-an-ip, 10.2.3.4")],
        }
        self.assertEqual(extract_client_ip(scope_bad_xff, nets, "X-Forwarded-For"), "127.0.0.1")

        # Trusted peer honors X-Real-IP and CF-Connecting-IP
        for hdr in ("X-Real-IP", "CF-Connecting-IP"):
            scope_single = {
                "type": "http",
                "client": ("::1", 4444),
                "headers": [(hdr.lower().encode(), b"2001:db8::42")],
            }
            self.assertEqual(extract_client_ip(scope_single, nets, hdr), "2001:db8::42")

    def test_authenticated_per_key_and_per_ip_rate_limiting_and_eviction(self) -> None:
        clock = _FakeClock()

        async def ok_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        mw = ApiAuthMiddleware(
            ok_app,
            keys={"k1": "key-one", "k2": "key-two"},
            rate_limit={"max_requests": 2, "window_seconds": 60},
            auth_lockout={"max_failures": 5, "failure_window_seconds": 60, "base_lockout_seconds": 30, "max_lockout_seconds": 60},
            clock=clock,
            max_tracked_ips=16,
            max_tracked_keys=16,
        )

        # Same key across two different IPs hits per-key limit on 3rd request
        self.assertEqual(asyncio.run(_run_asgi(mw, client=("198.51.100.1", 1), headers={"X-API-Key": "key-one"}))[0], 200)
        self.assertEqual(asyncio.run(_run_asgi(mw, client=("198.51.100.2", 1), headers={"X-API-Key": "key-one"}))[0], 200)
        st, hdrs, _ = asyncio.run(_run_asgi(mw, client=("198.51.100.3", 1), headers={"X-API-Key": "key-one"}))
        self.assertEqual(st, 429)
        self.assertGreaterEqual(int(hdrs[b"retry-after"]), 1)

        # Bounded memory eviction under IP churn
        for i in range(40):
            asyncio.run(_run_asgi(mw, client=(f"192.0.2.{i}", 1), headers={}))
        self.assertLessEqual(len(mw._ip_failures), 16)

    def test_request_logs_redact_raw_api_keys(self) -> None:
        secret = "super-secret-agent-token-999"
        self._write_cfg({"keys": {"bot": secret}})
        client = TestClient(create_app())

        with self.assertLogs("api.request", level=logging.INFO) as cm:
            r_ok = client.get("/api/v1/capabilities", headers={"Authorization": f"Bearer {secret}"})
            r_bad = client.get("/api/v1/capabilities", headers={"X-API-Key": "wrong-secret-value"})
        self.assertEqual(r_ok.status_code, 200)
        self.assertEqual(r_bad.status_code, 401)
        joined = "\n".join(cm.output)
        self.assertNotIn(secret, joined)
        self.assertNotIn("wrong-secret-value", joined)
        self.assertIn("key=bot", joined)


if __name__ == "__main__":
    unittest.main()
