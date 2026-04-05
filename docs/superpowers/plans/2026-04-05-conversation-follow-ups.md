# Conversation Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow users to ask follow-up questions in a conversational thread in the web UI, with full conversation context sent to Claude.

**Architecture:** Client-side conversation state (JS array of `{role, content}` objects). The `/ask` endpoint gains an optional `history` field. `query.py`'s `ask()` builds a multi-turn messages array for Claude. RAG retrieval uses only the current question. The preprocessor receives the last assistant reply to resolve references.

**Tech Stack:** Python (FastAPI, Anthropic SDK), vanilla JavaScript, Playwright for web tests.

**Spec:** `docs/superpowers/specs/2026-04-05-conversation-follow-ups-design.md`

---

### Task 1: Add `history` parameter to `query.py` `ask()`

**Files:**
- Test: `tests/test_query.py`
- Modify: `src/pep_oracle/query.py:156-199`

- [ ] **Step 1: Write failing test for multi-turn message building**

Add to `tests/test_query.py`:

```python
def test_ask_passes_history_to_claude():
    """ask() should build multi-turn messages from history + RAG-augmented current question."""
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Follow-up answer")]
    )

    history = [
        {"role": "user", "content": "What about tariffs?"},
        {"role": "assistant", "content": "In Episode 255, they discussed tariffs..."},
    ]

    with patch("pep_oracle.query.preprocess_query", return_value={
        "episode_numbers": [], "after_date": None, "before_date": None,
        "search_query": "EU response tariffs", "prefer_recent": False,
    }), patch("pep_oracle.query.embed_texts", return_value=[[0.1] * 10]), \
         patch("pep_oracle.query.get_client"), \
         patch("pep_oracle.query.get_collection"), \
         patch("pep_oracle.query.store_query", return_value=[{
            "episode_title": "Ep 255", "episode_number": 255,
            "episode_date": "2026-03-20", "start_time": 100.0,
            "end_time": 200.0, "text": "The EU responded to tariffs...",
         }]):
        from pep_oracle.query import ask
        result = ask(
            "What did Dr Dave think about the EU response?",
            anthropic_client=mock_anthropic,
            history=history,
        )

    assert result == "Follow-up answer"
    call_kwargs = mock_anthropic.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    # Should have 3 messages: 2 from history + 1 new (with RAG context)
    assert len(messages) == 3
    assert messages[0] == {"role": "user", "content": "What about tariffs?"}
    assert messages[1] == {"role": "assistant", "content": "In Episode 255, they discussed tariffs..."}
    assert messages[2]["role"] == "user"
    assert "TRANSCRIPT EXCERPTS" in messages[2]["content"]
    assert "What did Dr Dave think about the EU response?" in messages[2]["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_query.py::test_ask_passes_history_to_claude -v`
Expected: FAIL — `ask()` doesn't accept `history` parameter.

- [ ] **Step 3: Write failing test for backward compatibility (no history)**

Add to `tests/test_query.py`:

```python
def test_ask_without_history_sends_single_message():
    """ask() without history should send a single user message (backward compat)."""
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Single answer")]
    )

    with patch("pep_oracle.query.preprocess_query", return_value={
        "episode_numbers": [], "after_date": None, "before_date": None,
        "search_query": "tariffs", "prefer_recent": False,
    }), patch("pep_oracle.query.embed_texts", return_value=[[0.1] * 10]), \
         patch("pep_oracle.query.get_client"), \
         patch("pep_oracle.query.get_collection"), \
         patch("pep_oracle.query.store_query", return_value=[{
            "episode_title": "Ep 255", "episode_number": 255,
            "episode_date": "2026-03-20", "start_time": 100.0,
            "end_time": 200.0, "text": "Tariff discussion...",
         }]):
        from pep_oracle.query import ask
        result = ask("What about tariffs?", anthropic_client=mock_anthropic)

    assert result == "Single answer"
    call_kwargs = mock_anthropic.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
```

- [ ] **Step 4: Implement history support in `ask()`**

In `src/pep_oracle/query.py`, modify the `ask()` function:

```python
def ask(
    question: str,
    top_k: int = 10,
    model: str = QUERY_MODEL,
    anthropic_client: anthropic.Anthropic | None = None,
    openai_client=None,
    history: list[dict] | None = None,
) -> str:
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Pre-process to extract filters
    filters = preprocess_query(question, anthropic_client=anthropic_client)

    # Embed the search query (may be rewritten by pre-processor)
    query_embedding = embed_texts([filters["search_query"]], client=openai_client)[0]

    # Retrieve relevant chunks with filters
    client = get_client()
    collection = get_collection(client)
    recency_weight = 0.3 if filters.get("prefer_recent") else 0.0
    results = store_query(
        collection,
        query_embedding,
        top_k=top_k,
        episode_numbers=filters["episode_numbers"] or None,
        after_date=filters["after_date"],
        before_date=filters["before_date"],
        recency_weight=recency_weight,
    )

    if not results:
        return "No relevant content found. Have you ingested any episodes yet?"

    # Build prompt and call Claude
    context = build_context(results)
    user_message = f"TRANSCRIPT EXCERPTS:\n\n{context}\n\nQUESTION: {question}"

    messages = list(history or []) + [{"role": "user", "content": user_message}]

    response = anthropic_client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_query.py -v`
Expected: ALL PASS (including existing tests).

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/query.py tests/test_query.py
git commit -m "feat: add history parameter to ask() for multi-turn conversations"
```

---

### Task 2: Pass conversation context to preprocessor

**Files:**
- Test: `tests/test_query.py`
- Modify: `src/pep_oracle/query.py:85-153`

- [ ] **Step 1: Write failing test for preprocessor context**

Add to `tests/test_query.py`:

```python
def test_preprocess_query_receives_conversation_context():
    """When last_assistant_reply is provided, it should be included in the prompt."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "EU tariff response", "prefer_recent": false}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query(
            "What about the EU response?",
            anthropic_client=mock_client,
            last_assistant_reply="In Episode 255, they discussed new tariff announcements...",
        )

    # Check that the prompt sent to Haiku includes the conversation context
    call_kwargs = mock_client.messages.create.call_args
    prompt_text = call_kwargs.kwargs["messages"][0]["content"]
    assert "In Episode 255, they discussed new tariff announcements" in prompt_text
    assert result["search_query"] == "EU tariff response"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_query.py::test_preprocess_query_receives_conversation_context -v`
Expected: FAIL — `preprocess_query()` doesn't accept `last_assistant_reply`.

- [ ] **Step 3: Implement conversation context in preprocessor**

In `src/pep_oracle/query.py`, modify `preprocess_query()` signature and prompt:

Add the parameter:

```python
def preprocess_query(
    question: str,
    anthropic_client: anthropic.Anthropic | None = None,
    last_assistant_reply: str | None = None,
) -> dict:
```

After the `prompt = PREPROCESS_PROMPT.format(...)` call, add context injection:

```python
    if last_assistant_reply:
        prompt = prompt.replace(
            f"Question: {question}",
            f"Previous assistant reply (for context): {last_assistant_reply}\n\nQuestion: {question}",
        )
```

Then update `ask()` to pass the last assistant reply:

```python
    # Extract the last assistant reply from history for context
    last_assistant_reply = None
    if history:
        for msg in reversed(history):
            if msg["role"] == "assistant":
                last_assistant_reply = msg["content"]
                break

    # Pre-process to extract filters
    filters = preprocess_query(
        question,
        anthropic_client=anthropic_client,
        last_assistant_reply=last_assistant_reply,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_query.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/query.py tests/test_query.py
git commit -m "feat: pass conversation context to preprocessor for reference resolution"
```

---

### Task 3: Update `/ask` endpoint to accept `history`

**Files:**
- Test: `tests/test_server.py`
- Modify: `src/pep_oracle/server.py:26-27,64-67`

- [ ] **Step 1: Write failing test for `/ask` with history**

Add to `tests/test_server.py`:

```python
def test_ask_passes_history_to_do_ask(client_and_collection):
    client, _ = client_and_collection
    history = [
        {"role": "user", "content": "What about tariffs?"},
        {"role": "assistant", "content": "They discussed tariffs in Ep 255..."},
    ]
    with patch("pep_oracle.server.do_ask", return_value="Follow-up answer") as mock_ask:
        resp = client.post("/ask", json={
            "question": "What about the EU?",
            "history": history,
        })
    assert resp.status_code == 200
    assert resp.json()["answer"] == "Follow-up answer"
    mock_ask.assert_called_once_with(
        "What about the EU?", top_k=10, history=history,
    )


def test_ask_without_history_passes_empty_list(client_and_collection):
    client, _ = client_and_collection
    with patch("pep_oracle.server.do_ask", return_value="Answer") as mock_ask:
        resp = client.post("/ask", json={"question": "What is PEP?"})
    assert resp.status_code == 200
    mock_ask.assert_called_once_with("What is PEP?", top_k=10, history=[])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::test_ask_passes_history_to_do_ask tests/test_server.py::test_ask_without_history_passes_empty_list -v`
Expected: FAIL — `AskRequest` has no `history` field, `do_ask` not called with `history`.

- [ ] **Step 3: Implement server changes**

In `src/pep_oracle/server.py`, update `AskRequest`:

```python
class AskRequest(BaseModel):
    question: str
    top_k: int = 10
    history: list[dict] = []
```

Update the `api_ask` handler:

```python
@app.post("/ask")
async def api_ask(req: AskRequest):
    answer = await asyncio.to_thread(do_ask, req.question, top_k=req.top_k, history=req.history)
    return {"answer": answer}
```

- [ ] **Step 4: Update existing test to match new calling convention**

The existing `test_ask_returns_answer` test uses `patch("pep_oracle.server.do_ask", return_value="Test answer")` which replaces the function entirely — it will still pass since it doesn't check call args. No change needed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/server.py tests/test_server.py
git commit -m "feat: accept history in /ask endpoint for multi-turn conversations"
```

---

### Task 4: Rewrite web UI with chat bubble layout

**Files:**
- Modify: `src/pep_oracle/web/index.html`

This is the largest task — it replaces the single-answer UI with a conversational thread. No test-first here since it's pure frontend; the Playwright test in Task 5 covers it.

- [ ] **Step 1: Add CSS for chat bubbles, collapsed threads, and new conversation button**

In `src/pep_oracle/web/index.html`, replace the `#answer` and related CSS (lines 58-72) with:

```css
  #thread {
    display: none;
    background: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 8px;
    max-height: 60vh;
    overflow-y: auto;
  }
  .bubble-row { display: flex; margin-bottom: 10px; }
  .bubble-row.user { justify-content: flex-end; }
  .bubble-row.assistant { justify-content: flex-start; }
  .bubble {
    padding: 10px 14px;
    border-radius: 14px;
    max-width: 80%;
    line-height: 1.6;
    word-wrap: break-word;
  }
  .bubble.user {
    background: #4a90d9;
    color: white;
    border-bottom-right-radius: 4px;
  }
  .bubble.assistant {
    background: #f0f0f0;
    color: #1a1a1a;
    border-bottom-left-radius: 4px;
  }
  .bubble.assistant h1, .bubble.assistant h2, .bubble.assistant h3 { margin: 12px 0 6px; }
  .bubble.assistant p { margin-bottom: 8px; }
  .bubble.assistant ul, .bubble.assistant ol { margin-bottom: 8px; padding-left: 20px; }
  .bubble.assistant blockquote { border-left: 3px solid #ddd; padding-left: 10px; color: #555; margin-bottom: 8px; }
  .bubble.assistant code { background: #e0e0e0; padding: 2px 4px; border-radius: 3px; font-size: 0.9em; }
  #new-convo { display: none; text-align: center; margin-bottom: 24px; }
  #new-convo a {
    color: #4a90d9;
    font-size: 0.85rem;
    cursor: pointer;
    text-decoration: none;
    border-bottom: 1px dashed #4a90d9;
  }
  #new-convo a:hover { color: #3a7bc8; }
  .collapsed-thread {
    margin-bottom: 10px;
    padding: 8px 12px;
    background: #f5f5f5;
    border: 1px solid #e8e8e8;
    border-radius: 6px;
    opacity: 0.6;
    cursor: pointer;
    font-size: 0.82rem;
    color: #888;
  }
  .collapsed-thread:hover { opacity: 0.8; }
  .collapsed-thread .collapsed-body { display: none; margin-top: 8px; opacity: 0.7; }
  .collapsed-thread.expanded .collapsed-body { display: block; }
```

- [ ] **Step 2: Update HTML structure**

Replace `<div id="answer"></div>` (line 107) with:

```html
  <div id="collapsed-area"></div>
  <div id="thread"></div>
  <div id="new-convo"><a id="new-convo-btn">+ New conversation</a></div>
```

- [ ] **Step 3: Rewrite JavaScript for conversation state management**

Replace the entire `<script>` block with the following. Note: `innerHTML` is used for rendering Markdown from `marked.parse()` — the content comes from our own server (Claude API responses), not from untrusted user input. This matches the existing pattern in the codebase.

```javascript
  const form = document.getElementById("ask-form");
  const questionEl = document.getElementById("question");
  const submitBtn = document.getElementById("submit-btn");
  const topK = document.getElementById("top-k");
  const statusBar = document.getElementById("status-bar");
  const coverageEl = document.getElementById("coverage");
  const threadEl = document.getElementById("thread");
  const newConvoEl = document.getElementById("new-convo");
  const newConvoBtn = document.getElementById("new-convo-btn");
  const collapsedArea = document.getElementById("collapsed-area");
  const topicsEl = document.getElementById("topics");

  let conversationHistory = [];
  let collapsedThreads = [];

  function addBubble(role, content) {
    const row = document.createElement("div");
    row.className = "bubble-row " + role;
    const bubble = document.createElement("div");
    bubble.className = "bubble " + role;
    if (role === "assistant") {
      /* marked.parse renders trusted server markdown — server controls the content */
      bubble.innerHTML = marked.parse(content);
    } else {
      bubble.textContent = content;
    }
    row.appendChild(bubble);
    threadEl.appendChild(row);
    threadEl.scrollTop = threadEl.scrollHeight;
    return bubble;
  }

  function updateUIState() {
    const active = conversationHistory.length > 0;
    threadEl.style.display = active ? "block" : "none";
    newConvoEl.style.display = active ? "block" : "none";
    topicsEl.style.display = active ? "none" : topicsEl.dataset.loaded ? "flex" : "none";
    questionEl.placeholder = active ? "Ask a follow-up..." : "What have Chas and Dave said about...?";
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = questionEl.value.trim();
    if (!question) return;

    questionEl.value = "";
    addBubble("user", question);
    const pendingBubble = addBubble("assistant", "");
    pendingBubble.textContent = "Thinking...";
    pendingBubble.style.fontStyle = "italic";
    pendingBubble.style.color = "#888";
    submitBtn.disabled = true;
    updateUIState();

    try {
      const body = {
        question,
        top_k: parseInt(topK.value),
        history: conversationHistory,
      };
      conversationHistory.push({ role: "user", content: question });

      const resp = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();

      conversationHistory.push({ role: "assistant", content: data.answer });
      pendingBubble.style.fontStyle = "";
      pendingBubble.style.color = "";
      /* marked.parse renders trusted server markdown — server controls the content */
      pendingBubble.innerHTML = marked.parse(data.answer);
      threadEl.scrollTop = threadEl.scrollHeight;
    } catch (err) {
      pendingBubble.textContent = "Error: " + err.message;
      pendingBubble.style.color = "#c0392b";
      pendingBubble.style.fontStyle = "";
      // Remove the user message from history since the request failed
      conversationHistory.pop();
    } finally {
      submitBtn.disabled = false;
      questionEl.focus();
    }
  });

  newConvoBtn.addEventListener("click", () => {
    if (conversationHistory.length === 0) return;

    const firstUserMsg = conversationHistory.find(m => m.role === "user");
    const msgCount = conversationHistory.filter(m => m.role === "user").length;
    collapsedThreads.push({
      label: firstUserMsg ? firstUserMsg.content : "Conversation",
      count: msgCount,
      messages: [...conversationHistory],
    });

    renderCollapsed();
    conversationHistory = [];
    threadEl.innerHTML = "";
    updateUIState();
    questionEl.focus();
  });

  function renderCollapsed() {
    collapsedArea.innerHTML = "";
    collapsedThreads.forEach((thread, idx) => {
      const div = document.createElement("div");
      div.className = "collapsed-thread";
      const label = thread.label.length > 60 ? thread.label.slice(0, 60) + "..." : thread.label;

      const header = document.createElement("span");
      const arrow = document.createElement("span");
      arrow.className = "collapsed-arrow";
      arrow.textContent = "\u25b6";
      header.appendChild(arrow);
      header.appendChild(document.createTextNode(" Previous: " + label + " "));

      const count = document.createElement("span");
      count.style.color = "#aaa";
      count.textContent = "(" + thread.count + " question" + (thread.count !== 1 ? "s" : "") + ")";
      header.appendChild(count);
      div.appendChild(header);

      const body = document.createElement("div");
      body.className = "collapsed-body";
      thread.messages.forEach(msg => {
        const row = document.createElement("div");
        row.className = "bubble-row " + msg.role;
        const bubble = document.createElement("div");
        bubble.className = "bubble " + msg.role;
        if (msg.role === "assistant") {
          /* marked.parse renders trusted server markdown — server controls the content */
          bubble.innerHTML = marked.parse(msg.content);
        } else {
          bubble.textContent = msg.content;
        }
        row.appendChild(bubble);
        body.appendChild(row);
      });
      div.appendChild(body);

      div.addEventListener("click", () => {
        div.classList.toggle("expanded");
        arrow.textContent = div.classList.contains("expanded") ? "\u25bc" : "\u25b6";
      });

      collapsedArea.appendChild(div);
    });
  }

  async function loadStatus() {
    try {
      const resp = await fetch("/status");
      const s = await resp.json();
      const sizeMB = (s.db_size_bytes / 1_000_000).toFixed(1);
      statusBar.textContent = s.ingested_count + "/" + s.feed_count +
        " episodes ingested | " + s.chunk_count + " excerpts | " + sizeMB + " MB";

      if (s.ingested_count === 0) {
        coverageEl.textContent = "No episodes ingested yet";
      } else if (s.earliest_episode && s.latest_episode && s.earliest_date && s.latest_date) {
        const fmtDate = (d) => {
          const dt = new Date(d + "T00:00:00");
          return dt.toLocaleDateString("en-US", { month: "short", year: "numeric" });
        };
        coverageEl.textContent = s.ingested_count + " episodes ingested (Ep " +
          s.earliest_episode + "\u2013" + s.latest_episode + ", " +
          fmtDate(s.earliest_date) + " \u2013 " + fmtDate(s.latest_date) + ")";
      } else {
        coverageEl.textContent = s.ingested_count + " episodes ingested";
      }
    } catch (err) {
      statusBar.textContent = "Could not load status";
      coverageEl.textContent = "";
    }
  }

  async function loadTopics() {
    try {
      const resp = await fetch("/topics");
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.topics || data.topics.length === 0) return;

      data.topics.forEach(t => {
        const chip = document.createElement("button");
        chip.className = "topic-chip";
        chip.textContent = t.topic;
        chip.title = "Ep " + t.episode_number;
        chip.addEventListener("click", () => {
          questionEl.value = t.question;
          questionEl.focus();
        });
        topicsEl.appendChild(chip);
      });
      topicsEl.dataset.loaded = "true";
      topicsEl.style.display = "flex";
    } catch (e) {
      // Silent failure — topics are optional
    }
  }

  loadStatus();
  loadTopics();
```

- [ ] **Step 4: Remove the old `#answer` CSS and `.loading`/`.error` classes**

Remove these CSS rules which are no longer used:
- `#answer` block
- `#answer h1, #answer h2, ...` rules
- `.loading` class
- `.error` class

The `.error` styling for failed requests is now handled inline in the JS (`pendingBubble.style.color = "#c0392b"`).

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `uv run pytest tests/test_server.py tests/test_query.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/web/index.html
git commit -m "feat: rewrite web UI with chat bubble conversation thread"
```

---

### Task 5: Add Playwright tests for conversation flow

**Files:**
- Create: `tests/test_web_conversation.py`

- [ ] **Step 1: Write Playwright tests**

Create `tests/test_web_conversation.py`:

```python
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
        return "Follow-up answer about the EU response."

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
```

- [ ] **Step 2: Run the Playwright tests**

Run: `uv run pytest tests/test_web_conversation.py -v`
Expected: ALL PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_conversation.py
git commit -m "test: add Playwright tests for conversation follow-up flow"
```

---

### Task 6: Final integration check

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -x -q`
Expected: ALL PASS with no warnings related to our changes.

- [ ] **Step 2: Manual smoke test (if server available)**

Start the server with `uv run pep-oracle-server` and verify:
1. Page loads with topic chips visible
2. Submit a question — chat bubble appears
3. Submit a follow-up — second bubble pair appears, topic chips hidden
4. Click "+ New conversation" — thread collapses, chips reappear
5. Click collapsed thread — expands to show old messages
