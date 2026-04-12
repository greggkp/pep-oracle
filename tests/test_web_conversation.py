"""Test conversation follow-up flow in the web UI.

Uses Playwright against the real FastAPI app with mocked do_ask.
"""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import uvicorn

pytest.importorskip("playwright.sync_api", reason="playwright not installed")


@pytest.fixture()
def server_with_mock_ask():
    """Start FastAPI with do_ask mocked to return canned responses."""
    call_count = 0

    def fake_ask(question, top_k=10, history=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "First answer about tariffs from Episode 255."
        elif call_count == 2:
            return "Follow-up answer about the EU response."
        return f"Answer number {call_count}."

    patches = [
        patch("pep_oracle.server.do_ask", side_effect=fake_ask),
        patch("pep_oracle.server.fetch_episodes", return_value=[]),
        patch("pep_oracle.server._get_fresh_collection"),
        patch("pep_oracle.server.get_ingested_guids", return_value=set()),
        patch("pep_oracle.server.get_ingestion_stats", return_value={
            "earliest_date": None, "latest_date": None,
            "earliest_episode": None, "latest_episode": None,
        }),
        patch("pep_oracle.server.CHROMA_DIR", Path("/tmp/fake-chroma")),
        patch("pep_oracle.server.extract_topics", return_value=[]),
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

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)
    for p in patches:
        p.stop()


def test_conversation_follow_up(server_with_mock_ask, browser):
    """Submit a question, then a follow-up — both should appear as chat bubbles."""
    base_url = server_with_mock_ask
    page = browser.new_page()
    page.goto(base_url)

    # Ask first question
    page.fill("#question", "What about tariffs?")
    page.click("#submit-btn")
    page.wait_for_selector(".bubble.assistant", timeout=10000)

    # Verify first Q&A appears
    user_bubbles = page.query_selector_all(".bubble.user")
    assistant_bubbles = page.query_selector_all(".bubble.assistant")
    assert len(user_bubbles) == 1
    assert len(assistant_bubbles) == 1
    assert "tariffs" in user_bubbles[0].text_content().lower()
    assert "First answer" in assistant_bubbles[0].text_content()

    # Ask follow-up
    page.fill("#question", "What about the EU response?")
    page.click("#submit-btn")
    page.wait_for_function(
        "document.querySelectorAll('.bubble.assistant').length === 2",
        timeout=10000,
    )

    # Verify both Q&A pairs appear
    user_bubbles = page.query_selector_all(".bubble.user")
    assistant_bubbles = page.query_selector_all(".bubble.assistant")
    assert len(user_bubbles) == 2
    assert len(assistant_bubbles) == 2
    assert "Follow-up answer" in assistant_bubbles[1].text_content()

    # New conversation button should be visible
    assert page.is_visible("#new-convo-btn")

    page.close()


def test_new_conversation_collapses_thread(server_with_mock_ask, browser):
    """Clicking 'New conversation' should collapse the thread and reset."""
    base_url = server_with_mock_ask
    page = browser.new_page()
    page.goto(base_url)

    # Ask a question first
    page.fill("#question", "What about tariffs?")
    page.click("#submit-btn")
    page.wait_for_selector(".bubble.assistant", timeout=10000)

    # Click new conversation
    page.click("#new-convo-btn")

    # Thread should be hidden
    assert not page.is_visible("#thread")

    # Collapsed summary should appear
    collapsed = page.query_selector_all(".collapsed-thread")
    assert len(collapsed) == 1
    assert "tariffs" in collapsed[0].text_content().lower()

    # New conversation button should be hidden (no active thread)
    assert not page.is_visible("#new-convo-btn")

    # Placeholder should reset
    placeholder = page.get_attribute("#question", "placeholder")
    assert "What have Chas and Dave said about" in placeholder

    page.close()


def test_collapsed_thread_expands(server_with_mock_ask, browser):
    """Clicking a collapsed thread should expand to show the old messages."""
    base_url = server_with_mock_ask
    page = browser.new_page()
    page.goto(base_url)

    # Ask and then start new conversation
    page.fill("#question", "What about tariffs?")
    page.click("#submit-btn")
    page.wait_for_selector(".bubble.assistant", timeout=10000)
    page.click("#new-convo-btn")

    # Click the collapsed thread to expand
    collapsed = page.query_selector(".collapsed-thread")
    collapsed.click()

    # Should show the old messages
    body = page.query_selector(".collapsed-body")
    assert body.is_visible()
    assert "First answer" in body.text_content()

    page.close()


def test_resume_collapsed_thread(server_with_mock_ask, browser):
    """Clicking Resume on a collapsed thread should restore it as the active conversation."""
    base_url = server_with_mock_ask
    page = browser.new_page()
    page.goto(base_url)

    # Ask a question and collapse the thread
    page.fill("#question", "What about tariffs?")
    page.click("#submit-btn")
    page.wait_for_selector(".bubble.assistant", timeout=10000)
    page.click("#new-convo-btn")

    # Verify thread is collapsed
    assert not page.is_visible("#thread")
    collapsed = page.query_selector_all(".collapsed-thread")
    assert len(collapsed) == 1

    # Click Resume
    page.click(".collapsed-resume")

    # Thread should be restored
    assert page.is_visible("#thread")
    user_bubbles = page.query_selector_all(".bubble.user")
    assistant_bubbles = page.query_selector_all(".bubble.assistant")
    assert len(user_bubbles) == 1
    assert len(assistant_bubbles) == 1
    assert "tariffs" in user_bubbles[0].text_content().lower()

    # Collapsed area should be empty
    collapsed = page.query_selector_all(".collapsed-thread")
    assert len(collapsed) == 0

    # New conversation button should be visible again
    assert page.is_visible("#new-convo-btn")

    page.close()
