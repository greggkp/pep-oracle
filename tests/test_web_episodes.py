"""Test that the web UI correctly reflects ingestion status.

Uses Playwright against the real FastAPI app with an in-memory ChromaDB
collection and a mocked RSS feed + topics file.
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
import uvicorn

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from pep_oracle.models import Chunk, Episode
from pep_oracle.store import add_chunks, get_ingested_guids, get_ingestion_stats


def _make_episode(num, guid=None):
    return Episode(
        guid=guid or f"guid-{num}",
        title=f"Test Episode (Ep {num})",
        pub_date=datetime(2026, 1, num, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=f"Episode {num}",
        duration_seconds=3600,
        episode_number=num,
    )


EPISODES = [_make_episode(i) for i in range(1, 6)]

TOPICS_DATA = {
    "episodes": [
        {"episode_number": 5, "date": "2026-01-05", "topics": ["Topic from Ep 5", "Second Topic Ep 5"]},
        {"episode_number": 3, "date": "2026-01-03", "topics": ["Topic from Ep 3"]},
        {"episode_number": 2, "date": "2026-01-02", "topics": ["Topic from Ep 2"]},
    ]
}


def _ingest_into_collection(collection, guid: str, episode_number: int):
    chunk = Chunk(
        chunk_id=f"{guid}_0000",
        episode_guid=guid,
        text="Some transcript text",
        episode_title=f"Episode {episode_number}",
        episode_date=f"2026-01-{episode_number:02d}",
        episode_number=episode_number,
        start_time=0.0,
        end_time=60.0,
    )
    add_chunks(collection, [chunk], [[0.1] * 10])


@pytest.fixture()
def server_with_collection(tmp_path):
    """Start the real FastAPI app with in-memory ChromaDB, mocked feed, and topics file."""
    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection(
        name="pep_oracle_" + uuid.uuid4().hex[:8], metadata={"hnsw:space": "cosine"}
    )

    topics_path = tmp_path / "topics.json"
    topics_path.write_text(json.dumps(TOPICS_DATA))

    patches = [
        patch("pep_oracle.server.fetch_episodes", return_value=EPISODES),
        patch("pep_oracle.server._get_fresh_collection", return_value=collection),
        patch("pep_oracle.server.get_ingested_guids", wraps=lambda col: get_ingested_guids(collection)),
        patch("pep_oracle.server.get_ingestion_stats", wraps=lambda col: get_ingestion_stats(collection)),
        patch("pep_oracle.server.CHROMA_DIR", Path("/tmp/fake-chroma")),
        patch("pep_oracle.server.TOPICS_PATH", topics_path),
    ]
    for p in patches:
        p.start()

    from pep_oracle.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)

    started = threading.Event()
    original_startup = server.startup

    async def _startup_then_signal(*a, **kw):
        await original_startup(*a, **kw)
        started.set()

    server.startup = _startup_then_signal

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    started.wait(timeout=10)

    sockets = server.servers[0].sockets if server.servers else []
    port = sockets[0].getsockname()[1] if sockets else None
    assert port, "Server failed to bind"

    yield f"http://127.0.0.1:{port}", collection

    server.should_exit = True
    thread.join(timeout=5)
    for p in patches:
        p.stop()


def test_status_bar_shows_ingested_count(server_with_collection, browser):
    """The status bar should reflect how many episodes are ingested."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function(
        "document.getElementById('status-bar').textContent.includes('episodes ingested')",
        timeout=15000,
    )
    status_text = page.text_content("#status-bar")
    page.close()

    assert "/5 episodes ingested" in status_text
    assert "excerpts" in status_text


def test_coverage_line_shows_no_episodes(server_with_collection, browser):
    """Before ingestion, coverage should say no episodes ingested."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function(
        "document.getElementById('coverage').textContent.includes('No episodes')",
        timeout=15000,
    )
    coverage = page.text_content("#coverage")
    page.close()

    assert "No episodes ingested" in coverage


def test_coverage_line_shows_range_after_ingestion(server_with_collection, browser):
    """After ingestion, coverage should show episode range and dates."""
    base_url, collection = server_with_collection

    _ingest_into_collection(collection, "guid-1", 1)
    _ingest_into_collection(collection, "guid-3", 3)

    from pep_oracle.server import _caches, _fetch_status
    _caches["status"].set(_fetch_status())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function(
        "document.getElementById('coverage').textContent.includes('2 episodes')",
        timeout=15000,
    )
    coverage = page.text_content("#coverage")
    page.close()

    assert "2 episodes ingested" in coverage
    assert "1\u20133" in coverage


def test_chip_text_includes_episode_number(server_with_collection, browser):
    """Chips display topic and episode number inline."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip:not(.more)", timeout=10000)

    chip = page.locator(".topic-chip:not(.more)").first
    text = chip.text_content()
    page.close()

    # Chip text should contain topic + episode number with middot separator
    assert "\u00b7 Ep 5" in text


def test_initial_chips_from_latest_episode(server_with_collection, browser):
    """Initially only the latest episode's topics are shown as chips."""
    base_url, _ = server_with_collection

    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip:not(.more)", timeout=10000)

    chips = page.locator(".topic-chip:not(.more)")
    count = chips.count()
    page.close()

    # TOPICS_DATA has 2 topics for ep 5 (the latest)
    assert count == 2


def test_not_ingested_chips_have_amber_styling(server_with_collection, browser):
    """Chips for un-ingested episodes should have the not-ingested CSS class."""
    base_url, collection = server_with_collection

    _ingest_into_collection(collection, "guid-5", 5)

    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip:not(.more)", timeout=10000)

    chips = page.query_selector_all(".topic-chip:not(.more)")
    for chip in chips:
        ep_num = chip.get_attribute("data-episode")
        if ep_num == "5":
            assert "not-ingested" not in chip.get_attribute("class")

    page.close()


def test_ingest_banner_visible_when_not_ingested(server_with_collection, browser):
    """The ingest banner should appear when un-ingested episodes exist."""
    base_url, collection = server_with_collection

    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector("#ingest-banner", state="visible", timeout=15000)

    banner_text = page.text_content("#ingest-banner")
    assert "not yet ingested" in banner_text

    page.close()


def test_ingest_banner_hidden_when_all_ingested(server_with_collection, browser):
    """The ingest banner should not appear when all episodes are ingested."""
    base_url, collection = server_with_collection

    for i in range(1, 6):
        _ingest_into_collection(collection, f"guid-{i}", i)

    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    banner = page.query_selector("#ingest-banner")
    assert banner is None or not banner.is_visible()

    page.close()


def test_not_ingested_chip_tooltip(server_with_collection, browser):
    """Un-ingested chips should show '(not yet ingested)' in their tooltip."""
    base_url, collection = server_with_collection

    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip:not(.more)", timeout=10000)

    chips = page.query_selector_all(".topic-chip:not(.more)")
    for chip in chips:
        ep_num = chip.get_attribute("data-episode")
        title = chip.get_attribute("title")
        if ep_num == "5":
            assert "(not yet ingested)" in title

    page.close()


def test_chip_click_adds_used_class(server_with_collection, browser):
    """Clicking a topic chip adds the 'used' CSS class."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip:not(.more)", timeout=15000)

    chip = page.locator(".topic-chip:not(.more)").first
    chip.click()

    assert "used" in chip.get_attribute("class")
    page.close()


def test_chip_click_populates_question_with_episode(server_with_collection, browser):
    """Clicking a chip populates the question field with episode-specific template."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip:not(.more)", timeout=15000)

    chip = page.locator(".topic-chip:not(.more)").first
    chip.click()

    question_val = page.locator("#question").input_value()
    assert "What did Chas and Dave discuss about" in question_val
    assert "(Episode 5)" in question_val
    page.close()


def test_more_button_visible_when_more_episodes(server_with_collection, browser):
    """A 'More...' button appears when there are older episodes with topics."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=15000)

    more_btn = page.locator(".topic-chip.more")
    assert more_btn.count() == 1
    assert more_btn.text_content() == "More\u2026"
    page.close()


def test_more_button_adds_next_episode_chips(server_with_collection, browser):
    """Clicking 'More...' adds the next episode's topics."""
    base_url, _ = server_with_collection

    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=15000)

    count_before = page.locator(".topic-chip:not(.more)").count()
    page.locator(".topic-chip.more").click()

    # Ep 3 has 1 topic, should be added
    new_count = page.locator(".topic-chip:not(.more)").count()
    assert new_count == count_before + 1

    # "More..." still present (ep 2 remains)
    assert page.locator(".topic-chip.more").count() == 1

    # Click again to add ep 2
    page.locator(".topic-chip.more").click()
    final_count = page.locator(".topic-chip:not(.more)").count()
    assert final_count == new_count + 1

    # "More..." removed (no more episodes)
    assert page.locator(".topic-chip.more").count() == 0

    page.close()


def test_used_chip_still_populates_question(server_with_collection, browser):
    """Clicking a used chip still populates the question field."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip:not(.more)", timeout=15000)

    chip = page.locator(".topic-chip:not(.more)").first
    chip.click()
    chip.click()

    question_val = page.locator("#question").input_value()
    assert len(question_val) > 0
    page.close()
