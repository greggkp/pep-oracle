"""Playwright tests against the real database.

Starts the FastAPI server with the actual ChromaDB data and RSS feed,
then verifies the UI correctly reflects which episodes have been ingested.

Run explicitly:  pytest tests/test_web_live.py -v -m live
"""

import threading

import pytest
import uvicorn

pytestmark = pytest.mark.live

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from pep_oracle.feed import fetch_episodes
from pep_oracle.store import get_client, get_collection, get_ingested_guids


def _get_expected_ingestion_state():
    """Read the real ChromaDB and RSS feed to determine expected state.

    Returns (dict, int, int, int):
        - {episode_number: ingested_bool} for every numbered episode
        - count of ingested episodes (by number, deduped)
        - count of ingested episodes (by GUID, as server counts)
        - total feed episode count (including unnumbered)
    """
    from chromadb.api.shared_system_client import SharedSystemClient

    SharedSystemClient.clear_system_cache()
    client = get_client()
    collection = get_collection(client)
    ingested_guids = get_ingested_guids(collection)
    episodes = fetch_episodes()
    SharedSystemClient.clear_system_cache()

    expected = {}
    for ep in episodes:
        if ep.episode_number is None:
            continue
        expected[ep.episode_number] = ep.guid in ingested_guids

    ingested_by_number = sum(1 for v in expected.values() if v)
    ingested_by_guid = sum(1 for ep in episodes if ep.guid in ingested_guids)
    return expected, ingested_by_number, ingested_by_guid, len(episodes)


@pytest.fixture(scope="module")
def live_server():
    """Start the real FastAPI app against the real database."""
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

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


def _get_ui_episode_states(page):
    """Return {episode_number: ingested_bool} from the dropdown."""
    raw = page.evaluate("""() => {
        const opts = document.querySelectorAll('#episode-filter option');
        const result = {};
        for (const opt of opts) {
            if (!opt.value) continue;
            // star suffix means NOT ingested
            result[opt.value] = !opt.textContent.endsWith(' *');
        }
        return result;
    }""")
    return {int(k): v for k, v in raw.items()}


def test_ingested_episodes_match_database(live_server, browser):
    """Every episode the UI shows as ingested/not-ingested must match ChromaDB."""
    expected, ingested_count, _, _ = _get_expected_ingestion_state()
    assert len(expected) > 0, "No episodes found in feed"

    page = browser.new_page()
    page.goto(live_server)
    page.wait_for_function(
        "document.querySelectorAll('#episode-filter option').length > 10",
        timeout=10000,
    )
    ui_states = _get_ui_episode_states(page)
    page.close()

    # Every numbered episode in the DB must appear in the UI
    db_episodes = set(expected.keys())
    ui_episodes = set(ui_states.keys())
    missing_from_ui = db_episodes - ui_episodes
    assert not missing_from_ui, (
        f"Episodes in feed but missing from UI dropdown: {sorted(missing_from_ui)}"
    )

    # Ingested state must match for every episode
    mismatches = []
    for ep_num in sorted(db_episodes & ui_episodes):
        db_ingested = expected[ep_num]
        ui_ingested = ui_states[ep_num]
        if ui_ingested != db_ingested:
            db_label = "ingested" if db_ingested else "not ingested"
            ui_label = "ingested" if ui_ingested else "not ingested"
            mismatches.append(
                f"  Ep {ep_num}: DB={db_label}, UI={ui_label}"
            )

    assert not mismatches, (
        f"Ingestion state mismatch ({ingested_count} episodes ingested in DB):\n"
        + "\n".join(mismatches)
    )


def test_status_bar_counts_match_database(live_server, browser):
    """The status bar ingested/feed counts must match reality."""
    _, _, ingested_count, total_feed = _get_expected_ingestion_state()

    page = browser.new_page()
    page.goto(live_server)
    page.wait_for_function(
        "!document.getElementById('status-bar').textContent.includes('Loading')",
        timeout=10000,
    )
    status_text = page.text_content("#status-bar")
    page.close()

    assert f"{ingested_count}/{total_feed} episodes ingested" in status_text, (
        f"Status bar says '{status_text}' but expected "
        f"{ingested_count}/{total_feed} episodes ingested"
    )
