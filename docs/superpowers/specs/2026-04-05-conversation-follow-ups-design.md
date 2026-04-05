# Conversation Follow-ups Design

Allow users to ask follow-up questions in a conversational thread, or start a new conversation. Web UI first; CLI deferred.

## Decisions

- **Interface:** Web UI only (CLI later)
- **Context:** Full conversation history sent to Claude on every turn
- **Layout:** Chat bubbles — user messages right-aligned, Oracle answers left-aligned
- **New conversation:** Explicit "+ New conversation" link below the input
- **Old threads:** Collapse visually (greyed summary bar, expandable) — no server-side persistence
- **Topic chips:** Hidden during active conversation, reappear on new conversation
- **Architecture:** Client-side state only — no server sessions, no database changes
- **History feature:** Out of scope, deferred

## API Changes

### `POST /ask`

Request body adds an optional `history` field:

```json
{
  "question": "What did Dr Dave think about the EU response?",
  "top_k": 10,
  "history": [
    {"role": "user", "content": "What have they said about tariffs?"},
    {"role": "assistant", "content": "In Episode 255, Chas and Dr Dave..."}
  ]
}
```

- `history` is optional. Omit or send `[]` for a fresh query (backward compatible).
- Server passes `history` + new question as the `messages` array to Claude.
- RAG retrieval uses only the current question for embedding search — history is conversational context for Claude, not for vector search.
- Preprocessing (Haiku filter extraction) runs on the current question, with the last assistant reply as additional context to help resolve references like "tell me more about that."

## Changes to `query.py`

The `ask()` function gains an optional `history` parameter (list of `{role, content}` dicts).

Messages sent to Claude are built as:

```
[
  {role: "user", content: "What have they said about tariffs?"},
  {role: "assistant", content: "In Episode 255..."},
  {role: "user", content: "TRANSCRIPT EXCERPTS:\n...\n\nQUESTION: What did Dr Dave think about the EU response?"}
]
```

- Historical turns are passed through as-is.
- RAG context is injected into the final user message only.
- System prompt unchanged.
- Preprocessing receives the last assistant reply (if any) to resolve references.

## UI States

### 1. Empty state (initial load / after "New conversation")
- Header, subtitle, coverage stats
- Topic chips visible
- Input placeholder: "What have Chas and Dave said about...?"
- No conversation thread visible (except collapsed old threads if any)

### 2. Active conversation
- Chat bubble thread in a container (white background, bordered)
- User messages: right-aligned, blue background, white text
- Oracle messages: left-aligned, grey background, rendered Markdown
- Topic chips hidden
- Input placeholder: "Ask a follow-up..."
- "+ New conversation" link below the input

### 3. After "New conversation"
- Previous thread collapses into a greyed summary bar showing the first question and message count (e.g., "Previous: What have they said about tariffs recently? (2 messages)")
- Collapsed bar is expandable (click to reveal the old thread read-only — no interaction, just the bubbles)
- Topic chips reappear
- Input placeholder resets
- Conversation context cleared

## Client-side State

JavaScript maintains:
- `conversationHistory` — array of `{role, content}` objects
- `collapsedThreads` — array of previous conversations for collapsed UI

### Submit flow
1. Push user message to `conversationHistory`, render user bubble
2. Show "Thinking..." in a pending Oracle bubble
3. POST to `/ask` with `question` + `history` (prior turns, excluding current question)
4. On response, push assistant message to `conversationHistory`, render Markdown bubble

### New conversation flow
1. Snapshot `conversationHistory` into `collapsedThreads` (first user message as label, message count)
2. Clear `conversationHistory`
3. Render collapsed thread header
4. Show topic chips
5. Reset input placeholder

### Page refresh
Everything resets to empty state. No persistence.

## Testing

- **`query.py` unit tests:** Verify multi-turn message building with `history`, and backward-compatible single-turn without it.
- **Preprocessing unit test:** Verify last assistant reply passed as context to Haiku for reference resolution.
- **Server test:** Verify `/ask` accepts `history` field and passes it through.
- **Playwright test:** Submit question, get answer, submit follow-up, verify both bubbles render. Test "New conversation" clears thread and shows collapsed header, topic chips reappear.
