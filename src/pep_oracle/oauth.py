"""OAuth 2.1 + Dynamic Client Registration provider for the pep-oracle MCP server.

SQLite-backed clients + refresh tokens, in-memory 60s auth codes, HS256 JWT
access tokens. ``register_oauth_routes`` wires endpoints onto a FastAPI app.
The bearer wrapper that gates ``/mcp`` imports :func:`verify_access_token` and
:class:`InvalidToken` from this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlencode

import jwt
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

DEFAULT_AUDIENCE = "pep-oracle-mcp"
ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600
AUTH_CODE_TTL_SECONDS = 60
SCOPE = "mcp"

# code -> {client_id, code_challenge, redirect_uri, expires_at}
_auth_codes: dict[str, dict[str, Any]] = {}


class InvalidToken(Exception):
    """Raised by :func:`verify_access_token` on any verification failure.

    Single opaque error — don't leak which check (sig/exp/aud/iss) failed.
    """


def _connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: the shared in-memory connection is used across
    # request worker threads; we serialize via a lock in _Store.
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
  client_id     TEXT PRIMARY KEY,
  client_name   TEXT,
  redirect_uris TEXT NOT NULL,
  created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
  token        TEXT PRIMARY KEY,
  client_id    TEXT NOT NULL,
  issued_at    INTEGER NOT NULL,
  expires_at   INTEGER NOT NULL,
  revoked      INTEGER NOT NULL DEFAULT 0
);
"""


class _Store:
    """SQLite handle. For ``:memory:`` we keep one shared connection (a fresh
    open would give a different DB) serialized by a lock. For file paths we
    open a new connection per request — cheap and thread-safe."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._shared: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        if db_path == ":memory:":
            self._shared = _connect(db_path)
            self._shared.executescript(_SCHEMA)
        else:
            conn = _connect(db_path)
            try:
                conn.executescript(_SCHEMA)
            finally:
                conn.close()

    def conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            self._lock.acquire()
            return self._shared
        return _connect(self.db_path)

    def close(self, conn: sqlite3.Connection) -> None:
        if conn is self._shared:
            self._lock.release()
        else:
            conn.close()


def _pop_auth_code(code: str) -> Optional[dict[str, Any]]:
    now = time.time()
    # Purge expired entries lazily.
    for c in [c for c, v in _auth_codes.items() if v["expires_at"] <= now]:
        _auth_codes.pop(c, None)
    entry = _auth_codes.pop(code, None)
    if entry is None or entry["expires_at"] <= now:
        return None
    return entry


def _pkce_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


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


def _persist_refresh(store: _Store, token: str, client_id: str) -> None:
    now = int(time.time())
    conn = store.conn()
    try:
        conn.execute(
            "INSERT INTO refresh_tokens (token, client_id, issued_at, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, 0)",
            (token, client_id, now, now + REFRESH_TTL_SECONDS),
        )
    finally:
        store.close(conn)


def _lookup_refresh(store: _Store, token: str) -> Optional[sqlite3.Row]:
    conn = store.conn()
    try:
        cur = conn.execute("SELECT * FROM refresh_tokens WHERE token = ?", (token,))
        return cur.fetchone()
    finally:
        store.close(conn)


def _revoke_refresh(store: _Store, token: str) -> None:
    conn = store.conn()
    try:
        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token = ?", (token,))
    finally:
        store.close(conn)


def _lookup_client(store: _Store, client_id: str) -> Optional[sqlite3.Row]:
    conn = store.conn()
    try:
        cur = conn.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,))
        return cur.fetchone()
    finally:
        store.close(conn)


def _persist_client(
    store: _Store, client_id: str, client_name: str, redirect_uris: list[str]
) -> int:
    now = int(time.time())
    conn = store.conn()
    try:
        conn.execute(
            "INSERT INTO clients (client_id, client_name, redirect_uris, created_at) "
            "VALUES (?, ?, ?, ?)",
            (client_id, client_name or "", json.dumps(redirect_uris), now),
        )
    finally:
        store.close(conn)
    return now


def _err(status: int, error: str, description: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(status_code=status, content=body)


def _issue_token_pair(
    store: _Store, signing_key: str, issuer: str, client_id: str
) -> dict[str, Any]:
    access = mint_access_token(signing_key, client_id, issuer=issuer)
    refresh = secrets.token_urlsafe(32)
    _persist_refresh(store, refresh, client_id)
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": refresh,
        "scope": SCOPE,
    }


def register_oauth_routes(
    app: FastAPI, signing_key: str, public_url: str, db_path: str
) -> None:
    """Register OAuth 2.1 + DCR endpoints on a FastAPI app.

    Routes: ``/.well-known/oauth-authorization-server``, ``/oauth/register``,
    ``/oauth/authorize``, ``/oauth/token``, ``/oauth/revoke``.
    """
    issuer = public_url.rstrip("/")
    store = _Store(db_path)

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
            return _err(400, "invalid_redirect_uri", "redirect_uris must be a non-empty list of strings")

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
        issued_at = _persist_client(store, client_id, client_name, redirect_uris)
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

        row = _lookup_client(store, client_id)
        if row is None:
            logger.warning("authorize rejected: unknown client_id=%s", client_id)
            return _err(400, "invalid_client", "unknown client_id")
        if redirect_uri not in json.loads(row["redirect_uris"]):
            logger.warning("authorize rejected: redirect_uri mismatch for client_id=%s", client_id)
            return _err(400, "invalid_request", "redirect_uri not registered")

        code = secrets.token_urlsafe(32)
        _auth_codes[code] = {
            "client_id": client_id,
            "code_challenge": code_challenge,
            "redirect_uri": redirect_uri,
            "expires_at": time.time() + AUTH_CODE_TTL_SECONDS,
        }
        logger.info("authorize: issued code for client_id=%s", client_id)

        params = {"code": code}
        if state is not None:
            params["state"] = state
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    @app.post("/oauth/token")
    async def token(
        grant_type: str = Form(...),
        code: Optional[str] = Form(None),
        redirect_uri: Optional[str] = Form(None),
        client_id: Optional[str] = Form(None),
        code_verifier: Optional[str] = Form(None),
        refresh_token: Optional[str] = Form(None),
    ) -> Response:
        if grant_type == "authorization_code":
            if not (code and redirect_uri and client_id and code_verifier):
                return _err(400, "invalid_request", "missing required fields")
            entry = _pop_auth_code(code)
            if entry is None:
                logger.warning("token: code missing/expired/used for client_id=%s", client_id)
                return _err(400, "invalid_grant", "code missing, expired, or already used")
            if entry["client_id"] != client_id or entry["redirect_uri"] != redirect_uri:
                logger.warning("token: code binding mismatch for client_id=%s", client_id)
                return _err(400, "invalid_grant", "client_id or redirect_uri mismatch")
            expected = _pkce_s256(code_verifier).encode("ascii")
            stored = entry["code_challenge"].encode("ascii")
            if not secrets.compare_digest(expected, stored):
                logger.warning("token: PKCE verifier mismatch for client_id=%s", client_id)
                return _err(400, "invalid_grant", "PKCE verification failed")
            logger.info("token: issued access+refresh for client_id=%s", client_id)
            return JSONResponse(_issue_token_pair(store, signing_key, issuer, client_id))

        if grant_type == "refresh_token":
            if not (refresh_token and client_id):
                return _err(400, "invalid_request", "missing required fields")
            row = _lookup_refresh(store, refresh_token)
            if row is None:
                logger.warning("refresh: unknown token for client_id=%s", client_id)
                return _err(400, "invalid_grant", "unknown refresh_token")
            if row["revoked"]:
                logger.warning("refresh: token revoked for client_id=%s", client_id)
                return _err(400, "invalid_grant", "refresh_token revoked")
            if row["expires_at"] <= int(time.time()):
                logger.warning("refresh: token expired for client_id=%s", client_id)
                return _err(400, "invalid_grant", "refresh_token expired")
            if row["client_id"] != client_id:
                logger.warning("refresh: client_id mismatch")
                return _err(400, "invalid_grant", "client_id mismatch")
            _revoke_refresh(store, refresh_token)
            logger.info("refresh: rotated refresh_token for client_id=%s", client_id)
            return JSONResponse(_issue_token_pair(store, signing_key, issuer, client_id))

        return _err(400, "unsupported_grant_type")

    @app.post("/oauth/revoke")
    async def revoke(
        token: str = Form(...),
        token_type_hint: Optional[str] = Form(None),
    ) -> Response:
        # RFC 7009: always 200, don't leak existence. Access tokens are
        # stateless JWTs and can't be revoked here.
        row = _lookup_refresh(store, token)
        if row is not None:
            _revoke_refresh(store, token)
            logger.info("revoke: marked refresh token revoked for client_id=%s", row["client_id"])
        return Response(status_code=200)

    logger.info("OAuth provider routes registered (issuer=%s)", issuer)
