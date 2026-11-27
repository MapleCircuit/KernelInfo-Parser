"""api/routers/oauth.py - FastAPI routes for OAuth 2.1 discovery, DCR, authorization consent, token, and revocation."""
from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

from api.config import get_api_server_config
from api.oauth import (
    CONSENT_CSP,
    OAuthClient,
    OAuthError,
    intersect_effective_scopes,
    matches_client_redirect_uri,
    oauth_manager,
    parse_scope_string,
    render_consent_html,
    resolve_issuer,
    supported_scopes_list,
    validate_pkce_challenge,
    validate_resource_indicator,
    verify_pkce_verifier,
)
from api.security import (
    check_ip_lockout,
    clear_ip_auth_failures,
    extract_client_ip,
    parse_trusted_proxies,
    record_ip_auth_failure,
    verify_api_key_constant_time,
)

router = APIRouter(include_in_schema=False)

_NO_STORE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _require_oauth_enabled() -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = get_api_server_config()
    oauth_cfg = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
    if not oauth_cfg.get("enabled", False):
        raise OAuthError("not_found", "Not Found", status_code=404)
    return cfg, oauth_cfg


def _oauth_error_response(exc: OAuthError, request: Request | None = None) -> JSONResponse:
    if exc.status_code == 404:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if request is not None:
        logger.warning(
            "OAuth %s %s -> %d (%s: %s)",
            request.method,
            request.url.path,
            exc.status_code,
            exc.error,
            exc.description,
        )
    headers = {**_NO_STORE_HEADERS, **exc.headers}
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict(), headers=headers)


def _client_ip_for_request(request: Request, cfg: dict[str, Any]) -> str:
    ip = request.scope.get("client_ip")
    if isinstance(ip, str) and ip:
        return ip
    nets = parse_trusted_proxies(cfg.get("trusted_proxies"))
    return extract_client_ip(request.scope, nets, cfg.get("client_ip_header", "X-Forwarded-For"))


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    merged = existing + [(k, v) for k, v in params.items() if v is not None]
    new_query = urllib.parse.urlencode(merged)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, ""))


async def _parse_form_or_json(request: Request) -> dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    raw = await request.body()
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    if "application/json" in content_type:
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    # Default: application/x-www-form-urlencoded (with JSON fallback if body starts with '{')
    if "application/x-www-form-urlencoded" in content_type or not text.lstrip().startswith("{"):
        pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
        return {k: v for k, v in pairs}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _extract_basic_auth(request: Request) -> tuple[str | None, str | None]:
    """Parse HTTP Basic authentication (`client_secret_basic`, RFC 6749 §2.3.1)."""
    auth = request.headers.get("authorization") or ""
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None, None
    try:
        decoded = base64.b64decode(parts[1].strip()).decode("utf-8")
    except Exception as exc:
        raise OAuthError("invalid_client", "Malformed HTTP Basic Authorization header.", status_code=401) from exc
    if ":" not in decoded:
        raise OAuthError("invalid_client", "Malformed HTTP Basic credentials.", status_code=401)
    raw_id, raw_secret = decoded.split(":", 1)
    return urllib.parse.unquote_plus(raw_id), urllib.parse.unquote_plus(raw_secret)


# ------------------------------------------------------------------ 1. RFC 9728 Protected Resource Metadata


def _protected_resource_payload(request: Request, resource_suffix: str = "/api/mcp") -> Response:
    try:
        cfg, oauth_cfg = _require_oauth_enabled()
    except OAuthError as exc:
        return _oauth_error_response(exc)
    issuer = resolve_issuer(request.scope, oauth_cfg=oauth_cfg)
    payload = {
        "resource": f"{issuer}{resource_suffix}",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": supported_scopes_list(cfg.get("allow_writes", False)),
    }
    return JSONResponse(status_code=200, content=payload)


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_root(request: Request) -> Response:
    return _protected_resource_payload(request, "/api/mcp")


@router.get("/.well-known/oauth-protected-resource/api/mcp")
async def oauth_protected_resource_mcp(request: Request) -> Response:
    return _protected_resource_payload(request, "/api/mcp")


@router.get("/.well-known/oauth-protected-resource/api/v1")
async def oauth_protected_resource_v1(request: Request) -> Response:
    return _protected_resource_payload(request, "/api/v1")


# ------------------------------------------------------------------ 2. RFC 8414 Auth Server Metadata & OIDC Discovery


def _auth_server_metadata_payload(request: Request) -> Response:
    try:
        cfg, oauth_cfg = _require_oauth_enabled()
    except OAuthError as exc:
        return _oauth_error_response(exc)
    issuer = resolve_issuer(request.scope, oauth_cfg=oauth_cfg)
    scopes = supported_scopes_list(cfg.get("allow_writes", False))
    payload: dict[str, Any] = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/api/oauth/authorize",
        "token_endpoint": f"{issuer}/api/oauth/token",
        "revocation_endpoint": f"{issuer}/api/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": scopes,
        "client_id_metadata_document_supported": bool(oauth_cfg.get("allow_cimd", True)),
    }
    if oauth_cfg.get("allow_dcr", True):
        payload["registration_endpoint"] = f"{issuer}/api/oauth/register"
    return JSONResponse(status_code=200, content=payload)


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/api/mcp")
@router.get("/.well-known/openid-configuration")
@router.get("/.well-known/openid-configuration/api/mcp")
async def oauth_authorization_server_metadata(request: Request) -> Response:
    return _auth_server_metadata_payload(request)


# ------------------------------------------------------------------ 3. RFC 7591 Dynamic Client Registration


@router.post("/api/oauth/register")
async def oauth_register_client(request: Request) -> Response:
    try:
        cfg, oauth_cfg = _require_oauth_enabled()
        client_ip = _client_ip_for_request(request, cfg)
        rl = cfg.get("rate_limit", {})
        retry_after = oauth_manager.check_dcr_rate_limit(
            client_ip,
            max_requests=max(1, int(rl.get("max_requests", 120))),
            window_seconds=max(1, int(rl.get("window_seconds", 60))),
        )
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"error": "slow_down", "error_description": "Too many client registration requests."},
                headers={**_NO_STORE_HEADERS, "Retry-After": str(retry_after)},
            )
        try:
            body = await request.json()
        except Exception as exc:
            raise OAuthError("invalid_client_metadata", f"Invalid JSON body: {exc}", status_code=400) from exc
        registered = oauth_manager.register_dcr_client(body, oauth_cfg=oauth_cfg)
        return JSONResponse(status_code=201, content=registered, headers=_NO_STORE_HEADERS)
    except OAuthError as exc:
        return _oauth_error_response(exc, request)


# ------------------------------------------------------------------ 4. Authorization & Consent Endpoint


def _validate_authorize_params(
    params: dict[str, Any],
    *,
    issuer: str,
    oauth_cfg: dict[str, Any],
) -> tuple[OAuthClient, str, str, str, str, frozenset[str], str]:
    response_type = str(params.get("response_type") or "").strip()
    if response_type != "code":
        raise OAuthError("unsupported_response_type", "response_type must be 'code'.", status_code=400)

    client_id = str(params.get("client_id") or "").strip()
    client = oauth_manager.resolve_client(client_id, oauth_cfg=oauth_cfg)

    redirect_uri = str(params.get("redirect_uri") or "").strip()
    if not redirect_uri:
        if len(client.redirect_uris) == 1 and "*" not in client.redirect_uris[0]:
            redirect_uri = client.redirect_uris[0]
        else:
            raise OAuthError("invalid_request", "redirect_uri is required.", status_code=400)
    if not matches_client_redirect_uri(redirect_uri, client.redirect_uris):
        raise OAuthError(
            "invalid_request",
            "redirect_uri does not match any registered redirect URI for this client.",
            status_code=400,
        )

    code_challenge, code_challenge_method = validate_pkce_challenge(
        params.get("code_challenge"),
        params.get("code_challenge_method"),
    )
    state = str(params.get("state") or "").strip()[:1024]
    requested_scopes = parse_scope_string(params.get("scope"))
    resource = validate_resource_indicator(params.get("resource"), issuer)
    return client, redirect_uri, code_challenge, code_challenge_method, state, requested_scopes, resource


def _consent_html_response(html_body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content=html_body,
        status_code=status_code,
        headers={
            **_NO_STORE_HEADERS,
            "Content-Security-Policy": CONSENT_CSP,
            "X-Frame-Options": "DENY",
        },
    )


@router.get("/api/oauth/authorize")
async def oauth_authorize_get(request: Request) -> Response:
    try:
        _cfg, oauth_cfg = _require_oauth_enabled()
        issuer = resolve_issuer(request.scope, oauth_cfg=oauth_cfg)
        params = dict(request.query_params)
        client, redirect_uri, code_challenge, code_challenge_method, state, requested_scopes, resource = (
            _validate_authorize_params(params, issuer=issuer, oauth_cfg=oauth_cfg)
        )
        scope_str = " ".join(sorted(requested_scopes))
        csrf_token = oauth_manager.create_csrf_token(
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            state=state,
            scope=scope_str,
            resource=resource,
            oauth_cfg=oauth_cfg,
        )
        body = render_consent_html(
            client=client,
            redirect_uri=redirect_uri,
            response_type="code",
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            state=state,
            scope=scope_str,
            resource=resource,
            csrf_token=csrf_token,
        )
        return _consent_html_response(body, status_code=200)
    except OAuthError as exc:
        return _oauth_error_response(exc, request)


@router.post("/api/oauth/authorize")
async def oauth_authorize_post(request: Request) -> Response:
    try:
        cfg, oauth_cfg = _require_oauth_enabled()
        issuer = resolve_issuer(request.scope, oauth_cfg=oauth_cfg)
        form = await _parse_form_or_json(request)
        client, redirect_uri, code_challenge, code_challenge_method, state, requested_scopes, resource = (
            _validate_authorize_params(form, issuer=issuer, oauth_cfg=oauth_cfg)
        )
        scope_str = " ".join(sorted(requested_scopes))
        csrf_token = str(form.get("csrf_token") or "")
        if not oauth_manager.verify_csrf_token(
            csrf_token,
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            state=state,
            scope=scope_str,
            resource=resource,
            oauth_cfg=oauth_cfg,
        ):
            raise OAuthError("access_denied", "Invalid or expired CSRF token.", status_code=403)

        decision = str(form.get("decision") or "approve").strip().lower()
        if decision == "deny":
            q: dict[str, str] = {"error": "access_denied", "error_description": "User denied authorization request."}
            if state:
                q["state"] = state
            return RedirectResponse(url=_append_query_params(redirect_uri, q), status_code=302, headers=_NO_STORE_HEADERS)

        client_ip = _client_ip_for_request(request, cfg)
        lockout_wait = check_ip_lockout(client_ip)
        if lockout_wait is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed authentication attempts. IP temporarily locked out."},
                headers={**_NO_STORE_HEADERS, "Retry-After": str(lockout_wait)},
            )

        submitted_key = str(form.get("api_key") or "").strip()
        configured_keys = cfg.get("normalized_keys", ())
        matched_key = verify_api_key_constant_time(submitted_key, configured_keys)
        if matched_key is None:
            record_ip_auth_failure(client_ip, cfg.get("auth_lockout", {}))
            fresh_csrf = oauth_manager.create_csrf_token(
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                state=state,
                scope=scope_str,
                resource=resource,
                oauth_cfg=oauth_cfg,
            )
            html_err = render_consent_html(
                client=client,
                redirect_uri=redirect_uri,
                response_type="code",
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                state=state,
                scope=scope_str,
                resource=resource,
                csrf_token=fresh_csrf,
                error_message="Invalid or missing API key.",
            )
            return _consent_html_response(html_err, status_code=401)

        clear_ip_auth_failures(client_ip)
        granted_scopes = intersect_effective_scopes(
            requested_scopes,
            matched_key.scopes,
            client.max_scopes,
            allow_writes=bool(cfg.get("allow_writes", False)),
        )
        code = oauth_manager.issue_authorization_code(
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            key_id=matched_key.id,
            scopes=granted_scopes,
            resource=resource,
            ttl=int(oauth_cfg.get("auth_code_ttl", 120)),
        )
        redirect_params: dict[str, str] = {"code": code}
        if state:
            redirect_params["state"] = state
        return RedirectResponse(
            url=_append_query_params(redirect_uri, redirect_params),
            status_code=302,
            headers=_NO_STORE_HEADERS,
        )
    except OAuthError as exc:
        return _oauth_error_response(exc, request)


# ------------------------------------------------------------------ 5. Token Endpoint (`POST /api/oauth/token`)


def _authenticate_token_client(
    request: Request,
    form: dict[str, Any],
    *,
    expected_client_id: str | None,
    oauth_cfg: dict[str, Any],
) -> OAuthClient:
    basic_id, basic_secret = _extract_basic_auth(request)
    form_id = str(form.get("client_id") or "").strip() or None
    form_secret = str(form.get("client_secret") or "").strip() or None

    if basic_id and form_id and basic_id != form_id:
        raise OAuthError("invalid_client", "Mismatched client_id between Basic auth and form body.", status_code=401)
    if basic_secret is not None and form_secret is not None:
        raise OAuthError("invalid_client", "Client credentials must not use multiple auth methods.", status_code=401)

    client_id = basic_id or form_id or expected_client_id
    if not client_id:
        raise OAuthError("invalid_client", "Missing client_id.", status_code=401)
    if expected_client_id and client_id != expected_client_id:
        raise OAuthError("invalid_grant", "client_id does not match the authorization grant.", status_code=400)

    client = oauth_manager.resolve_client(client_id, oauth_cfg=oauth_cfg)
    presented_secret = basic_secret if basic_secret is not None else form_secret
    if client.is_confidential:
        if not client.verify_secret(presented_secret):
            raise OAuthError(
                "invalid_client",
                "Client authentication failed.",
                status_code=401,
                headers={"WWW-Authenticate": "Basic"},
            )
    return client


@router.post("/api/oauth/token")
async def oauth_token(request: Request) -> Response:
    try:
        cfg, oauth_cfg = _require_oauth_enabled()
        issuer = resolve_issuer(request.scope, oauth_cfg=oauth_cfg)
        form = await _parse_form_or_json(request)
        grant_type = str(form.get("grant_type") or "").strip()
        configured_keys = cfg.get("normalized_keys", ())
        allow_writes = bool(cfg.get("allow_writes", False))
        access_ttl = int(oauth_cfg.get("access_token_ttl", 3600))
        refresh_ttl = int(oauth_cfg.get("refresh_token_ttl", 2592000))

        if grant_type == "authorization_code":
            code_entry = oauth_manager.consume_authorization_code(form.get("code"))
            client = _authenticate_token_client(
                request,
                form,
                expected_client_id=str(code_entry["client_id"]),
                oauth_cfg=oauth_cfg,
            )
            redirect_uri = str(form.get("redirect_uri") or "").strip()
            if not redirect_uri or redirect_uri != code_entry["redirect_uri"]:
                raise OAuthError("invalid_grant", "redirect_uri does not match authorization request.", status_code=400)

            if not verify_pkce_verifier(form.get("code_verifier"), str(code_entry["code_challenge"])):
                raise OAuthError("invalid_grant", "PKCE code_verifier verification failed.", status_code=400)

            bound_resource = str(code_entry.get("resource") or f"{issuer}/api/mcp")
            req_resource = form.get("resource")
            if req_resource is not None and str(req_resource).strip():
                validated_res = validate_resource_indicator(str(req_resource), issuer)
                if validated_res != bound_resource:
                    raise OAuthError("invalid_target", "Requested resource does not match authorization grant.")

            key_id = str(code_entry["key_id"])
            matched_key = next((k for k in configured_keys if k.id == key_id), None)
            if matched_key is None:
                raise OAuthError("invalid_grant", "Backing API key has been removed or revoked.", status_code=400)

            effective_scopes = intersect_effective_scopes(
                code_entry.get("scopes") or ["read"],
                matched_key.scopes,
                client.max_scopes,
                allow_writes=allow_writes,
            )
            if not effective_scopes:
                raise OAuthError("invalid_scope", "No valid scopes available for this grant.", status_code=400)

            access_token, _payload = oauth_manager.issue_access_token(
                issuer=issuer,
                sub=matched_key.id,
                aud=bound_resource,
                client_id=client.client_id,
                scopes=effective_scopes,
                ttl=access_ttl,
                oauth_cfg=oauth_cfg,
            )
            refresh_token = oauth_manager.issue_refresh_token(
                client_id=client.client_id,
                key_id=matched_key.id,
                scopes=effective_scopes,
                resource=bound_resource,
                code_id=str(code_entry.get("code_id") or ""),
                ttl=refresh_ttl,
            )
            return JSONResponse(
                status_code=200,
                content={
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "expires_in": access_ttl,
                    "refresh_token": refresh_token,
                    "scope": " ".join(sorted(effective_scopes)),
                },
                headers=_NO_STORE_HEADERS,
            )

        if grant_type == "refresh_token":
            client = _authenticate_token_client(request, form, expected_client_id=None, oauth_cfg=oauth_cfg)
            new_rt, matched_key, effective_scopes, bound_resource = oauth_manager.rotate_refresh_token(
                form.get("refresh_token"),
                client=client,
                requested_scope=form.get("scope"),
                requested_resource=form.get("resource"),
                issuer=issuer,
                configured_keys=configured_keys,
                allow_writes=allow_writes,
                refresh_ttl=refresh_ttl,
            )
            access_token, _payload = oauth_manager.issue_access_token(
                issuer=issuer,
                sub=matched_key.id,
                aud=bound_resource,
                client_id=client.client_id,
                scopes=effective_scopes,
                ttl=access_ttl,
                oauth_cfg=oauth_cfg,
            )
            return JSONResponse(
                status_code=200,
                content={
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "expires_in": access_ttl,
                    "refresh_token": new_rt,
                    "scope": " ".join(sorted(effective_scopes)),
                },
                headers=_NO_STORE_HEADERS,
            )

        raise OAuthError(
            "unsupported_grant_type",
            f"Unsupported grant_type {grant_type!r}; supported: authorization_code, refresh_token.",
            status_code=400,
        )
    except OAuthError as exc:
        return _oauth_error_response(exc, request)


# ------------------------------------------------------------------ 6. Token Revocation (`POST /api/oauth/revoke`, RFC 7009)


@router.post("/api/oauth/revoke")
async def oauth_revoke(request: Request) -> Response:
    try:
        _cfg, oauth_cfg = _require_oauth_enabled()
        form = await _parse_form_or_json(request)
        basic_id, basic_secret = _extract_basic_auth(request)
        client_id = basic_id or (str(form.get("client_id") or "").strip() or None)
        if client_id:
            client = oauth_manager.resolve_client(client_id, oauth_cfg=oauth_cfg)
            if client.is_confidential:
                secret = basic_secret if basic_secret is not None else str(form.get("client_secret") or "").strip()
                if not client.verify_secret(secret):
                    raise OAuthError(
                        "invalid_client",
                        "Client authentication failed.",
                        status_code=401,
                        headers={"WWW-Authenticate": "Basic"},
                    )
        oauth_manager.revoke_token(form.get("token"), client_id=client_id, oauth_cfg=oauth_cfg)
        return JSONResponse(status_code=200, content={}, headers=_NO_STORE_HEADERS)
    except OAuthError as exc:
        return _oauth_error_response(exc)
