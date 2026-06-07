"""Tests for the OAuth 2.1 + DCR provider module."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pep_oracle import oauth, oauth_store
from pep_oracle.oauth import (
    InvalidToken,
    mint_access_token,
    register_oauth_routes,
    verify_access_token,
)

SIGNING_KEY = "test-signing-key-at-least-32-bytes-long-for-hs256"
PUBLIC_URL = "https://test.example.com"


# --- helpers --------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@pytest.fixture
def client():
    app = FastAPI()
    store = oauth_store.SqliteStore(":memory:")
    register_oauth_routes(app, SIGNING_KEY, PUBLIC_URL, store)
    with TestClient(app) as c:
        yield c


def _register(client: TestClient, redirect_uri: str = "https://claude.ai/cb") -> str:
    r = client.post(
        "/oauth/register",
        json={
            "client_name": "Test",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["client_id"]


def _authorize(
    client: TestClient,
    client_id: str,
    challenge: str,
    redirect_uri: str = "https://claude.ai/cb",
    state: str | None = "xyz",
    method: str = "S256",
):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": method,
        "scope": "mcp",
    }
    if state is not None:
        params["state"] = state
    return client.get("/oauth/authorize", params=params, follow_redirects=False)


# --- 1. discovery ---------------------------------------------------------


def test_discovery_doc_shape(client):
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    doc = r.json()
    assert doc["issuer"] == PUBLIC_URL
    for key in (
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
        "response_types_supported",
        "grant_types_supported",
        "code_challenge_methods_supported",
        "token_endpoint_auth_methods_supported",
        "scopes_supported",
    ):
        assert key in doc, f"missing {key}"
    assert "S256" in doc["code_challenge_methods_supported"]
    assert "code" in doc["response_types_supported"]


# --- 2. DCR happy path ----------------------------------------------------


def test_dcr_happy_path(client):
    r = client.post(
        "/oauth/register",
        json={"client_name": "Claude", "redirect_uris": ["https://claude.ai/cb"]},
    )
    assert r.status_code in (200, 201)
    body = r.json()
    assert body.get("client_id")
    assert "client_secret" not in body
    assert body["redirect_uris"] == ["https://claude.ai/cb"]
    assert body["token_endpoint_auth_method"] == "none"


# --- 3. DCR rejects empty redirect_uris ----------------------------------


def test_dcr_rejects_empty_redirect_uris(client):
    r = client.post("/oauth/register", json={"client_name": "x", "redirect_uris": []})
    assert r.status_code == 400


def test_dcr_rejects_missing_redirect_uris(client):
    r = client.post("/oauth/register", json={"client_name": "x"})
    assert r.status_code == 400


# --- 4. end-to-end auth code + PKCE → token ------------------------------


def test_end_to_end_auth_code_pkce(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()

    r = _authorize(client, client_id, challenge)
    assert r.status_code == 302
    loc = r.headers["location"]
    parsed = urlparse(loc)
    qs = parse_qs(parsed.query)
    assert "code" in qs and qs["code"][0]
    assert qs["state"] == ["xyz"]

    code = qs["code"][0]
    r2 = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert r2.status_code == 200, r2.text
    tok = r2.json()
    assert tok["token_type"] == "Bearer"
    assert tok["expires_in"] == 3600
    assert tok["access_token"]
    assert tok["refresh_token"]
    assert tok["scope"] == "mcp"

    claims = verify_access_token(SIGNING_KEY, tok["access_token"], PUBLIC_URL)
    assert claims["sub"] == client_id
    assert claims["aud"] == "pep-oracle-mcp"
    assert claims["iss"] == PUBLIC_URL


def test_authorize_without_state_omits_state(client):
    client_id = _register(client)
    _, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge, state=None)
    assert r.status_code == 302
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "state" not in qs


# --- 5. authorize rejects wrong code_challenge_method --------------------


def test_authorize_rejects_plain_method(client):
    client_id = _register(client)
    _, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge, method="plain")
    assert r.status_code == 400


# --- 6. authorize rejects bad redirect_uri -------------------------------


def test_authorize_rejects_bad_redirect_uri(client):
    client_id = _register(client)
    _, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge, redirect_uri="https://evil.com/cb")
    assert r.status_code == 400


def test_authorize_rejects_unknown_client(client):
    _, challenge = _pkce_pair()
    r = _authorize(client, "nonexistent-client-id", challenge)
    assert r.status_code == 400


# --- 7. token rejects wrong code_verifier --------------------------------


def test_token_rejects_wrong_verifier(client):
    client_id = _register(client)
    _, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r2 = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/cb",
            "client_id": client_id,
            "code_verifier": "totally-wrong-verifier-" + secrets.token_urlsafe(32),
        },
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


# --- 8. token rejects expired code ---------------------------------------


def test_token_rejects_expired_code(client, monkeypatch):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]

    # Advance the store's clock past the 60s code TTL so pop_auth_code sees it expired.
    real = time.time()
    monkeypatch.setattr(oauth_store.time, "time", lambda: real + 120)

    r2 = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


# --- 9. token rejects reused code ----------------------------------------


def test_token_rejects_reused_code(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://claude.ai/cb",
        "client_id": client_id,
        "code_verifier": verifier,
    }
    r1 = client.post("/oauth/token", data=payload)
    assert r1.status_code == 200
    r2 = client.post("/oauth/token", data=payload)
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


# --- 10. refresh rotation -------------------------------------------------


def test_refresh_rotation(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok1 = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    ).json()

    r2 = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok1["refresh_token"],
            "client_id": client_id,
        },
    )
    assert r2.status_code == 200
    tok2 = r2.json()
    assert tok2["refresh_token"] != tok1["refresh_token"]
    assert tok2["access_token"]

    # First refresh is now revoked.
    r3 = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok1["refresh_token"],
            "client_id": client_id,
        },
    )
    assert r3.status_code == 400
    assert r3.json()["error"] == "invalid_grant"


# --- 11/12/13. JWT verify failures ---------------------------------------


def test_jwt_verify_rejects_tampered_token():
    tok = mint_access_token(SIGNING_KEY, "client-x", PUBLIC_URL)
    # Mutate one character in the signature segment.
    parts = tok.split(".")
    sig = list(parts[2])
    sig[0] = "A" if sig[0] != "A" else "B"
    parts[2] = "".join(sig)
    tampered = ".".join(parts)
    with pytest.raises(InvalidToken):
        verify_access_token(SIGNING_KEY, tampered, PUBLIC_URL)


def test_jwt_verify_rejects_expired_token():
    tok = mint_access_token(SIGNING_KEY, "client-x", PUBLIC_URL, ttl_seconds=-1)
    with pytest.raises(InvalidToken):
        verify_access_token(SIGNING_KEY, tok, PUBLIC_URL)


def test_jwt_verify_rejects_wrong_audience():
    tok = mint_access_token(SIGNING_KEY, "client-x", PUBLIC_URL, audience="other-aud")
    with pytest.raises(InvalidToken):
        verify_access_token(SIGNING_KEY, tok, PUBLIC_URL, audience="pep-oracle-mcp")


def test_jwt_verify_rejects_wrong_issuer():
    tok = mint_access_token(SIGNING_KEY, "client-x", "https://wrong.example.com")
    with pytest.raises(InvalidToken):
        verify_access_token(SIGNING_KEY, tok, PUBLIC_URL)


def test_jwt_verify_rejects_wrong_signing_key():
    tok = mint_access_token(SIGNING_KEY, "client-x", PUBLIC_URL)
    with pytest.raises(InvalidToken):
        verify_access_token("a-different-but-still-32-bytes-long-key-zzz", tok, PUBLIC_URL)


# --- 14. revoke endpoint --------------------------------------------------


def test_revoke_endpoint(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    ).json()

    r_rev = client.post(
        "/oauth/revoke",
        data={"token": tok["refresh_token"], "token_type_hint": "refresh_token"},
    )
    assert r_rev.status_code == 200

    r2 = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": client_id,
        },
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


def test_revoke_unknown_token_still_200(client):
    r = client.post("/oauth/revoke", data={"token": "does-not-exist"})
    assert r.status_code == 200


# --- extras: idempotent schema, unsupported grant type -------------------


def test_register_oauth_routes_idempotent_schema(tmp_path):
    """Constructing SqliteStore twice against the same file DB doesn't error."""
    db = tmp_path / "oauth.db"
    oauth_store.SqliteStore(str(db))
    oauth_store.SqliteStore(str(db))  # CREATE TABLE IF NOT EXISTS -> no error


def test_token_unsupported_grant_type(client):
    r = client.post("/oauth/token", data={"grant_type": "client_credentials"})
    assert r.status_code == 400


# --- DCR redirect_uri structural validation ------------------------------


def _register_raw(client: TestClient, redirect_uris: list[str]):
    return client.post(
        "/oauth/register",
        json={"client_name": "Test", "redirect_uris": redirect_uris},
    )


def test_dcr_rejects_http_non_loopback(client):
    r = _register_raw(client, ["http://example.com/cb"])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_dcr_accepts_https(client):
    r = _register_raw(client, ["https://example.com/cb"])
    assert r.status_code in (200, 201), r.text


def test_dcr_accepts_http_localhost(client):
    r = _register_raw(client, ["http://localhost:3000/cb"])
    assert r.status_code in (200, 201), r.text
    r2 = _register_raw(client, ["http://127.0.0.1:8080/cb"])
    assert r2.status_code in (200, 201), r2.text


def test_dcr_rejects_uri_with_fragment(client):
    r = _register_raw(client, ["https://example.com/cb#frag"])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_dcr_rejects_uri_with_userinfo(client):
    r = _register_raw(client, ["https://user:pw@example.com/cb"])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_dcr_rejects_relative_uri(client):
    r = _register_raw(client, ["/cb"])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_dcr_rejects_non_http_scheme(client):
    r = _register_raw(client, ["javascript:alert(1)"])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


# --- Refresh-token family revocation on reuse ----------------------------


def _bootstrap_refresh(client: TestClient) -> tuple[str, str]:
    """Register a client, run an authcode flow, return (client_id, refresh_token)."""
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    ).json()
    return client_id, tok["refresh_token"]


def _rotate(client: TestClient, client_id: str, refresh: str) -> dict:
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_refresh_reuse_revokes_family(client):
    client_id, refresh1 = _bootstrap_refresh(client)

    tok2 = _rotate(client, client_id, refresh1)
    refresh2 = tok2["refresh_token"]
    tok3 = _rotate(client, client_id, refresh2)
    refresh3 = tok3["refresh_token"]

    # Reuse refresh1 — should fail AND revoke the whole family.
    r_reuse = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh1,
            "client_id": client_id,
        },
    )
    assert r_reuse.status_code == 400
    assert r_reuse.json()["error"] == "invalid_grant"

    # refresh2 was already rotated (revoked) — using it now is also reuse, 400.
    r2 = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh2,
            "client_id": client_id,
        },
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"

    # refresh3 was the current live one — family revocation kills it too.
    r3 = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh3,
            "client_id": client_id,
        },
    )
    assert r3.status_code == 400
    assert r3.json()["error"] == "invalid_grant"


def test_authcode_grant_creates_new_family(client):
    # Family A: register + authcode flow, then start a chain.
    client_id_a, refresh_a1 = _bootstrap_refresh(client)
    tok_a2 = _rotate(client, client_id_a, refresh_a1)
    refresh_a2 = tok_a2["refresh_token"]

    # Family B: independent client + authcode flow.
    client_id_b, refresh_b1 = _bootstrap_refresh(client)

    # Kill family A via reuse of refresh_a1.
    r_reuse = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_a1,
            "client_id": client_id_a,
        },
    )
    assert r_reuse.status_code == 400

    # Family A live token (refresh_a2) is now dead.
    r_a2 = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_a2,
            "client_id": client_id_a,
        },
    )
    assert r_a2.status_code == 400

    # Family B is untouched — its refresh still rotates successfully.
    r_b = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_b1,
            "client_id": client_id_b,
        },
    )
    assert r_b.status_code == 200, r_b.text
    assert r_b.json()["refresh_token"] != refresh_b1


# --- Lost concurrent-rotation race must NOT revoke the family ------------


def test_refresh_lost_race_does_not_revoke_family(client, monkeypatch):
    """Losing the conditional rotation race (revoke_refresh -> False) on a token that
    was NOT revoked at read time must yield a clean 400 WITHOUT revoking the family —
    a benign concurrent double-submit must not log the user out."""
    # Obtain a valid (client_id, refresh_token) via the normal auth-code + PKCE flow.
    client_id, refresh_token = _bootstrap_refresh(client)

    # Simulate losing the race: the token reads back un-revoked (rec.revoked is False
    # because we got a valid refresh_token above), but the conditional revoke returns
    # False as if a concurrent rotation already revoked it between our read and write.
    # The family revoke must NOT be called.
    family_calls: list[str] = []
    monkeypatch.setattr(oauth_store.SqliteStore, "revoke_refresh", lambda self, token: False)
    monkeypatch.setattr(
        oauth_store.SqliteStore,
        "revoke_family",
        lambda self, family_id: family_calls.append(family_id),
    )

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    assert family_calls == [], "lost rotation race must NOT revoke the token family"


# --- Identity-gate authorize + callback (Phase 2b2) ----------------------

from pep_oracle.authorize_gate import IdentityError  # noqa: E402


class _FakeGate:
    """Stand-in for CognitoGate; no network. ``verify`` may return claims or raise."""

    def __init__(self, *, identity=True, verify=None):
        self._identity = identity
        self._verify = verify
        self.exchange_calls: list[tuple[str, str]] = []
        self.login_url = "https://cognito.example/oauth2/authorize"

    def requires_identity(self) -> bool:
        return self._identity

    def login_redirect(self, *, redirect_uri: str, login_state: str) -> str:
        from urllib.parse import urlencode
        return f"{self.login_url}?{urlencode({'redirect_uri': redirect_uri, 'state': login_state})}"

    def exchange_and_verify(self, *, code: str, redirect_uri: str) -> dict:
        self.exchange_calls.append((code, redirect_uri))
        if self._verify is None:
            return {"email": "me@example.com"}
        return self._verify(code, redirect_uri)


def _client_with_gate(gate):
    app = FastAPI()
    store = oauth_store.SqliteStore(":memory:")
    register_oauth_routes(app, SIGNING_KEY, PUBLIC_URL, store, gate)
    return TestClient(app), store


def test_authorize_with_identity_gate_redirects_to_idp():
    gate = _FakeGate(identity=True)
    client, store = _client_with_gate(gate)
    client_id = _register(client)
    _, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge, state="client-state-xyz")
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert loc.netloc == "cognito.example"
    qs = parse_qs(loc.query)
    assert qs["redirect_uri"] == [f"{PUBLIC_URL}/oauth/authorize/callback"]
    ls = oauth._decode_login_state(SIGNING_KEY, PUBLIC_URL, qs["state"][0])
    assert ls["mcp_client_id"] == client_id
    assert ls["mcp_redirect_uri"] == "https://claude.ai/cb"
    assert ls["mcp_code_challenge"] == challenge
    assert ls["mcp_state"] == "client-state-xyz"


def test_callback_issues_pep_code_after_successful_identity():
    gate = _FakeGate(identity=True)  # default verify -> allowed identity
    client, store = _client_with_gate(gate)
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    login_state = oauth._encode_login_state(
        SIGNING_KEY, PUBLIC_URL, client_id=client_id,
        redirect_uri="https://claude.ai/cb", code_challenge=challenge,
        client_state="client-state-xyz",
    )
    r = client.get(
        "/oauth/authorize/callback",
        params={"code": "cognito-code", "state": login_state},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert gate.exchange_calls == [("cognito-code", f"{PUBLIC_URL}/oauth/authorize/callback")]
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://claude.ai/cb"
    qs = parse_qs(loc.query)
    assert qs["state"] == ["client-state-xyz"]
    code = qs["code"][0]
    r2 = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://claude.ai/cb", "client_id": client_id,
        "code_verifier": verifier,
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["access_token"]


def test_callback_rejects_failed_identity():
    def deny(code, redirect_uri):
        raise IdentityError("nope")

    gate = _FakeGate(identity=True, verify=deny)
    client, store = _client_with_gate(gate)
    client_id = _register(client)
    _, challenge = _pkce_pair()
    login_state = oauth._encode_login_state(
        SIGNING_KEY, PUBLIC_URL, client_id=client_id,
        redirect_uri="https://claude.ai/cb", code_challenge=challenge, client_state=None,
    )
    r = client.get("/oauth/authorize/callback",
                   params={"code": "bad", "state": login_state}, follow_redirects=False)
    assert r.status_code == 403
    assert r.json()["error"] == "access_denied"


def test_callback_rejects_tampered_login_state():
    gate = _FakeGate(identity=True)
    client, _ = _client_with_gate(gate)
    r = client.get("/oauth/authorize/callback",
                   params={"code": "x", "state": "not-a-real-jwt"}, follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_callback_rejects_expired_login_state(monkeypatch):
    gate = _FakeGate(identity=True)
    client, _ = _client_with_gate(gate)
    client_id = _register(client)
    _, challenge = _pkce_pair()
    real = time.time()
    monkeypatch.setattr(oauth.time, "time", lambda: real - oauth.LOGIN_STATE_TTL_SECONDS - 10)
    login_state = oauth._encode_login_state(
        SIGNING_KEY, PUBLIC_URL, client_id=client_id,
        redirect_uri="https://claude.ai/cb", code_challenge=challenge, client_state=None,
    )
    monkeypatch.setattr(oauth.time, "time", lambda: real)
    r = client.get("/oauth/authorize/callback",
                   params={"code": "x", "state": login_state}, follow_redirects=False)
    assert r.status_code == 400


def test_callback_route_absent_for_trusted_upstream_gate(client):
    r = client.get("/oauth/authorize/callback", params={"code": "x", "state": "y"})
    assert r.status_code == 404
