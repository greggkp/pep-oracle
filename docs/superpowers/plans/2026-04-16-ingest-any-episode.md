# Ingest Any Episode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the web UI ingest any uningested episode (including older gaps), not just episodes newer than the latest ingested.

**Architecture:** Remove the "newer than latest" server filter so `/topics` returns all uningested episodes. Add a `parse_episode_input()` function for range/comma string parsing. Redesign the ingest banner HTML to show an uningested summary, "Ingest latest" button, and a text input for arbitrary episode selection.

**Tech Stack:** Python (FastAPI/Pydantic), vanilla JS, Playwright for web tests

**Spec:** `docs/superpowers/specs/2026-04-16-ingest-any-episode-design.md`

---

### Task 1: `parse_episode_input()` — parser with tests

**Files:**
- Modify: `src/pep_oracle/server.py:40-43`
- Create: `tests/test_parse_episode_input.py`

- [ ] **Step 1: Write the test file**

Create `tests/test_parse_episode_input.py`:

```python
"""Tests for parse_episode_input() — range/comma episode string parsing."""

import pytest

from pep_oracle.server import parse_episode_input


def test_single_number():
    assert parse_episode_input("210") == [210]


def test_comma_list():
    assert parse_episode_input("210, 215, 220") == [210, 215, 220]


def test_range():
    assert parse_episode_input("150-155") == [150, 151, 152, 153, 154, 155]


def test_mixed():
    assert parse_episode_input("150-155, 210, 220-222") == [
        150, 151, 152, 153, 154, 155, 210, 220, 221, 222,
    ]


def test_whitespace_tolerance():
    assert parse_episode_input(" 150 - 155 , 210 ") == [
        150, 151, 152, 153, 154, 155, 210,
    ]


def test_duplicates_removed():
    assert parse_episode_input("150, 150-152") == [150, 151, 152]


def test_empty_string():
    assert parse_episode_input("") == []


def test_whitespace_only():
    assert parse_episode_input("   ") == []


def test_backwards_range_raises():
    with pytest.raises(ValueError, match="200-150"):
        parse_episode_input("200-150")


def test_non_numeric_raises():
    with pytest.raises(ValueError, match="abc"):
        parse_episode_input("abc")


def test_negative_number_raises():
    with pytest.raises(ValueError, match="-5"):
        parse_episode_input("-5")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parse_episode_input.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_episode_input'`

- [ ] **Step 3: Implement `parse_episode_input()`**

Add to `src/pep_oracle/server.py` after the `IngestRequest` class (after line 43):

```python
def parse_episode_input(s: str) -> list[int]:
    """Parse a string like '150-200, 210, 215' into a sorted list of episode numbers.

    Raises ValueError on invalid tokens (non-numeric, backwards ranges).
    """
    s = s.strip()
    if not s:
        return []
    nums: set[int] = set()
    for token in s.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start = int(parts[0].strip())
                end = int(parts[1].strip())
            except ValueError:
                raise ValueError(f"Invalid range: {token}")
            if start < 0 or end < 0:
                raise ValueError(f"Invalid range: {token}")
            if start > end:
                raise ValueError(f"Invalid range: {token}")
            nums.update(range(start, end + 1))
        else:
            try:
                n = int(token)
            except ValueError:
                raise ValueError(f"Invalid episode number: {token}")
            if n < 0:
                raise ValueError(f"Invalid episode number: {token}")
            nums.add(n)
    return sorted(nums)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parse_episode_input.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_parse_episode_input.py src/pep_oracle/server.py
git commit -m "feat: add parse_episode_input() for range/comma episode strings"
```

---

### Task 2: Wire `episode_input` into the ingest endpoint

**Files:**
- Modify: `src/pep_oracle/server.py:40-43` (IngestRequest)
- Modify: `src/pep_oracle/server.py:207-211` (api_ingest)
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_server.py` after `test_episodes_includes_stale_field`:

```python
def test_ingest_parses_episode_input(client_and_collection):
    """POST /ingest with episode_input parses the string into episode numbers."""
    client, collection = client_and_collection

    with patch("pep_oracle.server.ingest_all", return_value={"processed": 0, "skipped": 3, "failed": 0}) as mock_ingest:
        resp = client.post("/ingest", json={"episode_input": "1-2"})

    assert resp.status_code == 200
    call_kwargs = mock_ingest.call_args[1]
    assert sorted(call_kwargs["episode_numbers"]) == [1, 2]


def test_ingest_invalid_episode_input(client_and_collection):
    """POST /ingest with invalid episode_input returns 400."""
    client, _ = client_and_collection
    resp = client.post("/ingest", json={"episode_input": "abc"})
    assert resp.status_code == 400
    assert "Invalid" in resp.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::test_ingest_parses_episode_input tests/test_server.py::test_ingest_invalid_episode_input -v`
Expected: FAIL — `episode_input` field not recognized by Pydantic

- [ ] **Step 3: Add `episode_input` field and wire it up**

In `src/pep_oracle/server.py`, update `IngestRequest` (line 40-43):

```python
class IngestRequest(BaseModel):
    force: bool = False
    episode_numbers: list[int] = []
    episode_input: str = ""
    diarize: bool = False
```

In `api_ingest()`, add parsing before the `_run()` definition (after line 188, before the `async def _run():` line):

```python
    # Parse episode_input and merge with episode_numbers
    try:
        parsed = parse_episode_input(req.episode_input)
    except ValueError as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(e)})
    merged = sorted(set(req.episode_numbers + parsed))
```

Then update the `ingest_all` call (line 211) to use `merged` instead of `req.episode_numbers`:

```python
            result = await asyncio.to_thread(
                ingest_all,
                force=req.force,
                confirm_cost=False,
                episode_numbers=merged or None,
                diarize=req.diarize,
                progress_callback=_progress,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py::test_ingest_parses_episode_input tests/test_server.py::test_ingest_invalid_episode_input -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/server.py tests/test_server.py
git commit -m "feat: wire episode_input string parsing into POST /ingest"
```

---

### Task 3: Remove "newer than latest" filter

**Files:**
- Modify: `src/pep_oracle/server.py:168-170`
- Modify: `tests/test_server.py` (add test)

- [ ] **Step 1: Write the test**

Add to `tests/test_server.py`:

```python
def test_topics_returns_all_uningested_episodes(client_and_collection):
    """The /topics endpoint should return ALL uningested episodes, not just newer-than-latest."""
    client, collection = client_and_collection

    # Ingest only episode 2 (creates a gap: 1 is older and uningested)
    _ingest_chunk(collection, "guid-2", 2)

    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    resp = client.get("/topics")
    data = resp.json()
    not_ingested = data["not_ingested_episodes"]

    # Episode 1 is older than 2 but should still appear; episode 3 is newer
    assert 1 in not_ingested
    assert 3 in not_ingested
    assert 2 not in not_ingested
```

Note: This test requires that the `client_and_collection` fixture also patches `TOPICS_PATH`. Add this patch to the fixture's `patches` list:

```python
patch("pep_oracle.server.TOPICS_PATH", tmp_path / "topics.json"),
```

And add `tmp_path` to the fixture signature:

```python
@pytest.fixture()
def client_and_collection(tmp_path):
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_topics_returns_all_uningested_episodes -v`
Expected: FAIL — episode 1 not in `not_ingested` (filtered out by current code)

- [ ] **Step 3: Remove the filter**

In `src/pep_oracle/server.py`, delete lines 168-170:

```python
    if ingested_eps:
        latest_ingested = max(ingested_eps)
        not_ingested = [ep for ep in not_ingested if ep > latest_ingested]
```

The function should go from:

```python
    not_ingested = sorted(feed_eps - ingested_eps)
    if ingested_eps:
        latest_ingested = max(ingested_eps)
        not_ingested = [ep for ep in not_ingested if ep > latest_ingested]
    return {"episodes": topic_episodes, "not_ingested_episodes": not_ingested}
```

To:

```python
    not_ingested = sorted(feed_eps - ingested_eps)
    return {"episodes": topic_episodes, "not_ingested_episodes": not_ingested}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py::test_topics_returns_all_uningested_episodes -v`
Expected: PASS

- [ ] **Step 5: Run all server tests to check for regressions**

Run: `uv run pytest tests/test_server.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/server.py tests/test_server.py
git commit -m "fix: return all uningested episodes from /topics, not just newer-than-latest"
```

---

### Task 4: Redesign the ingest banner HTML and CSS

**Files:**
- Modify: `src/pep_oracle/web/index.html` (HTML structure and CSS)

- [ ] **Step 1: Replace the banner HTML**

Replace the current banner HTML (lines 197-200):

```html
  <div id="ingest-banner">
    <span class="ingest-text"></span>
    <button id="ingest-btn" type="button">Ingest now</button>
  </div>
```

With:

```html
  <div id="ingest-banner">
    <div class="ingest-summary" id="ingest-summary"></div>
    <div class="ingest-controls">
      <button id="ingest-latest-btn" type="button">Ingest latest</button>
      <input type="text" id="ingest-input" placeholder="e.g. 150-200, 210, 215">
      <button id="ingest-custom-btn" type="button">Ingest</button>
    </div>
    <div class="ingest-progress" id="ingest-progress" style="display:none"></div>
  </div>
```

- [ ] **Step 2: Update the CSS**

Replace the existing `#ingest-banner` CSS block (lines 158-181) with:

```css
  #ingest-banner {
    display: none;
    background: #fffbeb;
    border: 1px solid #fbbf24;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 12px;
    font-size: 0.85rem;
    color: #92400e;
    gap: 8px;
    flex-direction: column;
  }
  #ingest-banner .ingest-summary { line-height: 1.5; }
  #ingest-banner .ingest-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  #ingest-banner .ingest-controls button {
    padding: 6px 14px;
    font-size: 0.82rem;
    background: #f59e0b;
    border-radius: 6px;
    white-space: nowrap;
  }
  #ingest-banner .ingest-controls button:hover { background: #d97706; }
  #ingest-banner #ingest-input {
    padding: 6px 10px;
    font-size: 0.82rem;
    border: 1px solid #fbbf24;
    border-radius: 6px;
    flex: 1;
    min-width: 120px;
    outline: none;
    color: #92400e;
    background: #fffdf5;
  }
  #ingest-banner #ingest-input:focus { border-color: #d97706; }
  #ingest-banner .ingest-progress {
    font-style: italic;
    color: #b45309;
  }
```

- [ ] **Step 3: Commit**

```bash
git add src/pep_oracle/web/index.html
git commit -m "refactor: redesign ingest banner HTML/CSS for multi-control layout"
```

---

### Task 5: Rewire banner JavaScript

**Files:**
- Modify: `src/pep_oracle/web/index.html` (JS section)

- [ ] **Step 1: Update element references**

Replace the old element references (around lines 231-233):

```javascript
  const ingestBanner = document.getElementById("ingest-banner");
  const ingestBannerText = ingestBanner.querySelector(".ingest-text");
  const ingestBtn = document.getElementById("ingest-btn");
```

With:

```javascript
  const ingestBanner = document.getElementById("ingest-banner");
  const ingestSummary = document.getElementById("ingest-summary");
  const ingestLatestBtn = document.getElementById("ingest-latest-btn");
  const ingestCustomBtn = document.getElementById("ingest-custom-btn");
  const ingestInput = document.getElementById("ingest-input");
  const ingestProgress = document.getElementById("ingest-progress");
```

- [ ] **Step 2: Add the range collapse helper**

Add after the element references:

```javascript
  function collapseRanges(nums) {
    if (nums.length === 0) return "";
    const sorted = [...nums].sort((a, b) => a - b);
    const parts = [];
    let start = sorted[0], end = sorted[0];
    for (let i = 1; i < sorted.length; i++) {
      if (sorted[i] === end + 1) {
        end = sorted[i];
      } else {
        parts.push(start === end ? "" + start : start + "-" + end);
        start = end = sorted[i];
      }
    }
    parts.push(start === end ? "" + start : start + "-" + end);
    return parts.join(", ");
  }
```

- [ ] **Step 3: Update `renderTopics()` banner section**

Replace the existing banner logic inside `renderTopics()` (the `if (notIngestedEpisodes.length > 0)` block, around lines 501-509):

```javascript
    if (notIngestedEpisodes.length > 0) {
      ingestSummary.textContent = "Uningested: " + collapseRanges(notIngestedEpisodes);
      ingestBanner.style.display = "flex";
    } else {
      ingestBanner.style.display = "none";
    }
```

- [ ] **Step 4: Replace the old ingest button handler**

Remove the old `ingestBtn.addEventListener("click", ...)` block (around lines 524-548). Replace with two new handlers:

```javascript
  function startIngestion(body) {
    ingestLatestBtn.disabled = true;
    ingestCustomBtn.disabled = true;
    ingestInput.disabled = true;
    ingestProgress.style.display = "";
    ingestProgress.textContent = "Starting\u2026";

    fetch("/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(resp => resp.json())
      .then(data => {
        if (data.status === "already_running" || data.status === "started") {
          pollIngestion();
          startFreshnessPolling();
        } else if (data.detail) {
          ingestProgress.textContent = "Error: " + data.detail;
          ingestLatestBtn.disabled = false;
          ingestCustomBtn.disabled = false;
          ingestInput.disabled = false;
        }
      })
      .catch(() => {
        ingestProgress.textContent = "Request failed.";
        ingestLatestBtn.disabled = false;
        ingestCustomBtn.disabled = false;
        ingestInput.disabled = false;
      });
  }

  ingestLatestBtn.addEventListener("click", () => {
    const latest = Math.max(...notIngestedEpisodes);
    startIngestion({ episode_numbers: [latest], diarize: true });
  });

  ingestCustomBtn.addEventListener("click", () => {
    const val = ingestInput.value.trim();
    if (!val) return;
    startIngestion({ episode_input: val, diarize: true });
  });
```

- [ ] **Step 5: Update `pollIngestion()` to use new elements**

In the `pollIngestion()` function, update references. Replace the body of the `if (!data.running)` branch (the success/failure handling). The current code references `ingestBannerText` and `ingestBtn` — update to use the new elements:

In the error branch, replace:
```javascript
            ingestBannerText.textContent = "Ingestion failed: " + data.last_result.error;
            ingestBtn.style.display = "";
            ingestBtn.disabled = false;
            ingestBtn.textContent = "Retry";
```

With:
```javascript
            ingestProgress.textContent = "Ingestion failed: " + data.last_result.error;
            ingestLatestBtn.disabled = false;
            ingestCustomBtn.disabled = false;
            ingestInput.disabled = false;
```

In the success branch, keep the existing chip cleanup and cache refresh, but replace the banner hide with a full reset:
```javascript
            ingestBanner.style.display = "none";
```
This line stays the same — the next `loadTopics()` call will re-evaluate and re-show the banner if gaps remain.

In the `else` (still running) branch, replace:
```javascript
          ingestBannerText.textContent = progressText;
```

With:
```javascript
          ingestProgress.textContent = progressText;
```

- [ ] **Step 6: Update `checkIngestionOnLoad()`**

In `checkIngestionOnLoad()`, update the running branch. Replace:
```javascript
        ingestBtn.style.display = "none";
        ingestBanner.style.display = "flex";
        ingestBannerText.textContent = "Ingesting\u2026";
```

With:
```javascript
        ingestBanner.style.display = "flex";
        ingestLatestBtn.disabled = true;
        ingestCustomBtn.disabled = true;
        ingestInput.disabled = true;
        ingestProgress.style.display = "";
        ingestProgress.textContent = "Ingesting\u2026";
```

- [ ] **Step 7: Commit**

```bash
git add src/pep_oracle/web/index.html
git commit -m "feat: rewire ingest banner JS for latest-button and text input"
```

---

### Task 6: Update Playwright tests

**Files:**
- Modify: `tests/test_web_episodes.py`

- [ ] **Step 1: Update existing ingest banner tests**

The test `test_ingest_banner_visible_when_not_ingested` should verify the new layout. Replace its assertions:

```python
def test_ingest_banner_visible_when_not_ingested(server_with_collection, browser):
    """The ingest banner should appear with uningested summary and controls."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector("#ingest-banner", state="visible", timeout=15000)

    summary = page.text_content("#ingest-summary")
    assert "Uningested:" in summary

    assert page.locator("#ingest-latest-btn").is_visible()
    assert page.locator("#ingest-input").is_visible()
    assert page.locator("#ingest-custom-btn").is_visible()

    page.close()
```

- [ ] **Step 2: Add test for range-collapsed summary**

Add a new test:

```python
def test_ingest_summary_collapses_ranges(server_with_collection, browser):
    """Uningested summary should collapse consecutive episodes into ranges."""
    base_url, collection = server_with_collection

    # Ingest episodes 2 and 4 to create gaps: 1, 3, 5 uningested
    _ingest_into_collection(collection, "guid-2", 2)
    _ingest_into_collection(collection, "guid-4", 4)

    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector("#ingest-banner", state="visible", timeout=15000)

    summary = page.text_content("#ingest-summary")
    # Episodes 1, 3, 5 are uningested — no consecutive runs, so no ranges
    assert "1" in summary
    assert "3" in summary
    assert "5" in summary

    page.close()
```

- [ ] **Step 3: Add test for "ingest latest" button**

```python
def test_ingest_latest_button_targets_newest(server_with_collection, browser):
    """The 'Ingest latest' button should target the highest uningested episode number."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector("#ingest-latest-btn", timeout=15000)

    # Intercept the /ingest POST to check what it sends
    request_bodies = []
    page.on("request", lambda req: request_bodies.append(req.post_data) if req.url.endswith("/ingest") and req.method == "POST" else None)

    page.locator("#ingest-latest-btn").click()
    page.wait_for_timeout(500)

    assert len(request_bodies) == 1
    import json
    body = json.loads(request_bodies[0])
    assert body["episode_numbers"] == [5]

    page.close()
```

- [ ] **Step 4: Add test for custom input**

```python
def test_ingest_custom_sends_episode_input(server_with_collection, browser):
    """The custom ingest input should send episode_input string to the server."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector("#ingest-input", timeout=15000)

    request_bodies = []
    page.on("request", lambda req: request_bodies.append(req.post_data) if req.url.endswith("/ingest") and req.method == "POST" else None)

    page.fill("#ingest-input", "1-3")
    page.locator("#ingest-custom-btn").click()
    page.wait_for_timeout(500)

    assert len(request_bodies) == 1
    import json
    body = json.loads(request_bodies[0])
    assert body["episode_input"] == "1-3"

    page.close()
```

- [ ] **Step 5: Update `test_ingest_banner_hidden_when_all_ingested`**

This test should still pass as-is since the banner hide logic is unchanged (`ingestBanner.style.display = "none"` when `notIngestedEpisodes.length === 0`). Run it to confirm:

Run: `uv run pytest tests/test_web_episodes.py::test_ingest_banner_hidden_when_all_ingested -v`
Expected: PASS (no changes needed)

- [ ] **Step 6: Remove the old `test_not_ingested_chip_tooltip` if it references old banner structure**

Check if it still passes. The tooltip test checks `chip.get_attribute("title")` which is about topic chips, not the banner — it should still pass unchanged.

Run: `uv run pytest tests/test_web_episodes.py -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add tests/test_web_episodes.py
git commit -m "test: update Playwright tests for redesigned ingest banner"
```

---

### Task 7: Manual smoke test

- [ ] **Step 1: Restart the server**

```bash
pkill -f pep-oracle-server; sleep 1; uv run pep-oracle-server > /tmp/pep-oracle-server.log 2>&1 &
```

- [ ] **Step 2: Open the web UI and verify**

Open `http://localhost:8000` in a browser. Verify:
- The banner shows with "Uningested: ..." listing all uningested episodes (including older ones)
- "Ingest latest" button is visible
- Text input with placeholder is visible
- "Ingest" button beside the input is visible
- The uningested list collapses consecutive numbers into ranges

- [ ] **Step 3: Run the full non-Playwright test suite**

```bash
uv run pytest --ignore=tests/test_web_episodes.py --ignore=tests/test_web_conversation.py --ignore=tests/test_web_live.py -x -q
```

Expected: All pass
