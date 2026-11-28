"""tests/test_ai_oauth.py - End-to-end OAuth 2.1 authorization server tests for api/."""
from __future__ import annotations

import base64
import copy
import html
import io
import json
import os
import re
import socket
import stat
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.oauth import (
    OAuthError,
    compute_pkce_s256,
    is_allowed_redirect_uri,
    is_public_unicast_ip,
    matches_client_redirect_uri,
    oauth_manager,
    resolve_and_validate_cimd_host,
)
from core import config as core_config


def _extract_csrf_token(html_text: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_text)
    if not match:
        raise AssertionError("csrf_token hidden field not found in consent HTML")
    return html.unescape(match.group(1))


class TestAiOAuthServer(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_cfg = copy.deepcopy(core_config._ACTIVE_CONFIG)
        self._orig_cfg_path = core_config._LOADED_CONFIG_PATH
        core_config.reset_config()
        self._orig_env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self._repo_dir = Path(self._tmp.name) / "linux"
        self._repo_dir.mkdir()
        self._oauth_dir = Path(self._tmp.name) / "oauth-data"
        self._canvas_dir = Path(self._tmp.name) / "canvases"
        self._cfg_path = Path(self._tmp.name) / "config.json"
        oauth_manager._cached_secret = None
        oauth_manager._cimd_cache.clear()
        oauth_manager._dcr_ip_hits.clear()
        oauth_manager._revoked_jtis.clear()

    def tearDown(self) -> None:
        core_config.reset_config()
        os.environ.clear()
        os.environ.update(self._orig_env)
        core_config._ACTIVE_CONFIG = copy.deepcopy(self._orig_cfg)
        core_config._LOADED_CONFIG_PATH = self._orig_cfg_path
        oauth_manager._cached_secret = None
        oauth_manager._cimd_cache.clear()
        oauth_manager._dcr_ip_hits.clear()
        oauth_manager._revoked_jtis.clear()
        self._tmp.cleanup()

    def _write_cfg(self, api_overrides: dict | None = None) -> None:
        api_sec = {
            "keys": {
                "reader": {"key": "reader-secret-key-1111", "scopes": ["read"]},
                "writer": {"key": "writer-secret-key-2222", "scopes": ["read", "nodemap:write"]},
            },
            "allow_writes": True,
            "nodemap_canvas_dir": str(self._canvas_dir),
            "trusted_proxies": ["127.0.0.1", "::1", "testclient"],
            "oauth": {
                "enabled": True,
                "issuer": "https://kernelinfo.org",
                "data_dir": str(self._oauth_dir),
                "access_token_ttl": 3600,
                "refresh_token_ttl": 2592000,
                "auth_code_ttl": 120,
                "allow_dcr": True,
                "allow_cimd": True,
                "clients": [
                    {
                        "client_id": "static-public",
                        "name": "Static Public Client",
                        "redirect_uris": [
                            "https://claude.ai/api/mcp/auth_callback",
                            "http://127.0.0.1/callback",
                        ],
                    },
                    {
                        "client_id": "static-confidential",
                        "client_secret": "conf-secret-xyz-9876",
                        "name": "Static Confidential Client",
                        "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
                    },
                ],
            },
        }
        if api_overrides:
            for k, v in api_overrides.items():
                if k == "oauth" and isinstance(v, dict):
                    api_sec["oauth"].update(v)
                else:
                    api_sec[k] = v
        self._cfg_path.write_text(
            json.dumps(
                {
                    "database": {"engine": "mock", "ssh_tunnel": {"enabled": False}},
                    "workspace": {"auto_detect_repos": False},
                    "repositories": [{"name": "linux", "path": str(self._repo_dir), "default": True}],
                    "api": api_sec,
                }
            ),
            encoding="utf-8",
        )
        core_config.reset_config()
        core_config.init_config(self._cfg_path)

    # ------------------------------------------------------------------ 1. Disabled-by-default & Discovery

    def test_disabled_by_default_returns_404_and_preserves_static_keys(self) -> None:
        self._write_cfg({"oauth": {"enabled": False}})
        client = TestClient(create_app())

        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/api/mcp",
            "/.well-known/oauth-protected-resource/api/v1",
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
            "/api/oauth/authorize",
        ):
            r = client.get(path)
            self.assertEqual(r.status_code, 404, path)

        for post_path in ("/api/oauth/register", "/api/oauth/token", "/api/oauth/revoke", "/api/oauth/authorize"):
            r = client.post(post_path, json={})
            self.assertEqual(r.status_code, 404, post_path)

        # Static API keys still work
        r_ok = client.get("/api/v1/capabilities", headers={"X-API-Key": "reader-secret-key-1111"})
        self.assertEqual(r_ok.status_code, 200)

    def test_discovery_metadata_endpoints_and_forwarded_issuer_derivation(self) -> None:
        self._write_cfg({"allow_writes": True, "oauth": {"issuer": ""}})
        client = TestClient(create_app())

        fwd_headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "kip.example.org",
        }
        r_prm = client.get("/.well-known/oauth-protected-resource/api/mcp", headers=fwd_headers)
        self.assertEqual(r_prm.status_code, 200)
        prm = r_prm.json()
        self.assertEqual(prm["resource"], "https://kip.example.org/api/mcp")
        self.assertEqual(prm["authorization_servers"], ["https://kip.example.org"])
        self.assertEqual(prm["bearer_methods_supported"], ["header"])
        self.assertEqual(prm["scopes_supported"], ["read", "nodemap:write", "write"])

        r_prm_v1 = client.get("/.well-known/oauth-protected-resource/api/v1", headers=fwd_headers)
        self.assertEqual(r_prm_v1.status_code, 200)
        self.assertEqual(r_prm_v1.json()["resource"], "https://kip.example.org/api/v1")

        # When allow_writes is False, write scopes are filtered out of discovery metadata
        self._write_cfg({"allow_writes": False, "oauth": {"allow_dcr": False, "allow_cimd": True}})
        client_ro = TestClient(create_app())
        r_as = client_ro.get("/.well-known/oauth-authorization-server")
        self.assertEqual(r_as.status_code, 200)
        as_meta = r_as.json()
        self.assertEqual(as_meta["issuer"], "https://kernelinfo.org")
        self.assertEqual(as_meta["authorization_endpoint"], "https://kernelinfo.org/api/oauth/authorize")
        self.assertEqual(as_meta["token_endpoint"], "https://kernelinfo.org/api/oauth/token")
        self.assertEqual(as_meta["revocation_endpoint"], "https://kernelinfo.org/api/oauth/revoke")
        self.assertNotIn("registration_endpoint", as_meta)
        self.assertEqual(as_meta["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(as_meta["scopes_supported"], ["read"])
        self.assertTrue(as_meta["client_id_metadata_document_supported"])

        # OIDC discovery fallback returns the same metadata
        r_oidc = client_ro.get("/.well-known/openid-configuration")
        self.assertEqual(r_oidc.status_code, 200)
        self.assertEqual(r_oidc.json()["issuer"], "https://kernelinfo.org")

    def test_auto_generated_signing_secret_persisted_with_0600_permissions(self) -> None:
        self._write_cfg({"oauth": {"signing_secret": ""}})
        secret_1 = oauth_manager.get_signing_secret()
        self.assertGreaterEqual(len(secret_1), 32)
        secret_file = self._oauth_dir / "signing_secret.json"
        self.assertTrue(secret_file.is_file())
        mode = stat.S_IMODE(secret_file.stat().st_mode)
        self.assertEqual(mode, 0o600)

        # Survives cache clear / restart
        oauth_manager._cached_secret = None
        secret_2 = oauth_manager.get_signing_secret()
        self.assertEqual(secret_1, secret_2)

    # ------------------------------------------------------------------ 2. Redirect URI Validation & DCR

    def test_redirect_uri_allowlist_and_loopback_matching(self) -> None:
        # Allowed major AI supplier URIs and loopback with arbitrary ports
        for good in (
            "https://claude.ai/api/mcp/auth_callback",
            "https://chatgpt.com/connector_platform_oauth_redirect",
            "https://chatgpt.com/connector/oauth/cb123",
            "https://www.cursor.com/agents/mcp/oauth/callback",
            "cursor://anysphere.cursor-mcp/oauth/callback",
            "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-ead712414208c54e8a55b127",
            "https://gemini.google.com/oauth/callback",
            "https://a.b.google.com/callback",
            "https://vscode.dev/redirect",
            "vscode://vscode.git/authenticate",
            "http://localhost:54321/callback",
            "http://127.0.0.1:8080/oauth/cb",
            "http://[::1]:3000/auth",
        ):
            self.assertTrue(is_allowed_redirect_uri(good), good)

        # Forbidden schemes, non-loopback HTTP, fragments, and unallowed hosts
        for bad in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "http://claude.ai/api/mcp/auth_callback",
            "http://evil.example.com/callback",
            "https://evil.example.com/callback",
            "https://claude.ai/api/mcp/auth_callback#frag",
            "https://user:pass@claude.ai/api/mcp/auth_callback",
            "https://claude.ai/api/mcp/../other",
        ):
            self.assertFalse(is_allowed_redirect_uri(bad), bad)

        # RFC 8252 port-agnostic loopback matching against registered loopback URI
        self.assertTrue(
            matches_client_redirect_uri("http://127.0.0.1:49152/callback", ["http://127.0.0.1/callback"])
        )
        self.assertTrue(
            matches_client_redirect_uri("http://localhost:9999/cb", ["http://localhost:3000/cb"])
        )
        self.assertFalse(
            matches_client_redirect_uri("http://127.0.0.1:49152/other", ["http://127.0.0.1/callback"])
        )

    def test_dynamic_client_registration_public_and_confidential(self) -> None:
        self._write_cfg()
        client = TestClient(create_app())

        # 1. Public client registration
        r_pub = client.post(
            "/api/oauth/register",
            json={
                "client_name": "Claude Code CLI",
                "redirect_uris": ["http://localhost:54321/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        self.assertEqual(r_pub.status_code, 201, r_pub.text)
        pub_data = r_pub.json()
        self.assertTrue(pub_data["client_id"].startswith("dcr_"))
        self.assertNotIn("client_secret", pub_data)
        self.assertTrue((self._oauth_dir / "clients" / f"{pub_data['client_id']}.json").is_file())

        # 2. Confidential client registration
        r_conf = client.post(
            "/api/oauth/register",
            json={
                "client_name": "Confidential Connector",
                "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
                "token_endpoint_auth_method": "client_secret_basic",
            },
        )
        self.assertEqual(r_conf.status_code, 201)
        conf_data = r_conf.json()
        self.assertIn("client_secret", conf_data)
        # Disk file stores only sha256 hash, never plaintext secret
        stored = json.loads((self._oauth_dir / "clients" / f"{conf_data['client_id']}.json").read_text(encoding="utf-8"))
        self.assertNotIn("client_secret", stored)
        self.assertIn("client_secret_sha256", stored)

        # 3. Disallowed redirect URI rejected with 400 invalid_redirect_uri
        for bad_uri in ("https://evil.example.org/cb", "javascript:alert(1)", "http://example.com/cb"):
            r_bad = client.post(
                "/api/oauth/register",
                json={"client_name": "Bad", "redirect_uris": [bad_uri]},
            )
            self.assertEqual(r_bad.status_code, 400, bad_uri)
            self.assertEqual(r_bad.json()["error"], "invalid_redirect_uri")

    # ------------------------------------------------------------------ 3. CIMD Resolution & SSRF Protection

    def test_cimd_ssrf_defenses_and_resolution(self) -> None:
        self._write_cfg()

        # Public unicast IP check blocks loopback, private, link-local (cloud metadata), multicast, unspecified
        for blocked_ip in (
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
        ):
            self.assertFalse(is_public_unicast_ip(blocked_ip), blocked_ip)
        self.assertTrue(is_public_unicast_ip("93.184.216.34"))

        # Direct loopback/internal hostnames and private DNS resolutions are blocked
        for bad_host in ("localhost", "127.0.0.1", "169.254.169.254", "internal.local", "metadata.internal"):
            with self.assertRaises(OAuthError):
                resolve_and_validate_cimd_host(bad_host)

        with patch(
            "api.oauth.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))],
        ):
            with self.assertRaises(OAuthError) as ctx:
                resolve_and_validate_cimd_host("rebind.example.com")
            self.assertIn("non-public IP", ctx.exception.description)

        # Non-HTTPS CIMD URL is rejected
        with self.assertRaises(OAuthError):
            oauth_manager.resolve_client("http://claude.ai/oauth/client.json")

        # Valid HTTPS CIMD URL with mocked DNS + HTTPS response succeeds and caches
        cimd_url = "https://claude.ai/oauth/mcp-client.json"
        cimd_payload = {
            "client_id": cimd_url,
            "client_name": "Claude AI",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "token_endpoint_auth_method": "none",
        }

        class _FakeResp:
            status = 200

            def __init__(self, data: bytes) -> None:
                self._buf = io.BytesIO(data)

            def read(self, n: int = -1) -> bytes:
                return self._buf.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with (
            patch(
                "api.oauth.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            ),
            patch(
                "api.oauth.urllib.request.OpenerDirector.open",
                return_value=_FakeResp(json.dumps(cimd_payload).encode("utf-8")),
            ) as mock_open,
        ):
            resolved = oauth_manager.resolve_client(cimd_url)
            self.assertEqual(resolved.client_id, cimd_url)
            self.assertEqual(resolved.client_name, "Claude AI")
            self.assertEqual(resolved.source, "cimd")
            # Second call hits in-memory cache without network fetch
            resolved_cached = oauth_manager.resolve_client(cimd_url)
            self.assertEqual(resolved_cached.client_id, cimd_url)
            self.assertEqual(mock_open.call_count, 1)

    # ------------------------------------------------------------------ 4. Full Authorization Code + PKCE Flow

    def test_full_auth_code_pkce_flow_consent_security_and_replay_rejection(self) -> None:
        self._write_cfg()
        client = TestClient(create_app())

        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk-test-verifier-12345"
        challenge = compute_pkce_s256(verifier)
        redirect_uri = "http://127.0.0.1:51234/callback"

        # 1. Missing PKCE or code_challenge_method=plain is rejected with 400
        for bad_params in (
            {"response_type": "code", "client_id": "static-public", "redirect_uri": redirect_uri},
            {
                "response_type": "code",
                "client_id": "static-public",
                "redirect_uri": redirect_uri,
                "code_challenge": verifier,
                "code_challenge_method": "plain",
            },
        ):
            r_bad_pkce = client.get("/api/oauth/authorize", params=bad_params)
            self.assertEqual(r_bad_pkce.status_code, 400)
            self.assertEqual(r_bad_pkce.json()["error"], "invalid_request")

        # 2. Unregistered redirect_uri is rejected with 400 (no open redirect)
        r_bad_redir = client.get(
            "/api/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "static-public",
                "redirect_uri": "https://evil.example.com/cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        self.assertEqual(r_bad_redir.status_code, 400)

        # 3. GET /api/oauth/authorize renders consent HTML with strict CSP, X-Frame-Options: DENY, and loopback warning
        auth_params = {
            "response_type": "code",
            "client_id": "static-public",
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz-state-<script>",
            "scope": "read nodemap:write",
            "resource": "https://kernelinfo.org/api/mcp",
        }
        r_get = client.get("/api/oauth/authorize", params=auth_params)
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", r_get.headers.get("Content-Security-Policy", ""))
        self.assertIn("Loopback Redirect Warning", r_get.text)
        # XSS attempt in state is HTML-escaped
        self.assertNotIn("<script>", r_get.text)
        self.assertIn("xyz-state-&lt;script&gt;", r_get.text)
        csrf_token = _extract_csrf_token(r_get.text)

        # 4. POST with bad CSRF token is rejected with 403
        r_bad_csrf = client.post(
            "/api/oauth/authorize",
            data={**auth_params, "csrf_token": "tampered.token", "api_key": "reader-secret-key-1111", "decision": "approve"},
            follow_redirects=False,
        )
        self.assertEqual(r_bad_csrf.status_code, 403)

        # 5. POST with decision=deny redirects with error=access_denied
        r_deny = client.post(
            "/api/oauth/authorize",
            data={**auth_params, "csrf_token": csrf_token, "api_key": "", "decision": "deny"},
            follow_redirects=False,
        )
        self.assertEqual(r_deny.status_code, 302)
        deny_q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r_deny.headers["location"]).query))
        self.assertEqual(deny_q["error"], "access_denied")
        self.assertEqual(deny_q["state"], "xyz-state-<script>")

        # 6. POST with reader key approves, capping requested "read nodemap:write" scope to "read"
        r_approve = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "csrf_token": csrf_token,
                "api_key": "reader-secret-key-1111",
                "decision": "approve",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_approve.status_code, 302)
        loc = urllib.parse.urlsplit(r_approve.headers["location"])
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", redirect_uri)
        loc_q = dict(urllib.parse.parse_qsl(loc.query))
        code = loc_q["code"]
        self.assertEqual(loc_q["state"], "xyz-state-<script>")

        # 7. Wrong PKCE code_verifier fails without allowing token issuance
        r_wrong_pkce = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "static-public",
                "redirect_uri": redirect_uri,
                "code_verifier": "wrong-verifier-with-at-least-43-characters-0000000000",
            },
        )
        self.assertEqual(r_wrong_pkce.status_code, 400)
        self.assertEqual(r_wrong_pkce.json()["error"], "invalid_grant")

        # 8. Issue a fresh code and exchange it with valid PKCE code_verifier
        r_approve2 = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "csrf_token": csrf_token,
                "api_key": "reader-secret-key-1111",
                "decision": "approve",
            },
            follow_redirects=False,
        )
        code2 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r_approve2.headers["location"]).query))["code"]

        r_tok = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code2,
                "client_id": "static-public",
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        self.assertEqual(r_tok.status_code, 200, r_tok.text)
        self.assertEqual(r_tok.headers.get("Cache-Control"), "no-store")
        tok_data = r_tok.json()
        self.assertEqual(tok_data["token_type"], "Bearer")
        self.assertEqual(tok_data["scope"], "read")  # capped to reader key's scopes
        access_token = tok_data["access_token"]
        refresh_token = tok_data["refresh_token"]

        # 9. Replaying the same authorization code fails with invalid_grant AND revokes the refresh token issued from it
        r_replay = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code2,
                "client_id": "static-public",
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        self.assertEqual(r_replay.status_code, 400)
        self.assertEqual(r_replay.json()["error"], "invalid_grant")

        r_rt_after_replay = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": "static-public",
            },
        )
        self.assertEqual(r_rt_after_replay.status_code, 400)

        # 10. Issued read-only access token works on GET /api/v1/capabilities; no canvas write route exists
        r_cap = client.get("/api/v1/capabilities", headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(r_cap.status_code, 200)

        r_write = client.post(
            "/api/v1/nodemap/canvases",
            json={"name": "Denied"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertIn(r_write.status_code, (404, 405))

    # ------------------------------------------------------------------ 5. Lockout on Consent Brute-Force

    def test_consent_bad_api_key_triggers_ip_lockout(self) -> None:
        self._write_cfg(
            {
                "auth_lockout": {
                    "max_failures": 3,
                    "failure_window_seconds": 60,
                    "base_lockout_seconds": 30,
                    "max_lockout_seconds": 60,
                }
            }
        )
        client = TestClient(create_app())
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk-lockout-verifier-123"
        challenge = compute_pkce_s256(verifier)
        params = {
            "response_type": "code",
            "client_id": "static-public",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        r_get = client.get("/api/oauth/authorize", params=params)
        csrf = _extract_csrf_token(r_get.text)

        for _ in range(3):
            r_fail = client.post(
                "/api/oauth/authorize",
                data={**params, "csrf_token": csrf, "api_key": "wrong-key", "decision": "approve"},
            )
            self.assertEqual(r_fail.status_code, 401)
            csrf = _extract_csrf_token(r_fail.text)

        # 4th attempt is locked out with 429 even with a valid API key
        r_locked = client.post(
            "/api/oauth/authorize",
            data={**params, "csrf_token": csrf, "api_key": "reader-secret-key-1111", "decision": "approve"},
        )
        self.assertEqual(r_locked.status_code, 429)
        self.assertIn("Retry-After", r_locked.headers)

    # ------------------------------------------------------------------ 6. Refresh Rotation, Dynamic Scope Cap & Revocation

    def test_confidential_client_refresh_rotation_scope_cap_and_revocation(self) -> None:
        self._write_cfg()
        client = TestClient(create_app())
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk-refresh-verifier-999"
        challenge = compute_pkce_s256(verifier)
        redirect_uri = "https://chatgpt.com/connector_platform_oauth_redirect"
        params = {
            "response_type": "code",
            "client_id": "static-confidential",
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "read nodemap:write",
        }
        r_get = client.get("/api/oauth/authorize", params=params)
        csrf = _extract_csrf_token(r_get.text)
        r_approve = client.post(
            "/api/oauth/authorize",
            data={**params, "csrf_token": csrf, "api_key": "writer-secret-key-2222", "decision": "approve"},
            follow_redirects=False,
        )
        code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r_approve.headers["location"]).query))["code"]

        # Confidential client without secret is rejected with 401 invalid_client
        r_no_sec = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "static-confidential",
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        # Re-issue code since authorization codes are single-use on consumption attempt
        r_approve2 = client.post(
            "/api/oauth/authorize",
            data={**params, "csrf_token": csrf, "api_key": "writer-secret-key-2222", "decision": "approve"},
            follow_redirects=False,
        )
        code2 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r_approve2.headers["location"]).query))["code"]
        self.assertEqual(r_no_sec.status_code, 401)
        self.assertEqual(r_no_sec.json()["error"], "invalid_client")

        # Exchange with HTTP Basic auth (client_secret_basic)
        basic_creds = base64.b64encode(b"static-confidential:conf-secret-xyz-9876").decode("ascii")
        r_tok = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code2,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            headers={"Authorization": f"Basic {basic_creds}"},
        )
        self.assertEqual(r_tok.status_code, 200, r_tok.text)
        tok1 = r_tok.json()
        self.assertEqual(tok1["scope"], "nodemap:write read")
        rt1 = tok1["refresh_token"]

        # Flip allow_writes to False -> refresh token rotation dynamically drops nodemap:write
        with patch.dict(os.environ, {"API_ALLOW_WRITES": "0"}):
            r_refresh = client.post(
                "/api/oauth/token",
                data={"grant_type": "refresh_token", "refresh_token": rt1},
                headers={"Authorization": f"Basic {basic_creds}"},
            )
            self.assertEqual(r_refresh.status_code, 200, r_refresh.text)
            tok2 = r_refresh.json()
            self.assertEqual(tok2["scope"], "read")
            rt2 = tok2["refresh_token"]
            self.assertNotEqual(rt1, rt2)

            # Old refresh token rt1 cannot be reused
            r_old = client.post(
                "/api/oauth/token",
                data={"grant_type": "refresh_token", "refresh_token": rt1},
                headers={"Authorization": f"Basic {basic_creds}"},
            )
            self.assertEqual(r_old.status_code, 400)

        # Revoke rt2 via POST /api/oauth/revoke (idempotent 200 OK)
        r_rev1 = client.post(
            "/api/oauth/revoke",
            data={"token": rt2},
            headers={"Authorization": f"Basic {basic_creds}"},
        )
        self.assertEqual(r_rev1.status_code, 200)
        r_rev2 = client.post(
            "/api/oauth/revoke",
            data={"token": rt2},
            headers={"Authorization": f"Basic {basic_creds}"},
        )
        self.assertEqual(r_rev2.status_code, 200)

        # Revoked refresh token can no longer be used
        r_after_rev = client.post(
            "/api/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": rt2},
            headers={"Authorization": f"Basic {basic_creds}"},
        )
        self.assertEqual(r_after_rev.status_code, 400)

    def test_removing_backing_api_key_invalidates_access_and_refresh_tokens_and_cors_preflight(self) -> None:
        self._write_cfg()
        client = TestClient(create_app())

        # Browser CORS preflight on /api/mcp when OAuth is enabled
        r_opt = client.options(
            "/api/mcp",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
        self.assertEqual(r_opt.status_code, 204)
        self.assertEqual(r_opt.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("WWW-Authenticate", r_opt.headers.get("Access-Control-Expose-Headers", ""))

        # Issue access + refresh tokens for 'writer' key
        at, _ = oauth_manager.issue_access_token(
            issuer="https://kernelinfo.org",
            sub="writer",
            aud="https://kernelinfo.org/api/mcp",
            client_id="static-public",
            scopes=["read", "nodemap:write"],
            ttl=3600,
        )
        rt = oauth_manager.issue_refresh_token(
            client_id="static-public",
            key_id="writer",
            scopes=["read", "nodemap:write"],
            resource="https://kernelinfo.org/api/mcp",
        )
        self.assertEqual(
            client.get("/api/v1/capabilities", headers={"Authorization": f"Bearer {at}"}).status_code,
            200,
        )

        # Remove 'writer' from api.keys -> both access token and refresh token immediately fail
        self._write_cfg({"keys": {"reader": "reader-secret-key-1111"}})
        client_after = TestClient(create_app())
        self.assertEqual(
            client_after.get("/api/v1/capabilities", headers={"Authorization": f"Bearer {at}"}).status_code,
            401,
        )
        r_rt = client_after.post(
            "/api/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": "static-public"},
        )
        self.assertEqual(r_rt.status_code, 400)
        self.assertEqual(r_rt.json()["error"], "invalid_grant")


if __name__ == "__main__":
    unittest.main()
