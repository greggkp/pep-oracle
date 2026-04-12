# Preprocessor Full History for Pronoun Resolution — Design Spec

## Problem

Follow-up questions with pronouns ("what does he think about X?") fail because the preprocessor only sees the last assistant reply, not the user's original question. When a user asks about Pete Hegseth and then says "he", the preprocessor can't resolve "he" → "Pete Hegseth", so the search query misses the right chunks and the answer acts like it doesn't know who "he" is.

## Root Cause

In `query.py`, `ask()` extracts only `last_assistant_reply` from history (line 175-179) and passes it to `preprocess_query`. The preprocessor injects this as `"Previous assistant reply (for context): ..."` before the question. But the user's earlier questions — which contain the actual entity names — are not included.

## Solution

Pass the full conversation history to `preprocess_query` instead of just the last assistant reply. Format it as alternating User/Assistant lines and inject it before the question in the preprocessor prompt. Update the prompt to instruct Haiku to use this history to resolve pronouns and references when rewriting the search query.

## Changes to `query.py`

### `preprocess_query` signature

Replace `last_assistant_reply: str | None = None` with `history: list[dict] | None = None`.

### History formatting

When history is non-empty, format it as:

```
Conversation so far:
User: What did they say about Pete Hegseth?
Assistant: In Episode 254, they discussed...

Question: what does he think about tariffs?
```

This replaces the current `"Previous assistant reply (for context): ..."` injection.

### `PREPROCESS_PROMPT` update

Add after the existing instruction block (before the examples):

```
If conversation history is provided, use it to resolve pronouns and references \
in the question. For example, if the user previously asked about "Pete Hegseth" \
and now asks "what does he think?", rewrite the search query to include "Pete Hegseth".
```

Add one example with history context:

```
- Conversation: User asked about Pete Hegseth. Question: "what does he think about tariffs?" → search_query: "Pete Hegseth tariffs opinion"
```

### `ask()` call site

Change line 183 to pass `history=history` instead of `last_assistant_reply=last_assistant_reply`. Remove the `last_assistant_reply` extraction loop (lines 174-179).

## Test Changes

In `tests/test_query.py`:

- **Update** `test_preprocess_query_receives_conversation_context` (line 270): Change from passing `last_assistant_reply` to passing `history` (a list of user/assistant message dicts). Verify the prompt sent to Haiku contains both the user's question and the assistant's reply from history.
- **Add** `test_preprocess_query_resolves_pronouns_from_history`: Pass history where the user asked about "Pete Hegseth", then a follow-up "what does he think about tariffs?". Verify the prompt sent to Haiku contains "Pete Hegseth" from the history.
- **Update** `test_ask_passes_history_to_claude` (line 229): This test patches `preprocess_query` entirely, so it just needs the signature check updated — verify `history` kwarg is passed instead of `last_assistant_reply`.

## No Frontend Changes

The frontend already sends the full `history` array to `/ask`. The server already passes it to `ask()`. Only the internal plumbing from `ask()` to `preprocess_query` changes.

## Scope

- Modify: `src/pep_oracle/query.py` (~15 lines changed)
- Modify: `tests/test_query.py` (1 test updated, 1 test added)
