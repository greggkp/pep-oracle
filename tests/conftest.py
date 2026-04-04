import pytest


@pytest.fixture(scope="module")
def browser():
    """Shared Playwright browser instance for web tests."""
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()
