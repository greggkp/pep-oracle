import os

import pytest

# Clear PEP_ORACLE_MCP_TOKEN before any test imports so that the bare
# `from pep_oracle.server import app` path in tests/test_server.py doesn't
# mount the MCP sub-app on the global FastAPI instance. (Without this, the
# dev .env leaks the real token into the test process; the global app gets
# its lifespan wrapped to run a single-use StreamableHTTPSessionManager
# which then explodes on the second TestClient entry.)
# Set to empty string (not pop) so python-dotenv's load_dotenv() — which
# only fills *missing* keys by default — doesn't refill from .env.
os.environ["PEP_ORACLE_MCP_TOKEN"] = ""


@pytest.fixture(scope="module")
def browser():
    """Shared Playwright browser instance for web tests."""
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()
