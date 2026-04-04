"""Test that the web UI stays within viewport on mobile-sized screens.

Uses Playwright to load index.html with mocked API responses containing
many long episode titles, then checks no element overflows the viewport.
"""

import json
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "pep_oracle" / "web"

# Fake episodes with deliberately long titles
FAKE_EPISODES = [
    {
        "episode_number": i,
        "title": f"Episode {i}: A Very Long Podcast Title That Should Be Truncated Properly ({i})",
        "ingested": i % 2 == 0,
    }
    for i in range(1, 101)
]

FAKE_STATUS = {
    "ingested_count": 50,
    "feed_count": 100,
    "chunk_count": 5000,
    "db_size_bytes": 123_000_000,
}


class _MockHandler(SimpleHTTPRequestHandler):
    """Serves index.html and returns mock JSON for API endpoints."""

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((WEB_DIR / "index.html").read_bytes())
        elif self.path == "/episodes":
            self._json_response(FAKE_EPISODES)
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
        pass  # silence request logs during tests


@pytest.fixture(scope="module")
def server():
    """Start a local HTTP server serving the web UI with mock data."""
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
    """No element should extend beyond the viewport width after episodes load."""
    page = browser.new_page(viewport={"width": viewport["width"], "height": viewport["height"]})
    page.goto(server)

    # Wait for episodes to be loaded into the dropdown
    page.wait_for_function(
        "document.querySelectorAll('#episode-filter option').length > 10",
        timeout=5000,
    )

    # Check that no element overflows the viewport
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


def test_episode_dropdown_fits_within_controls(server, browser):
    """The episode select should not be wider than its parent container at 375px."""
    page = browser.new_page(viewport={"width": 375, "height": 667})
    page.goto(server)

    page.wait_for_function(
        "document.querySelectorAll('#episode-filter option').length > 10",
        timeout=5000,
    )

    result = page.evaluate("""() => {
        const select = document.getElementById('episode-filter');
        const controls = document.querySelector('.controls');
        const selectRect = select.getBoundingClientRect();
        const controlsRect = controls.getBoundingClientRect();
        return {
            selectWidth: Math.round(selectRect.width),
            controlsWidth: Math.round(controlsRect.width),
            selectRight: Math.round(selectRect.right),
            controlsRight: Math.round(controlsRect.right),
        };
    }""")

    page.close()

    assert result["selectRight"] <= result["controlsRight"] + 1, (
        f"Episode dropdown ({result['selectWidth']}px) extends past controls ({result['controlsWidth']}px)"
    )


def test_option_text_is_truncated(server, browser):
    """Episode option text should be truncated to keep the dropdown manageable."""
    page = browser.new_page(viewport={"width": 375, "height": 667})
    page.goto(server)

    page.wait_for_function(
        "document.querySelectorAll('#episode-filter option').length > 10",
        timeout=5000,
    )

    longest = page.evaluate("""() => {
        const options = document.querySelectorAll('#episode-filter option');
        let max = 0;
        for (const opt of options) {
            if (opt.textContent.length > max) max = opt.textContent.length;
        }
        return max;
    }""")

    page.close()

    # 30 char title + episode number prefix + marker = should be well under 50
    assert longest < 50, f"Longest option text is {longest} chars, expected < 50"
