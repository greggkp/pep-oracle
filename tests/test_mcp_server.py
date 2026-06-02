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
    monkeypatch.setattr(mcp_server, "get_fresh_collection", lambda: col)
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
    out = mcp_server.search_pep("anything", top_k=3)
    assert len(out["results"]) == 3


# --- (c) empty collection returns no results ---


def test_search_pep_empty_collection(patched):
    out = mcp_server.search_pep("nothing here", top_k=5)
    assert out["results"] == []
    # Corpus summary is present but empty when nothing is indexed.
    assert out["corpus"]["newest_episode"] is None


# --- (d) fields present on result items ---


def test_search_pep_result_fields(patched):
    _seed_chunks(patched, count=2, with_speakers=True)
    out = mcp_server.search_pep("hello", top_k=2)
    results = out["results"]
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
    out = mcp_server.search_pep("query")
    assert len(out["results"]) == 5


# --- corpus summary lets the caller answer "latest episode" questions ---


def test_search_pep_reports_newest_episode_in_corpus(patched):
    _seed_chunks(patched, count=3)  # seeds episode 251, dated 2026-04-01
    out = mcp_server.search_pep("anything", top_k=5)
    assert out["corpus"]["newest_episode"] == 251
    assert out["corpus"]["newest_episode_date"] == "2026-04-01"


def test_search_pep_episode_number_scopes_results(patched, monkeypatch):
    # episode_number must be forwarded to hybrid_search as an episode filter so
    # the caller can scope "in the latest episode..." questions.
    captured = {}
    real_query = mcp_server.hybrid_search

    def _spy(collection, query_text, embedding, **kwargs):
        captured.update(kwargs)
        return real_query(collection, query_text, embedding, **kwargs)

    monkeypatch.setattr(mcp_server, "hybrid_search", _spy)
    _seed_chunks(patched, count=3)  # episode 251
    mcp_server.search_pep("anything", top_k=5, episode_number=251)
    assert captured["episode_numbers"] == [251]
    # Default: no episode filter.
    mcp_server.search_pep("anything", top_k=5)
    assert captured["episode_numbers"] is None


def _dated_candidates():
    base = {"speaker_text": None, "speakers": None, "start_time": 1.0, "end_time": 2.0}
    return [
        {**base, "text": "newest", "episode_title": "E263", "episode_number": 263,
         "episode_date": "2026-05-29", "distance": 0.1},
        {**base, "text": "oldest", "episode_title": "E215", "episode_number": 215,
         "episode_date": "2025-06-01", "distance": 0.2},
    ]


def test_search_pep_evolution_intent_orders_chronologically(patched, monkeypatch):
    monkeypatch.setattr(mcp_server, "hybrid_search", lambda *a, **k: _dated_candidates())
    out = mcp_server.search_pep("x", top_k=5, intent="evolution")
    excerpts = [r["excerpt"] for r in out["results"]]
    assert excerpts.index("oldest") < excerpts.index("newest")  # oldest-first


def test_search_pep_default_intent_is_newest_first(patched, monkeypatch):
    monkeypatch.setattr(mcp_server, "hybrid_search", lambda *a, **k: _dated_candidates())
    out = mcp_server.search_pep("x", top_k=5)  # intent=None
    excerpts = [r["excerpt"] for r in out["results"]]
    assert excerpts.index("newest") < excerpts.index("oldest")


def test_search_pep_forwards_date_filters(patched, monkeypatch):
    captured = {}
    monkeypatch.setattr(mcp_server, "hybrid_search",
                        lambda *a, **k: captured.update(k) or [])
    mcp_server.search_pep("x", top_k=5, after_date="2026-01-01", before_date="2026-06-01")
    assert captured["after_date"] == "2026-01-01"
    assert captured["before_date"] == "2026-06-01"


# --- tool name + front-loaded description (deferred-truncation survival) ---


async def test_tool_exported_under_descriptive_name():
    tools = await mcp_server.mcp.list_tools()
    names = [t.name for t in tools]
    assert mcp_server.SEARCH_TOOL_NAME in names
    assert mcp_server.SEARCH_TOOL_NAME == "search_us_politics_commentary"


def test_description_front_loads_trigger():
    desc = mcp_server.SEARCH_PEP_DESCRIPTION
    # First sentence must be the "when to call" trigger, not "what it is",
    # because deferred MCP clients truncate the tail before they see it.
    first_sentence = desc.split(".")[0].lower()
    assert "call this" in first_sentence
    assert "us politics" in first_sentence
    # The "it's a podcast" framing must come AFTER the trigger.
    assert desc.index("Call this") < desc.index("podcast")


# --- (e) /mcp mount + JWT bearer auth ---


_TEST_SIGNING_KEY = "test-signing-key-not-a-secret-padding-padding"
_TEST_PUBLIC_URL = "https://test.example.com"
_TEST_ISSUER = _TEST_PUBLIC_URL.rstrip("/")


def _build_app(monkeypatch, *, signing_key=_TEST_SIGNING_KEY, public_url=_TEST_PUBLIC_URL, trust_flag="1", tmp_path=None):
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

    if signing_key is None:
        monkeypatch.delenv("PEP_ORACLE_OAUTH_SIGNING_KEY", raising=False)
    else:
        monkeypatch.setenv("PEP_ORACLE_OAUTH_SIGNING_KEY", signing_key)
    if public_url is None:
        monkeypatch.delenv("PEP_ORACLE_PUBLIC_URL", raising=False)
    else:
        monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", public_url)
    if trust_flag is None:
        monkeypatch.delenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", raising=False)
    else:
        monkeypatch.setenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", trust_flag)
    if tmp_path is not None:
        monkeypatch.setenv("PEP_ORACLE_DATA_DIR", str(tmp_path))

    app = FastAPI()
    mounted = mount_mcp_if_configured(app)
    return app, mounted


def _mint(client_id: str = "test-client", *, signing_key: str = _TEST_SIGNING_KEY, issuer: str = _TEST_ISSUER) -> str:
    from pep_oracle import oauth

    return oauth.mint_access_token(signing_key, client_id, issuer=issuer)


def test_mount_skipped_when_public_url_unset(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, public_url=None, tmp_path=tmp_path)
    assert mounted is False
    with TestClient(app) as client:
        # Any /mcp path should 404 since nothing is mounted.
        resp = client.post("/mcp")
        assert resp.status_code == 404
        resp = client.get("/mcp/")
        assert resp.status_code == 404


def test_mount_skipped_when_public_url_empty(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, public_url="   ", tmp_path=tmp_path)
    assert mounted is False
    with TestClient(app) as client:
        resp = client.post("/mcp")
        assert resp.status_code == 404


def test_mount_skipped_when_trust_flag_missing(monkeypatch, tmp_path, caplog):
    """Signing key + public URL set, but trust flag unset → mount refused
    with ERROR log explaining the upstream-auth requirement."""
    import logging as _logging

    from fastapi.testclient import TestClient

    with caplog.at_level(_logging.ERROR, logger="pep_oracle.server"):
        app, mounted = _build_app(monkeypatch, trust_flag=None, tmp_path=tmp_path)
    assert mounted is False
    assert any(
        "PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH" in r.message and r.levelno == _logging.ERROR
        for r in caplog.records
    )
    with TestClient(app) as client:
        resp = client.post("/mcp")
        assert resp.status_code == 404


def test_mount_skipped_when_trust_flag_not_one(monkeypatch, tmp_path):
    """Trust flag must be the literal '1' — 'true', 'yes', '0' all refuse."""
    from fastapi.testclient import TestClient

    for bad_val in ("0", "true", "yes", "TRUE", "1 "):
        app, mounted = _build_app(monkeypatch, trust_flag=bad_val, tmp_path=tmp_path)
        assert mounted is False, f"trust_flag={bad_val!r} should refuse mount"
        with TestClient(app) as client:
            resp = client.post("/mcp")
            assert resp.status_code == 404


def test_mount_extends_transport_security_allowed_hosts(monkeypatch, tmp_path):
    """mount_mcp_if_configured must add the public hostname to the FastMCP
    SDK's TransportSecurity allowed_hosts/allowed_origins. Default rejects
    non-localhost Host headers (DNS rebinding defense), which would 421 every
    real request once the server is behind a tunnel."""
    from pep_oracle.mcp_server import mcp as global_mcp

    app, mounted = _build_app(
        monkeypatch, public_url="https://pep-oracle.iicapn.com", tmp_path=tmp_path
    )
    assert mounted is True
    ts = global_mcp.settings.transport_security
    assert "pep-oracle.iicapn.com" in ts.allowed_hosts
    assert "https://pep-oracle.iicapn.com" in ts.allowed_origins
    # Localhost defaults still present (don't break dev / tests)
    assert any("localhost" in h for h in ts.allowed_hosts)


def test_mcp_401_when_no_authorization_header(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, tmp_path=tmp_path)
    assert mounted is True
    with TestClient(app) as client:
        resp = client.post("/mcp")
        assert resp.status_code == 401
        resp = client.get("/mcp/anything")
        assert resp.status_code == 401


def test_mcp_401_when_wrong_token(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, tmp_path=tmp_path)
    assert mounted is True
    with TestClient(app) as client:
        # Garbage non-JWT
        resp = client.post("/mcp", headers={"Authorization": "Bearer not-a-jwt"})
        assert resp.status_code == 401
        # JWT signed with the wrong key
        bad_jwt = _mint(signing_key="some-other-key-padding-padding-padding")
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {bad_jwt}"})
        assert resp.status_code == 401


def test_mcp_401_when_jwt_tampered(monkeypatch, tmp_path):
    """Flipping a character in the JWT body invalidates the signature."""
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, tmp_path=tmp_path)
    assert mounted is True
    good = _mint()
    # Tamper: flip a character in the middle (the payload section).
    parts = good.split(".")
    assert len(parts) == 3
    payload = parts[1]
    flipped = payload[:-2] + ("A" if payload[-2] != "A" else "B") + payload[-1]
    bad = ".".join([parts[0], flipped, parts[2]])
    with TestClient(app) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {bad}"})
        assert resp.status_code == 401


def test_mcp_401_when_jwt_wrong_issuer(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, tmp_path=tmp_path)
    assert mounted is True
    bad_iss = _mint(issuer="https://attacker.example.com")
    with TestClient(app) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {bad_iss}"})
        assert resp.status_code == 401


def test_mcp_401_when_malformed_authorization(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, tmp_path=tmp_path)
    assert mounted is True
    tok = _mint()
    with TestClient(app) as client:
        # Not "Bearer <token>"
        resp = client.post("/mcp", headers={"Authorization": tok})
        assert resp.status_code == 401
        resp = client.post("/mcp", headers={"Authorization": f"Basic {tok}"})
        assert resp.status_code == 401


def test_mcp_valid_jwt_passes_auth(monkeypatch, tmp_path):
    """When a valid JWT is sent, the request gets past the auth gate and
    into the mounted MCP ASGI app. We exercise both branches on the same
    mounted app instance so this test catches wrapper-removal regressions."""
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, tmp_path=tmp_path)
    assert mounted is True
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer not-a-jwt",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert resp.status_code == 401

        good = _mint()
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {good}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        # Anything but 401 means our auth gate let it through.
        assert resp.status_code != 401


def test_mcp_case_insensitive_bearer_scheme(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app, mounted = _build_app(monkeypatch, tmp_path=tmp_path)
    assert mounted is True
    good = _mint()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": f"bearer {good}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert resp.status_code != 401


def test_get_serving_corpus_uses_artifact_when_flagged(tmp_path, monkeypatch):
    import pep_oracle.config as config
    import pep_oracle.corpus as corpus
    import pep_oracle.hybrid as hybrid
    import pep_oracle.mcp_server as mcp_server

    corpus.write_artifact(
        [
            {"chunk_id": "z1", "text": "the byrd rule reconciliation senate",
             "embedding": [1.0, 0.0],
             "metadata": {"episode_number": 251, "episode_date": "2026-04-01",
                          "episode_guid": "g", "episode_title": "Ep 251",
                          "start_time": 0.0, "end_time": 10.0}},
        ],
        dest=str(tmp_path), version="v0001",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s", built_at="t",
    )
    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", True)
    monkeypatch.setattr(config, "CORPUS_URI", str(tmp_path))
    monkeypatch.setattr(config, "EMBED_BACKEND", "bedrock")
    monkeypatch.setattr(config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    corpus.reset_serving_cache()
    hybrid._CACHE.clear()

    c = mcp_server.get_serving_corpus()
    assert c.__class__.__name__ == "InMemoryCorpus"
    assert c.version == "v0001"


def test_get_serving_corpus_uses_chroma_by_default(monkeypatch):
    import pep_oracle.config as config
    import pep_oracle.mcp_server as mcp_server

    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", False)
    sentinel = object()
    monkeypatch.setattr(mcp_server, "get_fresh_collection", lambda: sentinel)
    assert mcp_server.get_serving_corpus() is sentinel
