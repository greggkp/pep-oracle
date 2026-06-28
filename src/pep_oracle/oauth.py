"""OAuth 2.1 + Dynamic Client Registration provider for the pep-oracle MCP server.

Store-backed clients + refresh tokens + single-use auth codes, HS256 JWT access
tokens. ``register_oauth_routes`` wires endpoints onto a FastAPI app.
The bearer wrapper that gates ``/mcp`` imports :func:`verify_access_token` and
:class:`InvalidToken` from this module.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from pep_oracle.authorize_gate import (
    CALLBACK_PATH,
    AuthorizeGate,
    IdentityError,
    TrustedUpstreamGate,
)
from pep_oracle.oauth_store import REFRESH_TTL_SECONDS, OAuthStore

logger = logging.getLogger(__name__)

DEFAULT_AUDIENCE = "pep-oracle-mcp"
ACCESS_TTL_SECONDS = 3600
AUTH_CODE_TTL_SECONDS = 60
SCOPE = "mcp"
LOGIN_STATE_AUDIENCE = "pep-oracle-login"
LOGIN_STATE_TTL_SECONDS = 600


class InvalidToken(Exception):
    """Raised by :func:`verify_access_token` on any verification failure.

    Single opaque error — don't leak which check (sig/exp/aud/iss) failed.
    """


def _pkce_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _validate_redirect_uri(uri: str) -> str | None:
    """Structural validation per OAuth 2.1 / RFC 8252.

    Returns an error message string on failure, None on success.
    Pure / no side effects.
    """
    try:
        parsed = urlparse(uri)
    except Exception:
        return f"redirect_uri is not parseable: {uri}"
    if not parsed.netloc:
        return f"redirect_uri must be absolute: {uri}"
    if parsed.fragment:
        return f"redirect_uri must not contain a fragment: {uri}"
    if parsed.username is not None:
        return f"redirect_uri must not contain userinfo: {uri}"
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return None
    if scheme == "http":
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            return None
        return f"redirect_uri http scheme requires loopback host: {uri}"
    return f"redirect_uri scheme must be https or http-loopback: {uri}"


def mint_access_token(
    signing_key: str,
    client_id: str,
    issuer: str,
    audience: str = DEFAULT_AUDIENCE,
    ttl_seconds: int = ACCESS_TTL_SECONDS,
) -> str:
    """Mint an HS256-signed JWT. ``ttl_seconds`` may be negative in tests."""
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": client_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "scope": SCOPE,
    }
    return jwt.encode(claims, signing_key, algorithm="HS256")


def verify_access_token(
    signing_key: str,
    token: str,
    issuer: str,
    audience: str = DEFAULT_AUDIENCE,
) -> dict[str, Any]:
    """Verify an HS256 JWT. Raises :class:`InvalidToken` on any failure."""
    try:
        return jwt.decode(
            token,
            signing_key,
            algorithms=["HS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except Exception as e:  # noqa: BLE001 — single opaque error per spec
        raise InvalidToken("invalid token") from e


def _encode_login_state(
    signing_key: str,
    issuer: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    client_state: str | None,
) -> str:
    """Short-lived HS256 JWT carrying the original MCP authorize params across the
    Cognito leg (stateless -- no store row). Verified on the callback."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": LOGIN_STATE_AUDIENCE,
        "iat": now,
        "exp": now + LOGIN_STATE_TTL_SECONDS,
        "mcp_client_id": client_id,
        "mcp_redirect_uri": redirect_uri,
        "mcp_code_challenge": code_challenge,
    }
    if client_state is not None:
        claims["mcp_state"] = client_state
    return jwt.encode(claims, signing_key, algorithm="HS256")


def _decode_login_state(signing_key: str, issuer: str, blob: str) -> dict[str, Any]:
    """Verify + decode a login-state JWT. Raises on bad signature / iss / aud / exp."""
    return jwt.decode(
        blob,
        signing_key,
        algorithms=["HS256"],
        audience=LOGIN_STATE_AUDIENCE,
        issuer=issuer,
        options={
            "require": [
                "exp",
                "iat",
                "iss",
                "aud",
                "mcp_client_id",
                "mcp_redirect_uri",
                "mcp_code_challenge",
            ]
        },
    )


def _err(status: int, error: str, description: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(status_code=status, content=body)


def _issue_token_pair(
    store: OAuthStore,
    signing_key: str,
    issuer: str,
    client_id: str,
    family_id: str | None = None,
) -> dict[str, Any]:
    access = mint_access_token(signing_key, client_id, issuer=issuer)
    refresh = secrets.token_urlsafe(32)
    if family_id is None:
        family_id = secrets.token_urlsafe(16)
    store.put_refresh(
        refresh, client_id=client_id, family_id=family_id, ttl_seconds=REFRESH_TTL_SECONDS
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": refresh,
        "scope": SCOPE,
    }


def register_oauth_routes(
    app: FastAPI,
    signing_key: str,
    public_url: str,
    store: OAuthStore,
    gate: AuthorizeGate | None = None,
) -> None:
    """Register OAuth 2.1 + DCR endpoints on a FastAPI app.

    Routes: ``/.well-known/oauth-authorization-server``, ``/oauth/register``,
    ``/oauth/authorize``, ``/oauth/token``, ``/oauth/revoke``.
    """
    issuer = public_url.rstrip("/")

    if gate is None:
        gate = TrustedUpstreamGate()

    def _issue_code_and_redirect(
        *, client_id: str, redirect_uri: str, code_challenge: str, client_state: str | None
    ) -> Response:
        code = secrets.token_urlsafe(32)
        store.put_auth_code(
            code,
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            ttl_seconds=AUTH_CODE_TTL_SECONDS,
        )
        logger.info("authorize: issued code for client_id=%s", client_id)
        params = {"code": code}
        if client_state is not None:
            params["state"] = client_state
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    @app.get("/.well-known/oauth-authorization-server")
    async def discovery() -> dict[str, Any]:
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register",
            "revocation_endpoint": f"{issuer}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
        }

    @app.post("/oauth/register")
    async def register(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _err(400, "invalid_client_metadata", "body must be JSON")
        if not isinstance(body, dict):
            return _err(400, "invalid_client_metadata", "body must be JSON object")
        redirect_uris = body.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or not all(isinstance(u, str) and u for u in redirect_uris)
        ):
            logger.warning("DCR rejected: missing/invalid redirect_uris")
            return _err(
                400, "invalid_redirect_uri", "redirect_uris must be a non-empty list of strings"
            )
        for u in redirect_uris:
            err = _validate_redirect_uri(u)
            if err is not None:
                logger.warning("DCR rejected: %s", err)
                return _err(400, "invalid_redirect_uri", err)

        gtypes = body.get("grant_types", ["authorization_code", "refresh_token"])
        rtypes = body.get("response_types", ["code"])
        auth_method = body.get("token_endpoint_auth_method", "none")
        if not isinstance(gtypes, list) or not isinstance(rtypes, list):
            return _err(400, "invalid_client_metadata", "grant_types/response_types must be lists")
        if auth_method not in ("none", "client_secret_basic", "client_secret_post"):
            return _err(400, "invalid_client_metadata", "unsupported token_endpoint_auth_method")
        client_name = body.get("client_name") or ""
        if not isinstance(client_name, str):
            return _err(400, "invalid_client_metadata", "client_name must be a string")

        client_id = str(uuid.uuid4())
        issued_at = store.put_client(client_id, client_name, redirect_uris)
        logger.info("DCR: registered client_id=%s name=%s", client_id, client_name or "<unnamed>")
        return JSONResponse(
            status_code=201,
            content={
                "client_id": client_id,
                "client_id_issued_at": issued_at,
                "redirect_uris": redirect_uris,
                "client_name": client_name,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )

    @app.get("/oauth/authorize")
    async def authorize(request: Request) -> Response:
        qp = request.query_params
        response_type = qp.get("response_type")
        client_id = qp.get("client_id")
        redirect_uri = qp.get("redirect_uri")
        code_challenge = qp.get("code_challenge")
        method = qp.get("code_challenge_method")
        state = qp.get("state")

        if response_type != "code":
            logger.warning("authorize rejected: response_type=%r", response_type)
            return _err(400, "unsupported_response_type")
        if not client_id:
            return _err(400, "invalid_request", "missing client_id")
        if not redirect_uri:
            return _err(400, "invalid_request", "missing redirect_uri")
        if method != "S256":
            logger.warning("authorize rejected: bad code_challenge_method=%r", method)
            return _err(400, "invalid_request", "code_challenge_method must be S256")
        if not code_challenge:
            return _err(400, "invalid_request", "missing code_challenge")

        client = store.get_client(client_id)
        if client is None:
            logger.warning("authorize rejected: unknown client_id=%s", client_id)
            return _err(400, "invalid_client", "unknown client_id")
        if redirect_uri not in client.redirect_uris:
            logger.warning("authorize rejected: redirect_uri mismatch for client_id=%s", client_id)
            return _err(400, "invalid_request", "redirect_uri not registered")

        if gate.requires_identity():
            login_state = _encode_login_state(
                signing_key,
                issuer,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                client_state=state,
            )
            callback_uri = f"{issuer}{CALLBACK_PATH}"
            logger.info("authorize: redirecting to identity provider for client_id=%s", client_id)
            return RedirectResponse(
                url=gate.login_redirect(redirect_uri=callback_uri, login_state=login_state),
                status_code=302,
            )
        return _issue_code_and_redirect(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            client_state=state,
        )

    @app.post("/oauth/token")
    async def token(
        grant_type: str = Form(...),
        code: str | None = Form(None),
        redirect_uri: str | None = Form(None),
        client_id: str | None = Form(None),
        code_verifier: str | None = Form(None),
        refresh_token: str | None = Form(None),
    ) -> Response:
        if grant_type == "authorization_code":
            if not (code and redirect_uri and client_id and code_verifier):
                return _err(400, "invalid_request", "missing required fields")
            entry = store.pop_auth_code(code)
            if entry is None:
                logger.warning("token: code missing/expired/used for client_id=%s", client_id)
                return _err(400, "invalid_grant", "code missing, expired, or already used")
            if entry.client_id != client_id or entry.redirect_uri != redirect_uri:
                logger.warning("token: code binding mismatch for client_id=%s", client_id)
                return _err(400, "invalid_grant", "client_id or redirect_uri mismatch")
            expected = _pkce_s256(code_verifier).encode("ascii")
            stored = entry.code_challenge.encode("ascii")
            if not secrets.compare_digest(expected, stored):
                logger.warning("token: PKCE verifier mismatch for client_id=%s", client_id)
                return _err(400, "invalid_grant", "PKCE verification failed")
            logger.info("token: issued access+refresh for client_id=%s", client_id)
            return JSONResponse(_issue_token_pair(store, signing_key, issuer, client_id))

        if grant_type == "refresh_token":
            if not (refresh_token and client_id):
                return _err(400, "invalid_request", "missing required fields")
            rec = store.get_refresh(refresh_token)
            if rec is None:
                logger.warning("refresh: unknown token for client_id=%s", client_id)
                return _err(400, "invalid_grant", "unknown refresh_token")
            if rec.revoked:
                # Token already revoked at read time = reuse of a rotated token
                # (RFC 9700 §4.13.2): possible compromise -> revoke the family.
                store.revoke_family(rec.family_id)
                logger.warning(
                    "Refresh token reuse detected — revoking family family_id=%s client_id=%s",
                    rec.family_id,
                    client_id,
                )
                return _err(400, "invalid_grant", "refresh_token revoked")
            if rec.expires_at <= int(time.time()):
                logger.warning("refresh: token expired for client_id=%s", client_id)
                return _err(400, "invalid_grant", "refresh_token expired")
            if rec.client_id != client_id:
                logger.warning("refresh: client_id mismatch")
                return _err(400, "invalid_grant", "client_id mismatch")
            if not store.revoke_refresh(refresh_token):
                # Lost a concurrent rotation race (another request revoked it between
                # our read and write). Benign — clean 400, do NOT revoke the family.
                logger.info("refresh: lost rotation race for client_id=%s", client_id)
                return _err(400, "invalid_grant", "refresh_token already rotated")
            logger.info("refresh: rotated refresh_token for client_id=%s", client_id)
            return JSONResponse(
                _issue_token_pair(store, signing_key, issuer, client_id, family_id=rec.family_id)
            )

        return _err(400, "unsupported_grant_type")

    @app.post("/oauth/revoke")
    async def revoke(
        token: str = Form(...),
        token_type_hint: str | None = Form(None),
    ) -> Response:
        # RFC 7009: always 200, don't leak existence. Access tokens are
        # stateless JWTs and can't be revoked here.
        rec = store.get_refresh(token)
        if rec is not None:
            store.revoke_refresh(token)
            logger.info("revoke: marked refresh token revoked for client_id=%s", rec.client_id)
        return Response(status_code=200)

    if gate.requires_identity():

        @app.get(CALLBACK_PATH)
        async def authorize_callback(request: Request) -> Response:
            qp = request.query_params
            cognito_code = qp.get("code")
            login_state = qp.get("state")
            if qp.get("error"):
                logger.warning("authorize callback: idp error=%s", qp.get("error"))
                return _err(400, "access_denied", "identity provider returned an error")
            if not cognito_code or not login_state:
                return _err(400, "invalid_request", "missing code or state")
            try:
                ls = _decode_login_state(signing_key, issuer, login_state)
            except Exception:
                logger.warning("authorize callback: bad/expired login_state")
                return _err(400, "invalid_request", "invalid login state")

            mcp_client_id = ls["mcp_client_id"]
            mcp_redirect_uri = ls["mcp_redirect_uri"]
            mcp_code_challenge = ls["mcp_code_challenge"]
            mcp_state = ls.get("mcp_state")

            # Re-validate the client binding (it may have been deleted mid-flow).
            client = store.get_client(mcp_client_id)
            if client is None or mcp_redirect_uri not in client.redirect_uris:
                logger.warning("authorize callback: client/redirect no longer valid")
                return _err(400, "invalid_request", "client or redirect_uri no longer valid")

            callback_uri = f"{issuer}{CALLBACK_PATH}"
            try:
                # This route is only registered when requires_identity() is True,
                # i.e. for CognitoGate, which is the only gate exposing
                # exchange_and_verify (kept off the minimal AuthorizeGate Protocol).
                gate.exchange_and_verify(code=cognito_code, redirect_uri=callback_uri)
            except IdentityError:
                logger.warning("authorize callback: identity verification failed")
                return _err(403, "access_denied", "identity verification failed")

            return _issue_code_and_redirect(
                client_id=mcp_client_id,
                redirect_uri=mcp_redirect_uri,
                code_challenge=mcp_code_challenge,
                client_state=mcp_state,
            )

    logger.info("OAuth provider routes registered (issuer=%s)", issuer)
