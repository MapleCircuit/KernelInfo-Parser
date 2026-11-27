"""api/oauth.py - Self-contained OAuth 2.1 authorization server for MCP and REST API clients.

Implements:
- RFC 9728 OAuth 2.0 Protected Resource Metadata & `WWW-Authenticate` challenges
- RFC 8414 OAuth 2.0 Authorization Server Metadata & OIDC Discovery compatibility
- OAuth 2.1 Authorization Code grant with mandatory PKCE `S256` (RFC 7636)
- RFC 8707 Resource Indicators for OAuth 2.0
- RFC 7591 Dynamic Client Registration (DCR) with redirect URI allowlist enforcement
- MCP Client ID Metadata Documents (CIMD) with DNS/IP SSRF protection and bounded caching
- RFC 7009 Token Revocation and atomic refresh token rotation
- Stateless HMAC-SHA256 access tokens (`typ: "kip+at"`) backed by configured `api.keys` scopes
- Atomic JSON persistence under `<oauth_data_dir>` with zero SQLite or `cache_engine` dependency
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.config import (
    DEFAULT_API_SCOPES,
    DEFAULT_OAUTH_REDIRECT_URIS,
    VALID_API_SCOPES,
    ApiKeyEntry,
    get_api_server_config,
    get_oauth_data_dir,
    normalize_api_scopes,
)

logger = logging.getLogger(__name__)

PROTECTED_RESOURCE_PATHS: frozenset[str] = frozenset(
    {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/api/mcp",
        "/.well-known/oauth-protected-resource/api/v1",
    }
)
AUTH_SERVER_METADATA_PATHS: frozenset[str] = frozenset(
    {
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/api/mcp",
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/api/mcp",
    }
)
OAUTH_ENDPOINT_PATHS: frozenset[str] = frozenset(
    {
        "/api/oauth/authorize",
        "/api/oauth/token",
        "/api/oauth/revoke",
        "/api/oauth/register",
    }
)

_SAFE_SEGMENT_RE = re.compile(r"\A[\w-]{1,64}\Z", re.ASCII)
_SAFE_HOST_RE = re.compile(r"\A[A-Za-z0-9.\-:\[\]]{1,255}\Z", re.ASCII)
_PKCE_CHALLENGE_RE = re.compile(r"\A[A-Za-z0-9\-._~]{43,128}\Z", re.ASCII)
_PKCE_VERIFIER_RE = re.compile(r"\A[A-Za-z0-9\-._~]{43,128}\Z", re.ASCII)

_FORBIDDEN_SCHEMES: frozenset[str] = frozenset(
    {"javascript", "data", "vbscript", "file", "blob", "about", "ws", "wss", "ftp"}
)
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
_SUPPORTED_AUTH_METHODS: frozenset[str] = frozenset({"none", "client_secret_post", "client_secret_basic"})
_SUPPORTED_GRANT_TYPES: frozenset[str] = frozenset({"authorization_code", "refresh_token"})

MAX_CIMD_BYTES = 64 * 1024  # 64 KiB
CIMD_TIMEOUT_SECONDS = 5.0
CIMD_CACHE_TTL_SECONDS = 600.0
CIMD_CACHE_MAX_ENTRIES = 256
CSRF_TOKEN_TTL_SECONDS = 600
MAX_DCR_CLIENTS = 1024
MAX_REVOKED_JTIS = 4096


class OAuthError(Exception):
    """Structured OAuth 2.1 protocol error."""

    def __init__(
        self,
        error: str,
        description: str = "",
        *,
        status_code: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status_code = status_code
        self.headers = dict(headers or {})

    def to_dict(self) -> dict[str, str]:
        out = {"error": self.error}
        if self.description:
            out["error_description"] = self.description
        return out


@dataclass(frozen=True)
class OAuthClient:
    """Resolved OAuth 2.1 client metadata (pre-registered, DCR, or CIMD)."""

    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str = "none"
    client_secret: str | None = None
    client_secret_sha256: str | None = None
    secret_optional: bool = False
    grant_types: tuple[str, ...] = ("authorization_code", "refresh_token")
    response_types: tuple[str, ...] = ("code",)
    max_scopes: frozenset[str] = VALID_API_SCOPES
    source: str = "static"

    @property
    def is_confidential(self) -> bool:
        return bool(
            self.client_secret
            or self.client_secret_sha256
            or self.token_endpoint_auth_method in ("client_secret_post", "client_secret_basic")
        )

    def verify_secret(self, candidate: str | None) -> bool:
        """Verify `candidate` client secret in constant time."""
        if not self.is_confidential:
            return True
        if not candidate:
            return bool(self.secret_optional)
        cand_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        if self.client_secret is not None:
            expected_hash = hashlib.sha256(self.client_secret.encode("utf-8")).hexdigest()
            return hmac.compare_digest(cand_hash, expected_hash)
        if self.client_secret_sha256 is not None:
            return hmac.compare_digest(cand_hash, self.client_secret_sha256)
        return False


def is_oauth_public_path(path: str) -> bool:
    """Return True if `path` is an OAuth discovery or OAuth flow endpoint."""
    cleaned = (path or "").rstrip("/") or "/"
    if cleaned in PROTECTED_RESOURCE_PATHS or cleaned in AUTH_SERVER_METADATA_PATHS or cleaned in OAUTH_ENDPOINT_PATHS:
        return True
    return cleaned.startswith("/.well-known/oauth-") or cleaned.startswith("/api/oauth/")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes | None:
    if not isinstance(text, str) or not text:
        return None
    pad = "=" * ((4 - len(text) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((text + pad).encode("ascii"))
    except (ValueError, TypeError):
        return None


def compute_pkce_s256(code_verifier: str) -> str:
    """Return `BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))` per RFC 7636."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return _b64url_encode(digest)


def validate_pkce_challenge(code_challenge: str | None, code_challenge_method: str | None) -> tuple[str, str]:
    """Validate PKCE `S256` challenge parameters; raise `OAuthError` when missing, `plain`, or malformed."""
    method = str(code_challenge_method or "").strip()
    if method != "S256":
        raise OAuthError(
            "invalid_request",
            "PKCE with code_challenge_method=S256 is required by OAuth 2.1.",
            status_code=400,
        )
    challenge = str(code_challenge or "").strip()
    if not _PKCE_CHALLENGE_RE.match(challenge):
        raise OAuthError(
            "invalid_request",
            "Invalid or missing code_challenge (must be 43-128 URL-safe base64 characters).",
            status_code=400,
        )
    return challenge, "S256"


def verify_pkce_verifier(code_verifier: str | None, expected_challenge: str) -> bool:
    """Verify `code_verifier` against `expected_challenge` (`S256`) in constant time."""
    verifier = str(code_verifier or "").strip()
    if not _PKCE_VERIFIER_RE.match(verifier):
        return False
    computed = compute_pkce_s256(verifier)
    return hmac.compare_digest(computed, expected_challenge)


def is_loopback_host(hostname: str | None) -> bool:
    """Return True when `hostname` is an RFC 8252 loopback address (`localhost`, `127.0.0.1`, `::1`)."""
    return str(hostname or "").strip().lower().strip("[]") in _LOOPBACK_HOSTS


def is_loopback_redirect_uri(uri: str) -> bool:
    """Return True when `uri` targets a loopback host."""
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    return is_loopback_host(parsed.hostname)


def is_syntactically_safe_redirect_uri(uri: str) -> bool:
    """Validate that `uri` is a safe absolute redirect URI (rejecting dangerous schemes and non-loopback HTTP)."""
    if not isinstance(uri, str):
        return False
    cleaned = uri.strip()
    if not cleaned or len(cleaned) > 2048 or cleaned != uri:
        return False
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F or ch == "\\" for ch in cleaned):
        return False
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        _ = parsed.port
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    if not scheme or not re.match(r"\A[a-z][a-z0-9+.\-]*\Z", scheme):
        return False
    if scheme in _FORBIDDEN_SCHEMES:
        return False
    if parsed.fragment:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if ".." in (parsed.path or "").split("/"):
        return False
    host = (parsed.hostname or "").lower()
    if scheme == "http":
        return host in _LOOPBACK_HOSTS
    if scheme == "https":
        return bool(host)
    # Custom native app URI scheme (e.g. cursor://, vscode://)
    return bool(host or parsed.path)


def is_allowed_redirect_uri(uri: str, allowed_patterns: Iterable[str] | None = None) -> bool:
    """Return True when `uri` is safe and matches at least one entry in `allowed_patterns`."""
    if not is_syntactically_safe_redirect_uri(uri):
        return False
    patterns = list(allowed_patterns if allowed_patterns is not None else DEFAULT_OAUTH_REDIRECT_URIS)
    parsed = urllib.parse.urlsplit(uri)
    uri_scheme = parsed.scheme.lower()
    uri_host = (parsed.hostname or "").lower()
    uri_port = parsed.port
    uri_path = parsed.path or "/"

    for raw_pat in patterns:
        pat_str = str(raw_pat or "").strip()
        if not pat_str:
            continue
        if pat_str == uri:
            return True
        if pat_str.endswith("://*"):
            pat_scheme = pat_str[:-4].lower()
            if pat_scheme == uri_scheme and pat_scheme not in _FORBIDDEN_SCHEMES and pat_scheme not in ("http", "https"):
                return True
            continue
        try:
            pat = urllib.parse.urlsplit(pat_str)
            pat_port = pat.port
        except ValueError:
            continue
        if pat.scheme.lower() != uri_scheme:
            continue
        pat_host = (pat.hostname or "").lower()
        if not pat_host or not uri_host:
            continue
        if "*" in pat_host:
            if not fnmatch.fnmatchcase(uri_host, pat_host):
                continue
        elif uri_host != pat_host:
            continue

        # Port matching: RFC 8252 loopback allows any port when the pattern does not specify one
        if uri_scheme == "http" and uri_host in _LOOPBACK_HOSTS:
            if pat_port is not None and uri_port != pat_port:
                continue
        else:
            if uri_port != pat_port:
                continue

        pat_path = pat.path or "/"
        if pat_path == "/*":
            return True
        if "*" in pat_path:
            if fnmatch.fnmatchcase(uri_path, pat_path):
                return True
        elif uri_path == pat_path and (not pat.query or parsed.query == pat.query):
            return True

    return False


def matches_client_redirect_uri(requested_uri: str, registered_uris: Iterable[str]) -> bool:
    """Verify `requested_uri` against a client's `registered_uris` (exact match or RFC 8252 loopback port match)."""
    if not is_syntactically_safe_redirect_uri(requested_uri):
        return False
    req = urllib.parse.urlsplit(requested_uri)
    req_scheme = req.scheme.lower()
    req_host = (req.hostname or "").lower()
    req_path = req.path or "/"

    for reg_raw in registered_uris or ():
        reg_str = str(reg_raw or "").strip()
        if not reg_str:
            continue
        if requested_uri == reg_str:
            return True
        if "*" in reg_str and is_allowed_redirect_uri(requested_uri, [reg_str]):
            return True
        try:
            reg = urllib.parse.urlsplit(reg_str)
        except ValueError:
            continue
        if (
            req_scheme == "http"
            and reg.scheme.lower() == "http"
            and req_host in _LOOPBACK_HOSTS
            and (reg.hostname or "").lower() == req_host
        ):
            reg_path = reg.path or "/"
            if reg_path == "/*" or ("*" in reg_path and fnmatch.fnmatchcase(req_path, reg_path)):
                return True
            if req_path == reg_path and req.query == reg.query:
                return True
    return False


def resolve_issuer(
    scope: dict[str, Any] | None = None,
    *,
    oauth_cfg: dict[str, Any] | None = None,
    trusted_proxies: Iterable[str] | None = None,
) -> str:
    """Resolve the canonical OAuth issuer URL (`api.oauth.issuer` or derived from request headers)."""
    cfg = get_api_server_config()
    oauth = oauth_cfg if oauth_cfg is not None else cfg.get("oauth", {})
    configured = str(oauth.get("issuer") or "").strip().rstrip("/")
    if configured:
        return configured

    if scope is None:
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 8001)
        return f"http://{host}:{port}"

    from api.security import is_trusted_ip, parse_trusted_proxies

    raw_proxies = list(trusted_proxies if trusted_proxies is not None else cfg.get("trusted_proxies", ()))
    nets = parse_trusted_proxies(raw_proxies)
    client = scope.get("client")
    peer_ip = str(client[0]).strip() if client and client[0] else ""
    peer_trusted = is_trusted_ip(peer_ip, nets) or (
        peer_ip == "testclient" and ("testclient" in raw_proxies or "127.0.0.1" in raw_proxies)
    )

    headers: dict[bytes, str] = {}
    for k, v in scope.get("headers", ()):
        headers.setdefault(k.lower(), v.decode("latin-1", errors="replace").strip())

    scheme = str(scope.get("scheme") or "http").lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    host_val = headers.get(b"host", "")

    if peer_trusted:
        fwd_proto = headers.get(b"x-forwarded-proto", "")
        if fwd_proto:
            first_proto = fwd_proto.split(",", 1)[0].strip().lower()
            if first_proto in ("http", "https"):
                scheme = first_proto
        fwd_host = headers.get(b"x-forwarded-host", "")
        if fwd_host:
            first_host = fwd_host.split(",", 1)[0].strip()
            if _SAFE_HOST_RE.match(first_host):
                host_val = first_host

    if not _SAFE_HOST_RE.match(host_val):
        server = scope.get("server")
        if server and server[0]:
            srv_host = str(server[0])
            srv_port = int(server[1]) if len(server) > 1 and server[1] else 80
            if (scheme == "http" and srv_port == 80) or (scheme == "https" and srv_port == 443):
                host_val = srv_host
            else:
                host_val = f"{srv_host}:{srv_port}"
        else:
            host_val = f"{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8001)}"

    return f"{scheme}://{host_val}".rstrip("/")


def allowed_resources_for_issuer(issuer: str) -> tuple[str, str, str]:
    """Return canonical RFC 8707 resource indicators valid for `issuer`."""
    base = issuer.rstrip("/")
    return (base, f"{base}/api/mcp", f"{base}/api/v1")


def validate_resource_indicator(resource: str | None, issuer: str) -> str:
    """Validate optional RFC 8707 `resource` parameter against `issuer`; raise `OAuthError('invalid_target')` on mismatch."""
    base = issuer.rstrip("/")
    if resource is None or not str(resource).strip():
        return f"{base}/api/mcp"
    cleaned = str(resource).strip().rstrip("/")
    allowed = allowed_resources_for_issuer(base)
    if cleaned not in allowed:
        raise OAuthError(
            "invalid_target",
            f"Requested resource {cleaned!r} does not match this server ({base}/api/mcp).",
            status_code=400,
        )
    return cleaned


def supported_scopes_list(allow_writes: bool | None = None) -> list[str]:
    """Return advertised `scopes_supported`, filtering write scopes when `api.allow_writes` is False."""
    if allow_writes is None:
        allow_writes = bool(get_api_server_config().get("allow_writes", False))
    if allow_writes:
        return ["read", "nodemap:write", "write"]
    return ["read"]


def parse_scope_string(raw_scope: str | None, *, allow_empty_default: bool = True) -> frozenset[str]:
    """Parse space- or comma-separated OAuth scope string and validate against `VALID_API_SCOPES`."""
    if raw_scope is None or not str(raw_scope).strip():
        return DEFAULT_API_SCOPES if allow_empty_default else frozenset()
    tokens = [tok.strip() for tok in re.split(r"[\s,]+", str(raw_scope).strip()) if tok.strip()]
    if not tokens:
        return DEFAULT_API_SCOPES if allow_empty_default else frozenset()
    invalid = [t for t in tokens if t not in VALID_API_SCOPES]
    if invalid:
        raise OAuthError(
            "invalid_scope",
            f"Unsupported scope(s): {', '.join(invalid)}. Supported: read, nodemap:write, write.",
            status_code=400,
        )
    return frozenset(tokens)


def intersect_effective_scopes(
    requested_scopes: Iterable[str],
    key_scopes: Iterable[str],
    client_max_scopes: Iterable[str] = VALID_API_SCOPES,
    *,
    allow_writes: bool = False,
) -> frozenset[str]:
    """Compute effective granted scopes: `requested ∩ key.scopes ∩ client.max_scopes` gated by `allow_writes`."""
    req = frozenset(requested_scopes) or DEFAULT_API_SCOPES
    key_sc = frozenset(key_scopes)
    cli_sc = frozenset(client_max_scopes)
    granted = set(req & key_sc & cli_sc)
    if not allow_writes:
        granted.discard("nodemap:write")
        granted.discard("write")
    if not granted and "read" in key_sc and "read" in cli_sc:
        granted.add("read")
    return frozenset(granted)


def build_www_authenticate_header(
    issuer: str,
    *,
    error: str | None = None,
    error_description: str | None = None,
    scope: str | None = None,
) -> str:
    """Build an RFC 9728 `WWW-Authenticate: Bearer ...` challenge header."""
    meta_url = f"{issuer.rstrip('/')}/.well-known/oauth-protected-resource/api/mcp"
    parts: list[str] = []
    if error:
        parts.append(f'error="{error}"')
    if error_description:
        safe_desc = error_description.replace('"', "'")
        parts.append(f'error_description="{safe_desc}"')
    if scope:
        parts.append(f'scope="{scope}"')
    parts.append(f'resource_metadata="{meta_url}"')
    return "Bearer " + ", ".join(parts)


# ------------------------------------------------------------------ SSRF-hardened CIMD Fetcher


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject all HTTP redirects during CIMD fetches to prevent redirect-based SSRF."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise OAuthError(
            "invalid_client",
            f"CIMD metadata fetch refused HTTP {code} redirect.",
            status_code=400,
        )


def is_public_unicast_ip(ip_str: str) -> bool:
    """Return True only when `ip_str` is a globally routable public unicast IPv4 or IPv6 address."""
    try:
        addr = ipaddress.ip_address(str(ip_str).strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    ):
        return False
    return True


def resolve_and_validate_cimd_host(hostname: str, port: int = 443) -> list[str]:
    """Resolve `hostname` via DNS and verify every resolved IP is a public unicast address."""
    host = str(hostname or "").strip().lower().strip("[]")
    if not host or host in _LOOPBACK_HOSTS or host.endswith((".localhost", ".local", ".internal", ".arpa")):
        raise OAuthError(
            "invalid_client",
            "CIMD client_id hostname is disallowed (loopback or internal domain).",
            status_code=400,
        )
    # Direct IP literal check first
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not is_public_unicast_ip(host):
            raise OAuthError(
                "invalid_client",
                "CIMD client_id IP address is non-public or disallowed.",
                status_code=400,
            )
        return [host]

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OAuthError(
            "invalid_client",
            f"Unable to resolve CIMD client_id host {host!r}: {exc}",
            status_code=400,
        ) from exc

    resolved_ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_str = str(sockaddr[0])
        if not is_public_unicast_ip(ip_str):
            raise OAuthError(
                "invalid_client",
                f"CIMD client_id host {host!r} resolved to disallowed non-public IP {ip_str}.",
                status_code=400,
            )
        if ip_str not in resolved_ips:
            resolved_ips.append(ip_str)

    if not resolved_ips:
        raise OAuthError("invalid_client", f"CIMD client_id host {host!r} returned no usable addresses.", status_code=400)
    return resolved_ips


def is_cimd_client_id(client_id: str) -> bool:
    """Return True when `client_id` looks like a URL (candidate for CIMD resolution)."""
    cleaned = str(client_id or "").strip()
    return cleaned.startswith(("https://", "http://"))


def fetch_cimd_document(client_id: str) -> dict[str, Any]:
    """Fetch and decode a JSON Client ID Metadata Document with strict SSRF protections."""
    cleaned = str(client_id or "").strip()
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port or 443
    except ValueError as exc:
        raise OAuthError("invalid_client", f"Malformed CIMD client_id URL: {exc}", status_code=400) from exc

    if parsed.scheme.lower() != "https":
        raise OAuthError("invalid_client", "CIMD client_id URL must use https://.", status_code=400)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise OAuthError("invalid_client", "CIMD client_id URL must not contain credentials or fragments.", status_code=400)
    if not parsed.hostname:
        raise OAuthError("invalid_client", "CIMD client_id URL is missing a hostname.", status_code=400)
    if not parsed.path or parsed.path == "/":
        raise OAuthError("invalid_client", "CIMD client_id URL must have a non-empty path.", status_code=400)

    resolve_and_validate_cimd_host(parsed.hostname, port)

    opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(
        cleaned,
        headers={
            "Accept": "application/json",
            "User-Agent": "KernelInfo-Parser-OAuth/1.0",
        },
        method="GET",
    )
    try:
        with opener.open(req, timeout=CIMD_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise OAuthError("invalid_client", f"CIMD fetch returned HTTP {status}.", status_code=400)
            raw = resp.read(MAX_CIMD_BYTES + 1)
    except OAuthError:
        raise
    except Exception as exc:
        raise OAuthError("invalid_client", f"Failed to fetch CIMD metadata document: {exc}", status_code=400) from exc

    if len(raw) > MAX_CIMD_BYTES:
        raise OAuthError("invalid_client", "CIMD metadata document exceeds 64 KiB limit.", status_code=400)

    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError("invalid_client", f"CIMD metadata document is not valid JSON: {exc}", status_code=400) from exc

    if not isinstance(doc, dict):
        raise OAuthError("invalid_client", "CIMD metadata document must be a JSON object.", status_code=400)
    return doc


# ------------------------------------------------------------------ Atomic JSON Store & OAuth Manager


def validate_safe_segment(value: str, label: str = "id") -> str:
    """Validate file identifier segment against `/^[\\w-]{1,64}$/` to prevent path traversal."""
    cleaned = str(value or "").strip()
    if not _SAFE_SEGMENT_RE.match(cleaned) or ".." in cleaned:
        raise OAuthError("invalid_request", f"Invalid {label}: must match /^[\\w-]{{1,64}}$/.")
    return cleaned


class OAuthManager:
    """Self-contained OAuth 2.1 state manager with atomic JSON file persistence."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._guard = threading.RLock()
        self._file_locks: dict[str, threading.RLock] = {}
        self._clock = clock
        self._cached_secret: tuple[Path, bytes] | None = None
        self._cimd_cache: OrderedDict[str, tuple[float, OAuthClient]] = OrderedDict()
        self._dcr_ip_hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._revoked_jtis: OrderedDict[str, float] = OrderedDict()

    def _lock_for(self, key: str) -> threading.RLock:
        with self._guard:
            lock = self._file_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._file_locks[key] = lock
            return lock

    def _base_dir(self) -> Path:
        return get_oauth_data_dir()

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _write_json_atomic(self, path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            try:
                os.chmod(tmp_path, mode)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            try:
                os.chmod(path, mode)
            except OSError:
                pass
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    # --- Signing Secret ---

    def get_signing_secret(self, oauth_cfg: dict[str, Any] | None = None) -> bytes:
        """Return the HMAC-SHA256 signing key, auto-generating and persisting `<oauth_data_dir>/signing_secret.json` (0600) if unset."""
        cfg = oauth_cfg if oauth_cfg is not None else get_api_server_config().get("oauth", {})
        configured = str(cfg.get("signing_secret") or "").strip()
        if configured:
            return configured.encode("utf-8")

        base_dir = self._base_dir()
        secret_path = (base_dir / "signing_secret.json").resolve()
        with self._lock_for("signing_secret"):
            if self._cached_secret is not None and self._cached_secret[0] == secret_path and secret_path.is_file():
                return self._cached_secret[1]

            existing = self._read_json(secret_path)
            if existing and isinstance(existing.get("signing_secret"), str) and existing["signing_secret"].strip():
                secret_bytes = existing["signing_secret"].strip().encode("utf-8")
                try:
                    os.chmod(secret_path, 0o600)
                except OSError:
                    pass
                self._cached_secret = (secret_path, secret_bytes)
                return secret_bytes

            generated = secrets.token_urlsafe(32)
            self._write_json_atomic(
                secret_path,
                {
                    "signing_secret": generated,
                    "created_at": int(self._clock()),
                },
                mode=0o600,
            )
            secret_bytes = generated.encode("utf-8")
            self._cached_secret = (secret_path, secret_bytes)
            return secret_bytes

    # --- Dynamic Client Registration (RFC 7591) & Client Resolution ---

    def check_dcr_rate_limit(self, client_ip: str, max_requests: int = 20, window_seconds: int = 60) -> int | None:
        """Enforce per-IP sliding-window rate limit on `POST /api/oauth/register`."""
        now = self._clock()
        cutoff = now - window_seconds
        with self._guard:
            hits = self._dcr_ip_hits.get(client_ip)
            if hits is None:
                while len(self._dcr_ip_hits) >= 1024:
                    self._dcr_ip_hits.popitem(last=False)
                hits = deque()
                self._dcr_ip_hits[client_ip] = hits
            else:
                self._dcr_ip_hits.move_to_end(client_ip)
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= max_requests:
                return max(1, int(hits[0] + window_seconds - now) + 1)
            hits.append(now)
            return None

    def _clients_dir(self) -> Path:
        base = self._base_dir().resolve()
        target = (base / "clients").resolve()
        if target.parent != base:
            raise OAuthError("server_error", "Invalid OAuth clients directory.", status_code=500)
        return target

    def _dcr_client_path(self, client_id: str) -> Path:
        safe_id = validate_safe_segment(client_id, "client_id")
        cdir = self._clients_dir()
        target = (cdir / f"{safe_id}.json").resolve()
        if target.parent != cdir:
            raise OAuthError("invalid_client", "Invalid client_id path.")
        return target

    def register_dcr_client(self, metadata: Any, oauth_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate and persist an RFC 7591 Dynamic Client Registration request."""
        cfg = oauth_cfg if oauth_cfg is not None else get_api_server_config().get("oauth", {})
        if not cfg.get("allow_dcr", True):
            raise OAuthError(
                "registration_not_supported",
                "Dynamic client registration is disabled on this server.",
                status_code=403,
            )
        if not isinstance(metadata, dict):
            raise OAuthError("invalid_client_metadata", "Client registration payload must be a JSON object.")

        raw_redirects = metadata.get("redirect_uris")
        if not isinstance(raw_redirects, list) or not raw_redirects:
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be a non-empty list of URI strings.")

        allowed_patterns = cfg.get("allowed_redirect_uris") or list(DEFAULT_OAUTH_REDIRECT_URIS)
        redirect_uris: list[str] = []
        for item in raw_redirects:
            if not isinstance(item, str) or not item.strip():
                raise OAuthError("invalid_redirect_uri", "Every redirect_uri must be a non-empty string.")
            uri = item.strip()
            if not is_allowed_redirect_uri(uri, allowed_patterns):
                raise OAuthError(
                    "invalid_redirect_uri",
                    f"Redirect URI {uri!r} is not permitted by server policy (api.oauth.allowed_redirect_uris).",
                )
            if uri not in redirect_uris:
                redirect_uris.append(uri)

        raw_auth_method = metadata.get("token_endpoint_auth_method")
        auth_method_omitted = raw_auth_method is None or not str(raw_auth_method).strip()
        auth_method = "client_secret_post" if auth_method_omitted else str(raw_auth_method).strip()
        if auth_method not in _SUPPORTED_AUTH_METHODS:
            raise OAuthError(
                "invalid_client_metadata",
                f"Unsupported token_endpoint_auth_method {auth_method!r}; supported: none, client_secret_post, client_secret_basic.",
            )

        raw_grants = metadata.get("grant_types")
        if raw_grants is None:
            grant_types = ["authorization_code", "refresh_token"]
        elif isinstance(raw_grants, list) and raw_grants:
            grant_types = [str(g).strip() for g in raw_grants if isinstance(g, str) and str(g).strip()]
            if not grant_types or any(g not in _SUPPORTED_GRANT_TYPES for g in grant_types) or "authorization_code" not in grant_types:
                raise OAuthError(
                    "invalid_client_metadata",
                    "grant_types must be a subset of ['authorization_code', 'refresh_token'] and include 'authorization_code'.",
                )
        else:
            raise OAuthError("invalid_client_metadata", "grant_types must be a non-empty list.")

        raw_resp_types = metadata.get("response_types")
        if raw_resp_types is None:
            response_types = ["code"]
        elif isinstance(raw_resp_types, list) and raw_resp_types == ["code"]:
            response_types = ["code"]
        else:
            raise OAuthError("invalid_client_metadata", "response_types must be ['code'].")

        client_name = str(metadata.get("client_name") or "MCP OAuth Client").strip()[:256] or "MCP OAuth Client"
        raw_scope = metadata.get("scope")
        if isinstance(raw_scope, str) and raw_scope.strip():
            tokens = [tok.strip() for tok in re.split(r"[\s,]+", raw_scope.strip()) if tok.strip()]
            valid_tokens = [t for t in tokens if t in VALID_API_SCOPES]
            max_scopes = frozenset(valid_tokens) if valid_tokens else VALID_API_SCOPES
        else:
            max_scopes = VALID_API_SCOPES

        client_id = f"dcr_{secrets.token_urlsafe(24)}"
        issued_at = int(self._clock())
        client_secret: str | None = None
        client_secret_sha256: str | None = None
        if auth_method in ("client_secret_post", "client_secret_basic"):
            client_secret = secrets.token_urlsafe(32)
            client_secret_sha256 = hashlib.sha256(client_secret.encode("utf-8")).hexdigest()

        record: dict[str, Any] = {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": auth_method,
            "secret_optional": auth_method_omitted,
            "grant_types": grant_types,
            "response_types": response_types,
            "max_scopes": sorted(max_scopes),
            "client_id_issued_at": issued_at,
        }
        if client_secret_sha256:
            record["client_secret_sha256"] = client_secret_sha256

        path = self._dcr_client_path(client_id)
        with self._lock_for(f"client:{client_id}"):
            self._write_json_atomic(path, record, mode=0o600)

        response: dict[str, Any] = {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": auth_method,
            "grant_types": grant_types,
            "response_types": response_types,
            "scope": " ".join(sorted(max_scopes)),
            "client_id_issued_at": issued_at,
        }
        if client_secret is not None:
            response["client_secret"] = client_secret
            response["client_secret_expires_at"] = 0
        return response

    def _load_dcr_client(self, client_id: str) -> OAuthClient | None:
        if not _SAFE_SEGMENT_RE.match(client_id):
            return None
        path = self._dcr_client_path(client_id)
        with self._lock_for(f"client:{client_id}"):
            data = self._read_json(path)
        if not data or data.get("client_id") != client_id:
            return None
        uris = data.get("redirect_uris")
        if not isinstance(uris, list) or not uris:
            return None
        scopes = normalize_api_scopes(data.get("max_scopes")) or VALID_API_SCOPES
        return OAuthClient(
            client_id=client_id,
            client_name=str(data.get("client_name") or client_id),
            redirect_uris=tuple(str(u) for u in uris if isinstance(u, str)),
            token_endpoint_auth_method=str(data.get("token_endpoint_auth_method") or "none"),
            client_secret_sha256=data.get("client_secret_sha256"),
            secret_optional=bool(data.get("secret_optional", False)),
            grant_types=tuple(data.get("grant_types") or ("authorization_code", "refresh_token")),
            response_types=tuple(data.get("response_types") or ("code",)),
            max_scopes=scopes,
            source="dcr",
        )

    def _resolve_cimd_client(self, client_id: str, oauth_cfg: dict[str, Any]) -> OAuthClient:
        if not oauth_cfg.get("allow_cimd", True):
            raise OAuthError("invalid_client", "Client ID Metadata Documents (CIMD) are disabled.", status_code=400)

        now = self._clock()
        with self._guard:
            cached = self._cimd_cache.get(client_id)
            if cached is not None:
                exp, client_obj = cached
                if now < exp:
                    self._cimd_cache.move_to_end(client_id)
                    return client_obj
                self._cimd_cache.pop(client_id, None)

        doc = fetch_cimd_document(client_id)
        if doc.get("client_id") != client_id:
            raise OAuthError(
                "invalid_client",
                "CIMD document client_id does not match the requested client_id URL.",
                status_code=400,
            )

        auth_method = str(doc.get("token_endpoint_auth_method") or "none").strip()
        if auth_method != "none" or "client_secret" in doc:
            raise OAuthError(
                "invalid_client",
                "CIMD metadata documents must use token_endpoint_auth_method='none' without a shared secret.",
                status_code=400,
            )

        raw_uris = doc.get("redirect_uris")
        if not isinstance(raw_uris, list) or not raw_uris:
            raise OAuthError("invalid_client", "CIMD metadata document must contain a non-empty redirect_uris list.")

        allowed_patterns = oauth_cfg.get("allowed_redirect_uris") or list(DEFAULT_OAUTH_REDIRECT_URIS)
        validated_uris: list[str] = []
        for u in raw_uris:
            if not isinstance(u, str) or not u.strip():
                raise OAuthError("invalid_client", "CIMD redirect_uris entries must be non-empty strings.")
            uri = u.strip()
            if not is_allowed_redirect_uri(uri, allowed_patterns):
                raise OAuthError(
                    "invalid_client",
                    f"CIMD redirect_uri {uri!r} is not permitted by api.oauth.allowed_redirect_uris.",
                )
            validated_uris.append(uri)

        client_name = str(doc.get("client_name") or urllib.parse.urlsplit(client_id).hostname or client_id).strip()[:256]
        client_obj = OAuthClient(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=tuple(validated_uris),
            token_endpoint_auth_method="none",
            max_scopes=VALID_API_SCOPES,
            source="cimd",
        )
        with self._guard:
            while len(self._cimd_cache) >= CIMD_CACHE_MAX_ENTRIES:
                self._cimd_cache.popitem(last=False)
            self._cimd_cache[client_id] = (now + CIMD_CACHE_TTL_SECONDS, client_obj)
        return client_obj

    def resolve_client(self, client_id: str | None, oauth_cfg: dict[str, Any] | None = None) -> OAuthClient:
        """Resolve `client_id` from pre-registered config, DCR storage, or CIMD HTTPS URL."""
        cleaned = str(client_id or "").strip()
        if not cleaned:
            raise OAuthError("invalid_client", "Missing client_id.", status_code=400)

        cfg = oauth_cfg if oauth_cfg is not None else get_api_server_config().get("oauth", {})

        # 1. Pre-registered static clients in api.oauth.clients
        for item in cfg.get("normalized_clients") or ():
            if item["client_id"] == cleaned:
                return OAuthClient(
                    client_id=item["client_id"],
                    client_name=item.get("name") or item["client_id"],
                    redirect_uris=tuple(item["redirect_uris"]),
                    token_endpoint_auth_method=item.get("token_endpoint_auth_method", "none"),
                    client_secret=item.get("client_secret") or None,
                    max_scopes=item.get("max_scopes") or VALID_API_SCOPES,
                    source="static",
                )

        # 2. Client ID Metadata Document (CIMD) when client_id is a URL
        if is_cimd_client_id(cleaned):
            return self._resolve_cimd_client(cleaned, cfg)

        # 3. Dynamically registered client (DCR) on disk
        dcr_client = self._load_dcr_client(cleaned)
        if dcr_client is not None:
            return dcr_client

        # 4. Ad-hoc client_id entered manually in a supplier UI (e.g. Gemini / ChatGPT custom connector form)
        # when DCR is enabled: permitted only for redirect URIs matching api.oauth.allowed_redirect_uris.
        if cfg.get("allow_dcr", True) and _SAFE_SEGMENT_RE.match(cleaned):
            allowed_patterns = cfg.get("allowed_redirect_uris") or list(DEFAULT_OAUTH_REDIRECT_URIS)
            return OAuthClient(
                client_id=cleaned,
                client_name=cleaned,
                redirect_uris=tuple(allowed_patterns),
                token_endpoint_auth_method="none",
                max_scopes=VALID_API_SCOPES,
                source="adhoc",
            )

        raise OAuthError("invalid_client", f"Unknown OAuth client_id: {cleaned[:128]!r}.", status_code=400)

    # --- CSRF Token for Consent Form ---

    def create_csrf_token(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        state: str,
        scope: str,
        resource: str,
        oauth_cfg: dict[str, Any] | None = None,
    ) -> str:
        """Create a stateless HMAC-SHA256 CSRF token bound to the exact authorization request parameters."""
        exp = int(self._clock()) + CSRF_TOKEN_TTL_SECONDS
        nonce = secrets.token_urlsafe(12)
        payload = {
            "cid": client_id,
            "uri": redirect_uri,
            "cc": code_challenge,
            "ccm": code_challenge_method,
            "st": state,
            "sc": scope,
            "res": resource,
            "exp": exp,
            "n": nonce,
        }
        payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        secret = self.get_signing_secret(oauth_cfg)
        sig = hmac.new(secret, f"csrf.{payload_b64}".encode("ascii"), hashlib.sha256).digest()
        return f"{payload_b64}.{_b64url_encode(sig)}"

    def verify_csrf_token(
        self,
        token: str | None,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        state: str,
        scope: str,
        resource: str,
        oauth_cfg: dict[str, Any] | None = None,
    ) -> bool:
        """Verify a consent form CSRF token in constant time."""
        if not token or "." not in token:
            return False
        payload_b64, sig_b64 = token.split(".", 1)
        secret = self.get_signing_secret(oauth_cfg)
        expected_sig = _b64url_encode(
            hmac.new(secret, f"csrf.{payload_b64}".encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig_b64, expected_sig):
            return False
        raw = _b64url_decode(payload_b64)
        if raw is None:
            return False
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        if int(data.get("exp", 0)) < int(self._clock()):
            return False
        return (
            data.get("cid") == client_id
            and data.get("uri") == redirect_uri
            and data.get("cc") == code_challenge
            and data.get("ccm") == code_challenge_method
            and data.get("st") == state
            and data.get("sc") == scope
            and data.get("res") == resource
        )

    # --- Single-Use Authorization Codes ---

    def _auth_codes_path(self) -> Path:
        return (self._base_dir() / "auth_codes.json").resolve()

    def issue_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        key_id: str,
        scopes: Iterable[str],
        resource: str,
        ttl: int = 120,
    ) -> str:
        """Issue and atomically persist a single-use authorization code."""
        raw_code = secrets.token_urlsafe(32)
        code_hash = hashlib.sha256(raw_code.encode("utf-8")).hexdigest()
        code_id = secrets.token_urlsafe(16)
        now = int(self._clock())
        path = self._auth_codes_path()

        with self._lock_for("auth_codes"):
            store = self._read_json(path) or {}
            codes = store.get("codes") if isinstance(store.get("codes"), dict) else {}
            # Prune expired codes older than 5 minutes past expiration
            codes = {
                k: v
                for k, v in codes.items()
                if isinstance(v, dict) and int(v.get("expires_at", 0)) > now - 300
            }
            codes[code_hash] = {
                "code_id": code_id,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "key_id": key_id,
                "scopes": sorted(set(scopes)),
                "resource": resource,
                "issued_at": now,
                "expires_at": now + max(1, int(ttl)),
                "used": False,
            }
            self._write_json_atomic(path, {"codes": codes}, mode=0o600)
        return raw_code

    def consume_authorization_code(self, raw_code: str | None) -> dict[str, Any]:
        """Atomically consume a single-use authorization code; revoke dependent tokens on replay."""
        cleaned = str(raw_code or "").strip()
        if not cleaned:
            raise OAuthError("invalid_grant", "Missing authorization code.", status_code=400)
        code_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        now = int(self._clock())
        path = self._auth_codes_path()

        with self._lock_for("auth_codes"):
            store = self._read_json(path) or {}
            codes = store.get("codes") if isinstance(store.get("codes"), dict) else {}
            entry = codes.get(code_hash)
            if not isinstance(entry, dict):
                raise OAuthError("invalid_grant", "Invalid or unknown authorization code.", status_code=400)

            if entry.get("used"):
                code_id = entry.get("code_id")
                codes.pop(code_hash, None)
                self._write_json_atomic(path, {"codes": codes}, mode=0o600)
                if isinstance(code_id, str) and code_id:
                    self._revoke_refresh_tokens_by_code_id(code_id)
                raise OAuthError("invalid_grant", "Authorization code has already been used.", status_code=400)

            entry["used"] = True
            entry["used_at"] = now
            codes[code_hash] = entry
            self._write_json_atomic(path, {"codes": codes}, mode=0o600)

        if now >= int(entry.get("expires_at", 0)):
            raise OAuthError("invalid_grant", "Authorization code has expired.", status_code=400)
        return entry

    # --- Access Tokens (Stateless HMAC-SHA256) & Refresh Tokens ---

    def issue_access_token(
        self,
        *,
        issuer: str,
        sub: str,
        aud: str,
        client_id: str,
        scopes: Iterable[str],
        ttl: int = 3600,
        oauth_cfg: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Issue a compact HMAC-SHA256 signed access token (`typ: 'kip+at'`)."""
        now = int(self._clock())
        exp = now + max(1, int(ttl))
        jti = secrets.token_urlsafe(16)
        scope_str = " ".join(sorted(set(scopes)))
        header = {"alg": "HS256", "typ": "kip+at"}
        payload = {
            "iss": issuer.rstrip("/"),
            "sub": sub,
            "aud": aud.rstrip("/"),
            "client_id": client_id,
            "scope": scope_str,
            "iat": now,
            "exp": exp,
            "jti": jti,
        }
        header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        secret = self.get_signing_secret(oauth_cfg)
        sig_b64 = _b64url_encode(hmac.new(secret, signing_input, hashlib.sha256).digest())
        return f"{header_b64}.{payload_b64}.{sig_b64}", payload

    def verify_access_token(
        self,
        token: str | None,
        *,
        expected_issuer: str | None = None,
        allow_writes: bool = False,
        configured_keys: Iterable[ApiKeyEntry] = (),
        oauth_cfg: dict[str, Any] | None = None,
    ) -> tuple[str, frozenset[str], dict[str, Any]] | None:
        """Verify an HMAC-SHA256 OAuth access token and re-intersect scopes with the backing API key."""
        if not token or token.count(".") != 2:
            return None
        header_b64, payload_b64, sig_b64 = token.split(".")
        if not header_b64 or not payload_b64 or not sig_b64:
            return None

        secret = self.get_signing_secret(oauth_cfg)
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected_sig = _b64url_encode(hmac.new(secret, signing_input, hashlib.sha256).digest())
        if not hmac.compare_digest(sig_b64, expected_sig):
            return None

        raw_header = _b64url_decode(header_b64)
        raw_payload = _b64url_decode(payload_b64)
        if raw_header is None or raw_payload is None:
            return None
        try:
            header = json.loads(raw_header.decode("utf-8"))
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(header, dict) or not isinstance(payload, dict):
            return None
        if header.get("alg") != "HS256" or header.get("typ") != "kip+at":
            return None

        now = int(self._clock())
        exp = payload.get("exp")
        if not isinstance(exp, int) or now >= exp:
            return None

        jti = str(payload.get("jti") or "")
        if jti:
            with self._guard:
                if jti in self._revoked_jtis:
                    return None

        tok_iss = str(payload.get("iss") or "").rstrip("/")
        if not tok_iss:
            return None
        if expected_issuer and tok_iss != expected_issuer.rstrip("/"):
            return None

        tok_aud = str(payload.get("aud") or "").rstrip("/")
        if tok_aud not in allowed_resources_for_issuer(tok_iss):
            return None

        sub_key_id = str(payload.get("sub") or "").strip()
        client_id = str(payload.get("client_id") or "").strip()
        if not sub_key_id or not client_id:
            return None

        matched_key: ApiKeyEntry | None = None
        for entry in configured_keys:
            if isinstance(entry, ApiKeyEntry) and entry.id == sub_key_id:
                matched_key = entry
                break
        if matched_key is None:
            return None

        raw_scope = str(payload.get("scope") or "read")
        tok_scopes = frozenset(s for s in raw_scope.split() if s in VALID_API_SCOPES)
        effective_scopes = intersect_effective_scopes(
            tok_scopes,
            matched_key.scopes,
            VALID_API_SCOPES,
            allow_writes=allow_writes,
        )
        if not effective_scopes:
            return None

        actor_id = f"oauth:{sub_key_id}:{client_id}"
        return actor_id, effective_scopes, payload

    def _refresh_tokens_path(self) -> Path:
        return (self._base_dir() / "refresh_tokens.json").resolve()

    def _revoke_refresh_tokens_by_code_id(self, code_id: str) -> None:
        path = self._refresh_tokens_path()
        with self._lock_for("refresh_tokens"):
            store = self._read_json(path) or {}
            tokens = store.get("tokens") if isinstance(store.get("tokens"), dict) else {}
            filtered = {
                k: v
                for k, v in tokens.items()
                if isinstance(v, dict) and v.get("code_id") != code_id
            }
            if len(filtered) != len(tokens):
                self._write_json_atomic(path, {"tokens": filtered}, mode=0o600)

    def issue_refresh_token(
        self,
        *,
        client_id: str,
        key_id: str,
        scopes: Iterable[str],
        resource: str,
        code_id: str = "",
        ttl: int = 2592000,
    ) -> str:
        """Issue and atomically persist an opaque refresh token (stored as SHA-256 hash)."""
        raw_rt = f"kip_rt_{secrets.token_urlsafe(32)}"
        rt_hash = hashlib.sha256(raw_rt.encode("utf-8")).hexdigest()
        now = int(self._clock())
        path = self._refresh_tokens_path()

        with self._lock_for("refresh_tokens"):
            store = self._read_json(path) or {}
            tokens = store.get("tokens") if isinstance(store.get("tokens"), dict) else {}
            tokens = {
                k: v
                for k, v in tokens.items()
                if isinstance(v, dict) and int(v.get("expires_at", 0)) > now
            }
            tokens[rt_hash] = {
                "client_id": client_id,
                "key_id": key_id,
                "scopes": sorted(set(scopes)),
                "resource": resource,
                "code_id": code_id,
                "issued_at": now,
                "expires_at": now + max(1, int(ttl)),
            }
            self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)
        return raw_rt

    def rotate_refresh_token(
        self,
        raw_rt: str | None,
        *,
        client: OAuthClient,
        requested_scope: str | None,
        requested_resource: str | None,
        issuer: str,
        configured_keys: Iterable[ApiKeyEntry],
        allow_writes: bool,
        refresh_ttl: int = 2592000,
    ) -> tuple[str, ApiKeyEntry, frozenset[str], str]:
        """Verify and atomically rotate a refresh token, re-intersecting scopes with current key & `allow_writes`."""
        cleaned = str(raw_rt or "").strip()
        if not cleaned:
            raise OAuthError("invalid_grant", "Missing refresh_token.", status_code=400)
        rt_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        now = int(self._clock())
        path = self._refresh_tokens_path()

        with self._lock_for("refresh_tokens"):
            store = self._read_json(path) or {}
            tokens = store.get("tokens") if isinstance(store.get("tokens"), dict) else {}
            entry = tokens.get(rt_hash)
            if not isinstance(entry, dict):
                raise OAuthError("invalid_grant", "Invalid or unknown refresh token.", status_code=400)

            # Always remove the old refresh token on use (single-use rotation)
            tokens.pop(rt_hash, None)

            if now >= int(entry.get("expires_at", 0)):
                self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)
                raise OAuthError("invalid_grant", "Refresh token has expired.", status_code=400)

            if entry.get("client_id") != client.client_id:
                self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)
                raise OAuthError("invalid_grant", "Refresh token does not belong to this client_id.", status_code=400)

            key_id = str(entry.get("key_id") or "")
            matched_key: ApiKeyEntry | None = None
            for k in configured_keys:
                if isinstance(k, ApiKeyEntry) and k.id == key_id:
                    matched_key = k
                    break
            if matched_key is None:
                self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)
                raise OAuthError("invalid_grant", "Backing API key has been removed or revoked.", status_code=400)

            stored_scopes = frozenset(s for s in (entry.get("scopes") or ["read"]) if s in VALID_API_SCOPES)
            if requested_scope is not None and str(requested_scope).strip():
                req_scopes = parse_scope_string(requested_scope)
                if not req_scopes.issubset(stored_scopes):
                    self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)
                    raise OAuthError(
                        "invalid_scope",
                        "Requested refresh scope exceeds originally granted scopes.",
                        status_code=400,
                    )
                target_scopes = req_scopes
            else:
                target_scopes = stored_scopes

            effective_scopes = intersect_effective_scopes(
                target_scopes,
                matched_key.scopes,
                client.max_scopes,
                allow_writes=allow_writes,
            )
            if not effective_scopes:
                self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)
                raise OAuthError("invalid_scope", "No valid scopes remain for this refresh token.", status_code=400)

            bound_resource = str(entry.get("resource") or f"{issuer.rstrip('/')}/api/mcp")
            if requested_resource is not None and str(requested_resource).strip():
                validated_res = validate_resource_indicator(requested_resource, issuer)
                if validated_res != bound_resource:
                    self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)
                    raise OAuthError("invalid_target", "Requested resource does not match refresh token resource.")

            new_raw_rt = f"kip_rt_{secrets.token_urlsafe(32)}"
            new_rt_hash = hashlib.sha256(new_raw_rt.encode("utf-8")).hexdigest()
            tokens = {
                k: v
                for k, v in tokens.items()
                if isinstance(v, dict) and int(v.get("expires_at", 0)) > now
            }
            tokens[new_rt_hash] = {
                "client_id": client.client_id,
                "key_id": matched_key.id,
                "scopes": sorted(effective_scopes),
                "resource": bound_resource,
                "code_id": str(entry.get("code_id") or ""),
                "issued_at": now,
                "expires_at": now + max(1, int(refresh_ttl)),
            }
            self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)

        return new_raw_rt, matched_key, effective_scopes, bound_resource

    def revoke_token(
        self,
        raw_token: str | None,
        *,
        client_id: str | None = None,
        oauth_cfg: dict[str, Any] | None = None,
    ) -> None:
        """Idempotently revoke a refresh token (or signed access token JTI) per RFC 7009."""
        cleaned = str(raw_token or "").strip()
        if not cleaned:
            return

        # 1. If it's a signed access token, record its JTI in the in-memory revocation list
        if cleaned.count(".") == 2:
            _h, payload_b64, sig_b64 = cleaned.split(".")
            secret = self.get_signing_secret(oauth_cfg)
            expected_sig = _b64url_encode(
                hmac.new(secret, f"{_h}.{payload_b64}".encode("ascii"), hashlib.sha256).digest()
            )
            if hmac.compare_digest(sig_b64, expected_sig):
                raw_payload = _b64url_decode(payload_b64)
                if raw_payload:
                    try:
                        payload = json.loads(raw_payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = None
                    if isinstance(payload, dict):
                        if client_id and payload.get("client_id") != client_id:
                            return
                        jti = str(payload.get("jti") or "")
                        exp = int(payload.get("exp") or 0)
                        if jti:
                            with self._guard:
                                while len(self._revoked_jtis) >= MAX_REVOKED_JTIS:
                                    self._revoked_jtis.popitem(last=False)
                                self._revoked_jtis[jti] = float(exp)
            return

        # 2. Otherwise check refresh_tokens.json
        rt_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        path = self._refresh_tokens_path()
        with self._lock_for("refresh_tokens"):
            store = self._read_json(path)
            if not store or not isinstance(store.get("tokens"), dict):
                return
            tokens = store["tokens"]
            entry = tokens.get(rt_hash)
            if not isinstance(entry, dict):
                return
            if client_id and entry.get("client_id") != client_id:
                return
            tokens.pop(rt_hash, None)
            self._write_json_atomic(path, {"tokens": tokens}, mode=0o600)


# ------------------------------------------------------------------ Consent HTML Renderer

# Note: Do not set `form-action 'self'` here because CSP Level 3 enforces `form-action` across
# the entire 302 redirect chain of a POST form submission, which would cause Chrome/Safari to block
# the 302 redirect from `POST /api/oauth/authorize` to external OAuth callback URLs
# (e.g. `https://oauth-redirect.googleusercontent.com/...`, `https://claude.ai/...`, `http://localhost:...`).
CONSENT_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)


def render_consent_html(
    *,
    client: OAuthClient,
    redirect_uri: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str,
    scope: str,
    resource: str,
    csrf_token: str,
    error_message: str = "",
) -> str:
    """Render a self-contained, XSS-hardened HTML5 consent page with zero external assets."""
    parsed_uri = urllib.parse.urlsplit(redirect_uri)
    redirect_host = parsed_uri.hostname or f"{parsed_uri.scheme}://"
    is_loopback = is_loopback_redirect_uri(redirect_uri)

    esc_client_name = html.escape(client.client_name, quote=True)
    esc_client_id = html.escape(client.client_id, quote=True)
    esc_redirect_host = html.escape(redirect_host, quote=True)
    esc_redirect_uri = html.escape(redirect_uri, quote=True)
    esc_response_type = html.escape(response_type, quote=True)
    esc_code_challenge = html.escape(code_challenge, quote=True)
    esc_code_challenge_method = html.escape(code_challenge_method, quote=True)
    esc_state = html.escape(state, quote=True)
    esc_scope = html.escape(scope, quote=True)
    esc_resource = html.escape(resource, quote=True)
    esc_csrf = html.escape(csrf_token, quote=True)
    esc_error = html.escape(error_message, quote=True)

    scope_badges = "".join(
        f'<span class="badge">{html.escape(s, quote=True)}</span>'
        for s in (scope.split() or ["read"])
    )

    loopback_banner = ""
    if is_loopback:
        loopback_banner = (
            '<div class="banner warning" role="alert">'
            "<strong>Loopback Redirect Warning:</strong> This application redirects to a local loopback "
            f"address (<code>{esc_redirect_host}</code>). Only approve if you initiated this connection "
            "from an AI client or CLI tool running on your own machine."
            "</div>"
        )

    error_banner = ""
    if esc_error:
        error_banner = f'<div class="banner error" role="alert">{esc_error}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize MCP Access - KernelInfo-Parser</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #2f81f7;
    --success: #238636;
    --danger: #da3633;
    --warn-bg: #3b2e13;
    --warn-border: #d29922;
    --err-bg: #3d1619;
    --err-border: #f85149;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 2rem 1rem;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    justify-content: center;
    align-items: flex-start;
    min-height: 100vh;
  }}
  .card {{
    width: 100%;
    max-width: 540px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.75rem;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  }}
  h1 {{
    margin: 0 0 0.35rem 0;
    font-size: 1.3rem;
  }}
  .subtitle {{
    margin: 0 0 1.25rem 0;
    color: var(--muted);
    font-size: 0.9rem;
  }}
  .banner {{
    padding: 0.75rem 1rem;
    border-radius: 6px;
    margin-bottom: 1rem;
    font-size: 0.88rem;
    line-height: 1.4;
  }}
  .banner.warning {{
    background: var(--warn-bg);
    border: 1px solid var(--warn-border);
    color: #f0c674;
  }}
  .banner.error {{
    background: var(--err-bg);
    border: 1px solid var(--err-border);
    color: #ffa198;
  }}
  .meta {{
    background: #090d13;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.9rem 1rem;
    margin-bottom: 1.25rem;
    font-size: 0.88rem;
  }}
  .meta-row {{
    margin-bottom: 0.65rem;
    word-break: break-all;
  }}
  .meta-row:last-child {{ margin-bottom: 0; }}
  .meta-label {{
    display: block;
    color: var(--muted);
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 0.15rem;
  }}
  .host-highlight {{
    color: #79c0ff;
    font-weight: 600;
    font-size: 1rem;
  }}
  .badge {{
    display: inline-block;
    background: #1f2937;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.15rem 0.45rem;
    margin-right: 0.35rem;
    font-family: monospace;
    font-size: 0.82rem;
  }}
  label {{
    display: block;
    font-size: 0.88rem;
    font-weight: 600;
    margin-bottom: 0.4rem;
  }}
  input[type="password"] {{
    width: 100%;
    padding: 0.65rem 0.75rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-size: 0.95rem;
    margin-bottom: 1.25rem;
  }}
  input[type="password"]:focus {{
    outline: none;
    border-color: var(--accent);
  }}
  .actions {{
    display: flex;
    gap: 0.75rem;
  }}
  button {{
    flex: 1;
    padding: 0.65rem 1rem;
    border-radius: 6px;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
  }}
  button.approve {{
    background: var(--success);
    color: #fff;
  }}
  button.deny {{
    background: transparent;
    border-color: var(--border);
    color: var(--text);
  }}
</style>
</head>
<body>
<div class="card">
  <h1>Authorize AI / MCP Client</h1>
  <p class="subtitle">KernelInfo-Parser OAuth 2.1 Authorization Server</p>
  {loopback_banner}
  {error_banner}
  <div class="meta">
    <div class="meta-row">
      <span class="meta-label">Client Application</span>
      <strong>{esc_client_name}</strong><br>
      <code>{esc_client_id}</code>
    </div>
    <div class="meta-row">
      <span class="meta-label">Redirect Destination Host</span>
      <span class="host-highlight">{esc_redirect_host}</span><br>
      <code>{esc_redirect_uri}</code>
    </div>
    <div class="meta-row">
      <span class="meta-label">Target Resource</span>
      <code>{esc_resource}</code>
    </div>
    <div class="meta-row">
      <span class="meta-label">Requested Scopes</span>
      {scope_badges}
    </div>
  </div>
  <form method="post" action="/api/oauth/authorize" autocomplete="off">
    <input type="hidden" name="client_id" value="{esc_client_id}">
    <input type="hidden" name="redirect_uri" value="{esc_redirect_uri}">
    <input type="hidden" name="response_type" value="{esc_response_type}">
    <input type="hidden" name="code_challenge" value="{esc_code_challenge}">
    <input type="hidden" name="code_challenge_method" value="{esc_code_challenge_method}">
    <input type="hidden" name="state" value="{esc_state}">
    <input type="hidden" name="scope" value="{esc_scope}">
    <input type="hidden" name="resource" value="{esc_resource}">
    <input type="hidden" name="csrf_token" value="{esc_csrf}">
    <label for="api_key">Server API Key</label>
    <input type="password" id="api_key" name="api_key" placeholder="Enter your configured API key to authorize" autofocus>
    <div class="actions">
      <button type="submit" name="decision" value="approve" class="approve">Approve</button>
      <button type="submit" name="decision" value="deny" class="deny" formnovalidate>Deny</button>
    </div>
  </form>
</div>
</body>
</html>
"""


oauth_manager = OAuthManager()

__all__ = [
    "AUTH_SERVER_METADATA_PATHS",
    "CONSENT_CSP",
    "OAUTH_ENDPOINT_PATHS",
    "PROTECTED_RESOURCE_PATHS",
    "OAuthClient",
    "OAuthError",
    "OAuthManager",
    "allowed_resources_for_issuer",
    "build_www_authenticate_header",
    "compute_pkce_s256",
    "fetch_cimd_document",
    "intersect_effective_scopes",
    "is_allowed_redirect_uri",
    "is_cimd_client_id",
    "is_loopback_host",
    "is_loopback_redirect_uri",
    "is_oauth_public_path",
    "is_public_unicast_ip",
    "is_syntactically_safe_redirect_uri",
    "matches_client_redirect_uri",
    "oauth_manager",
    "parse_scope_string",
    "render_consent_html",
    "resolve_and_validate_cimd_host",
    "resolve_issuer",
    "supported_scopes_list",
    "validate_pkce_challenge",
    "validate_resource_indicator",
    "validate_safe_segment",
    "verify_pkce_verifier",
]
