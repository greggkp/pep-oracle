"""Test that the web UI stays within viewport on mobile-sized screens.

Uses Playwright to load index.html with mocked API responses,
then checks no element overflows the viewport.
"""

import json
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "pep_oracle" / "web"

FAKE_STATUS = {
    "ingested_count": 50,
    "feed_count": 100,
    "chunk_count": 5000,
    "db_size_bytes": 123_000_000,
    "earliest_date": "2024-01-15",
    "latest_date": "2026-04-01",
    "earliest_episode": 200,
    "latest_episode": 253,
}


class _MockHandler(SimpleHTTPRequestHandler):
    """Serves index.html and returns mock JSON for API endpoints."""

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((WEB_DIR / "index.html").read_bytes())
        elif self.path == "/status":
            self._json_response(FAKE_STATUS)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/ask":
            self._json_response({"answer": "Mock answer."})
        else:
            self.send_error(404)

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


VIEWPORTS = [
    {"width": 375, "height": 667, "name": "iPhone SE"},
    {"width": 390, "height": 844, "name": "iPhone 14"},
    {"width": 320, "height": 568, "name": "iPhone 5"},
    {"width": 768, "height": 1024, "name": "iPad portrait"},
]


@pytest.mark.parametrize(
    "viewport",
    VIEWPORTS,
    ids=[v["name"] for v in VIEWPORTS],
)
def test_no_horizontal_overflow(server, browser, viewport):
    """No element should extend beyond the viewport width after status loads."""
    page = browser.new_page(viewport={"width": viewport["width"], "height": viewport["height"]})
    page.goto(server)

    # Wait for status to load
    page.wait_for_function(
        "!document.getElementById('status-bar').textContent.includes('Loading')",
        timeout=5000,
    )

    overflow_info = page.evaluate("""() => {
        const vw = document.documentElement.clientWidth;
        const problems = [];
        for (const el of document.querySelectorAll('*')) {
            const rect = el.getBoundingClientRect();
            if (rect.right > vw + 1) {
                problems.push({
                    tag: el.tagName,
                    id: el.id,
                    class: el.className,
                    right: Math.round(rect.right),
                    vw: vw,
                    overflow: Math.round(rect.right - vw),
                });
            }
        }
        return problems;
    }""")

    page.close()

    assert overflow_info == [], (
        f"Elements overflow viewport ({viewport['width']}px) on {viewport['name']}:\n"
        + "\n".join(
            f"  <{p['tag']} id='{p['id']}' class='{p['class']}'> overflows by {p['overflow']}px (right={p['right']}, vw={p['vw']})"
            for p in overflow_info
        )
    )


def test_coverage_line_visible(server, browser):
    """The coverage info line should be visible and contain episode range."""
    page = browser.new_page(viewport={"width": 375, "height": 667})
    page.goto(server)

    page.wait_for_function(
        "!document.getElementById('coverage').textContent.includes('Loading')",
        timeout=5000,
    )

    coverage = page.text_content("#coverage")
    page.close()

    assert "50 episodes ingested" in coverage
    assert "200\u2013253" in coverage
