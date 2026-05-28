import json

import pytest

from pep_oracle import mcp_server
from pep_oracle.models import Chunk
from pep_oracle.store import add_chunks, get_client


_counter = 0


def _fresh_collection():
    global _counter
    _counter += 1
    client = get_client(persistent=False)
    return client.get_or_create_collection(
        name=f"test_mcp_{_counter}",
        metadata={"hnsw:space": "cosine"},
    )


@pytest.fixture
def patched(monkeypatch):
    """Patch embed_texts and store accessors to use an in-memory collection."""
    col = _fresh_collection()

    # Fixed embedding — must match dimension of seeded chunks.
    fixed_embedding = [1.0] + [0.0] * 9

    monkeypatch.setattr(
        mcp_server, "embed_texts", lambda texts: [fixed_embedding for _ in texts]
    )
    monkeypatch.setattr(mcp_server, "get_client", lambda: object())
    monkeypatch.setattr(mcp_server, "get_collection", lambda client: col)
    return col


def _seed_chunks(col, count: int = 3, with_speakers: bool = False):
    chunks = []
    embeddings = []
    for i in range(count):
        speaker_text = None
        speaker_turns = None
        if with_speakers:
            speaker_text = f"[Chas] Hello {i}. [Dave] World {i}."
            speaker_turns = [
                {"speaker": "Chas", "start": float(i * 240), "end": float(i * 240 + 120)},
                {"speaker": "Dave", "start": float(i * 240 + 120), "end": float((i + 1) * 240)},
            ]
        chunks.append(
            Chunk(
                chunk_id=f"ep-1_{i:04d}",
                episode_guid="ep-1",
                text=f"Chunk {i} content",
                episode_title="Test Episode",
                episode_date="2026-04-01",
                start_time=float(i * 240),
                end_time=float((i + 1) * 240),
                episode_number=251,
                speaker_text=speaker_text,
                speaker_turns=speaker_turns,
            )
        )
        emb = [1.0] + [0.0] * 9
        embeddings.append(emb)
    add_chunks(col, chunks, embeddings)


# --- (a) format_citation shape ---


def test_format_citation_shape_basic():
    result = {
        "chunk_id": "ep-1_0000",
        "text": "raw text",
        "distance": 0.1,
        "episode_guid": "ep-1",
        "episode_title": "Big Episode",
        "episode_date": "2026-04-01",
        "episode_number": 251,
        "start_time": 125.0,
        "end_time": 250.0,
    }
    cit = mcp_server.format_citation(result)
    assert cit == {
        "episode_number": 251,
        "episode_title": "Big Episode",
        "episode_date": "2026-04-01",
        "timestamp": "0:02:05",
        "start_seconds": 125.0,
        "end_seconds": 250.0,
        "speakers": [],
        "excerpt": "raw text",
    }


def test_format_citation_prefers_speaker_text():
    result = {
        "text": "fallback text",
        "speaker_text": "[Chas] preferred text.",
        "episode_title": "T",
        "episode_date": "2026-01-01",
        "episode_number": 1,
        "start_time": 0.0,
        "end_time": 60.0,
    }
    cit = mcp_server.format_citation(result)
    assert cit["excerpt"] == "[Chas] preferred text."


def test_format_citation_speakers_parsed_from_json():
    turns = [
        {"speaker": "Dave", "start": 0.0, "end": 5.0},
        {"speaker": "Chas", "start": 5.0, "end": 10.0},
        {"speaker": "Chas", "start": 10.0, "end": 15.0},
    ]
    result = {
        "text": "x",
        "episode_title": "T",
        "episode_date": "2026-01-01",
        "episode_number": 1,
        "start_time": 0.0,
        "end_time": 15.0,
        "speakers": json.dumps(turns),
    }
    cit = mcp_server.format_citation(result)
    assert cit["speakers"] == ["Chas", "Dave"]  # sorted unique


def test_format_citation_timestamp_question_mark_when_none():
    result = {
        "text": "x",
        "episode_title": "T",
        "episode_date": "2026-01-01",
        "episode_number": 1,
        "start_time": None,
        "end_time": None,
    }
    cit = mcp_server.format_citation(result)
    assert cit["timestamp"] == "?"
    assert cit["start_seconds"] is None
    assert cit["end_seconds"] is None


def test_format_citation_episode_number_none_when_missing():
    result = {
        "text": "x",
        "episode_title": "T",
        "episode_date": "2026-01-01",
        "episode_number": None,
        "start_time": 0.0,
        "end_time": 1.0,
    }
    cit = mcp_server.format_citation(result)
    assert cit["episode_number"] is None


# --- (b) search_pep top_k ---


def test_search_pep_respects_top_k(patched):
    _seed_chunks(patched, count=10)
    results = mcp_server.search_pep("anything", top_k=3)
    assert len(results) == 3


# --- (c) empty collection returns [] ---


def test_search_pep_empty_collection(patched):
    results = mcp_server.search_pep("nothing here", top_k=5)
    assert results == []


# --- (d) fields present on result items ---


def test_search_pep_result_fields(patched):
    _seed_chunks(patched, count=2, with_speakers=True)
    results = mcp_server.search_pep("hello", top_k=2)
    assert len(results) == 2
    expected_keys = {
        "episode_number",
        "episode_title",
        "episode_date",
        "timestamp",
        "start_seconds",
        "end_seconds",
        "speakers",
        "excerpt",
    }
    for item in results:
        assert set(item.keys()) == expected_keys
        assert isinstance(item["timestamp"], str)
        assert isinstance(item["start_seconds"], float) or item["start_seconds"] is None
        assert isinstance(item["end_seconds"], float) or item["end_seconds"] is None
        assert isinstance(item["speakers"], list)
        assert isinstance(item["excerpt"], str)
        # With speakers seeded, we should find Chas and Dave
        assert item["speakers"] == ["Chas", "Dave"]


def test_search_pep_default_top_k_is_5(patched):
    _seed_chunks(patched, count=8)
    results = mcp_server.search_pep("query")
    assert len(results) == 5


# --- (e) /mcp mount + bearer-token auth ---


def _build_app_with_token(monkeypatch, token: str | None):
    """Build a fresh FastAPI app with mount_mcp_if_configured applied.

    The MCP SDK's session_manager is created lazily on the first call to
    streamable_http_app(). Reset it here so each test gets a fresh manager
    (the SDK refuses to .run() a manager twice).
    """
    from fastapi import FastAPI

    from pep_oracle import mcp_server
    from pep_oracle.server import mount_mcp_if_configured

    # Reset the FastMCP session manager so each test's mount gets a fresh one.
    mcp_server.mcp._session_manager = None

    if token is None:
        monkeypatch.delenv("PEP_ORACLE_MCP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("PEP_ORACLE_MCP_TOKEN", token)

    app = FastAPI()
    mounted = mount_mcp_if_configured(app)
    return app, mounted


def test_mount_skipped_when_token_unset(monkeypatch):
    from fastapi.testclient import TestClient

    app, mounted = _build_app_with_token(monkeypatch, None)
    assert mounted is False
    with TestClient(app) as client:
        # Any /mcp path should 404 since nothing is mounted.
        resp = client.post("/mcp")
        assert resp.status_code == 404
        resp = client.get("/mcp/")
        assert resp.status_code == 404


def test_mount_skipped_when_token_empty(monkeypatch):
    from fastapi.testclient import TestClient

    app, mounted = _build_app_with_token(monkeypatch, "   ")
    assert mounted is False
    with TestClient(app) as client:
        resp = client.post("/mcp")
        assert resp.status_code == 404


def test_mcp_401_when_no_authorization_header(monkeypatch):
    from fastapi.testclient import TestClient

    app, mounted = _build_app_with_token(monkeypatch, "secret-token")
    assert mounted is True
    with TestClient(app) as client:
        resp = client.post("/mcp")
        assert resp.status_code == 401
        resp = client.get("/mcp/anything")
        assert resp.status_code == 401


def test_mcp_401_when_wrong_token(monkeypatch):
    from fastapi.testclient import TestClient

    app, mounted = _build_app_with_token(monkeypatch, "secret-token")
    assert mounted is True
    with TestClient(app) as client:
        resp = client.post("/mcp", headers={"Authorization": "Bearer wrongtoken"})
        assert resp.status_code == 401


def test_mcp_401_when_malformed_authorization(monkeypatch):
    from fastapi.testclient import TestClient

    app, mounted = _build_app_with_token(monkeypatch, "secret-token")
    assert mounted is True
    with TestClient(app) as client:
        # Not "Bearer <token>"
        resp = client.post("/mcp", headers={"Authorization": "secret-token"})
        assert resp.status_code == 401
        resp = client.post("/mcp", headers={"Authorization": "Basic secret-token"})
        assert resp.status_code == 401


def test_mcp_correct_token_passes_auth(monkeypatch):
    """When the right bearer token is sent, the request gets past the auth
    gate and into the mounted MCP ASGI app. We exercise both branches on the
    same mounted app instance so this test catches wrapper-removal regressions."""
    from fastapi.testclient import TestClient

    app, mounted = _build_app_with_token(monkeypatch, "secret-token")
    assert mounted is True
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer wrong-token",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert resp.status_code == 401

        resp = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer secret-token",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        # Anything but 401 means our auth gate let it through.
        assert resp.status_code != 401


def test_mcp_case_insensitive_bearer_scheme(monkeypatch):
    from fastapi.testclient import TestClient

    app, mounted = _build_app_with_token(monkeypatch, "secret-token")
    assert mounted is True
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": "bearer secret-token",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert resp.status_code != 401
