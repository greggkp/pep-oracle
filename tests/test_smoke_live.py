"""End-to-end smoke tests against the *running* pep-oracle server.

These exercise the real deployed process, real ChromaDB data, real RSS feed,
and (for /ask) the real Anthropic API — the layers that unit tests mock and
therefore can't protect. Every production failure we've hit (a stale ChromaDB
client, a 421 Host-header rejection on /mcp, and a speaker+recency filter that
dead-ended to "No relevant content found") would have been caught here.

Opt-in, never part of the default run:

    pytest tests/test_smoke_live.py -v -m live

Targets http://localhost:8000 by default; override with PEP_ORACLE_SMOKE_URL.
Skips cleanly if no server is reachable.
"""

import os
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.live

NO_CONTENT = "No relevant content found"
TIMEOUT = 90


def _base_url() -> str:
    return os.environ.get("PEP_ORACLE_SMOKE_URL", "http://localhost:8000").rstrip("/")


@pytest.fixture(scope="module")
def base_url() -> str:
    url = _base_url()
    try:
        r = requests.get(f"{url}/health", timeout=5)
        r.raise_for_status()
    except Exception as e:
        pytest.skip(f"No pep-oracle server reachable at {url}: {e}")
    return url


# --- /episodes: the UI's source of truth (regresses the "empty UI" symptom) ---


def test_episodes_listing_is_populated_and_current(base_url):
    r = requests.get(f"{base_url}/episodes", timeout=TIMEOUT)
    r.raise_for_status()
    episodes = r.json()["episodes"]
    assert episodes, "/episodes returned an empty list"
    assert any(e["ingested"] for e in episodes), "no episode reports as ingested"
    # The most recent episode by date must be present in the listing.
    newest = max(episodes, key=lambda e: e["date"])
    assert newest["date"], f"newest episode has no date: {newest}"


# --- /ask: must produce grounded answers, not dead-end (regresses the filter bug) ---


@pytest.mark.parametrize(
    "question",
    [
        # Plain topic — exercises basic retrieval.
        "What have Chas and Dave said about tariffs?",
        # Speaker + recency — the exact shape that dead-ended: the preprocessor
        # sets speaker='Chas' + an after_date floor, and recent episodes may
        # carry unmapped diarization labels. Must relax filters, not give up.
        "What does Chas say about Trump in the latest episode?",
    ],
)
def test_ask_returns_grounded_answer(base_url, question):
    r = requests.post(
        f"{base_url}/ask",
        json={"question": question, "top_k": 10},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    answer = r.json()["answer"]
    assert answer.strip(), "empty answer"
    assert NO_CONTENT not in answer, (
        f"/ask dead-ended for {question!r}:\n{answer}"
    )


# --- /mcp: auth gate works AND a valid token gets past it (regresses the 421) ---


def _signing_key() -> str:
    data_dir = Path(os.environ.get("PEP_ORACLE_DATA_DIR") or (Path.home() / ".pep-oracle"))
    key_path = data_dir.expanduser() / "oauth_signing_key"
    if not key_path.exists():
        pytest.skip(f"no OAuth signing key at {key_path} (MCP not enabled here)")
    return key_path.read_text().strip()


def _mcp_post(base_url, headers):
    # Minimal MCP initialize request. We only care whether the auth gate lets
    # the request reach the MCP app; the JSON-RPC result/handshake is the SDK's.
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "0"},
        },
    }
    base_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    base_headers.update(headers)
    return requests.post(f"{base_url}/mcp", json=body, headers=base_headers, timeout=TIMEOUT)


def test_mcp_rejects_request_without_token(base_url):
    if requests.get(f"{base_url}/.well-known/oauth-authorization-server", timeout=10).status_code != 200:
        pytest.skip("MCP/OAuth not enabled on this server")
    resp = _mcp_post(base_url, headers={})
    assert resp.status_code == 401, f"unauthenticated /mcp should be 401, got {resp.status_code}"


def test_mcp_accepts_valid_jwt(base_url):
    disco = requests.get(f"{base_url}/.well-known/oauth-authorization-server", timeout=10)
    if disco.status_code != 200:
        pytest.skip("MCP/OAuth not enabled on this server")
    issuer = disco.json()["issuer"]

    from pep_oracle.oauth import mint_access_token

    token = mint_access_token(_signing_key(), "smoke-test-client", issuer)
    resp = _mcp_post(base_url, headers={"Authorization": f"Bearer {token}"})
    # Any non-401 proves the bearer passed the auth gate (and that the Host
    # header was accepted — a 421 here would mean the TransportSecurity
    # allowlist regressed).
    assert resp.status_code != 401, "valid JWT was rejected by the /mcp auth gate"
    assert resp.status_code != 421, (
        "got 421 Misdirected Request — FastMCP rejected the Host header; "
        "the TransportSecurity allowed_hosts allowlist has regressed"
    )
