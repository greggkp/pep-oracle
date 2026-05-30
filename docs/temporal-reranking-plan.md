# Intent-gated temporal reranking — implementation spec

## Context

PEP mixes point-in-time reporting, historical recounting, speculation, and
retrospective updates. Plain semantic retrieval ignores *when* a thing was said
and whether it's been superseded, so it can surface a stale June prediction as
settled fact (the "outdated information harms RAG" failure, benchmarked by HoH /
ConflictBank).

The 2025 literature's key finding: a **global** recency prior backfires — over-
decaying makes old-but-relevant content statistically unretrievable (the
"temporal event horizon"). So recency must be **conditional on query intent**,
not always-on. pep-oracle already has the right bones — a Haiku preprocessor that
extracts `prefer_recent`, and `store._apply_recency_boost`. This spec generalizes
that binary flag into an intent taxonomy and a reranking layer that picks a
strategy per intent. Scope: the `/ask` path (which has the preprocessor). The MCP
tool stays client-driven (its `episode_number`/`corpus` already cover "latest").

## Temporal intent taxonomy

`preprocess_query` gains a `temporal_intent` field (replaces the binary
`prefer_recent`); explicit `after_date`/`before_date` stay orthogonal (hard
filters from phrases like "in June").

| intent | example | retrieval/rerank strategy | context order |
|---|---|---|---|
| `current` | "latest on Iran", present-tense evolving | soft exponential recency decay (NO hard date cut) | newest-first |
| `historical` | "what did they say about X back in June" | pure relevance within date window | newest-first |
| `evolution` | "how has their view on X changed" | temporal-spread: ≤1–2 hits per episode, cover the timeline | chronological |
| `prediction` | "what did they predict about X; did it pan out" | pure relevance, no decay (old speculation + later updates both surface) | chronological |
| `timeless` | "who is Dr Dave" | pure relevance | relevance |

Deliberate change: `current` drops today's hard `after_date = today-60d` floor and
uses a **soft** decay instead, so a slightly older but strongly relevant chunk can
still appear (avoids the event-horizon hard cut; matches the "soft recency prior >
hard cut" literature).

## Architecture / data flow

Today: `ask` → `preprocess_query` → embed → `_retrieve_relaxing_filters` →
`store.query(..., recency_weight=0.3 if prefer_recent)` → `build_context`
(newest-first) → Claude.

New: keep retrieval generic, move temporal logic to a pure, testable module.

1. `preprocess_query` returns `temporal_intent`.
2. `ask` retrieves a **candidate pool** (e.g. `top_k * 4`) via `store.query` with
   `recency_weight=0` (pure similarity + hard date/episode/speaker filters only).
3. New `temporal.py` selects + orders the final `top_k` from candidates per intent.
4. `build_context(results, order=...)` renders in the chosen order.
5. A short per-intent instruction is appended to the prompt (see below).

## New module: `src/pep_oracle/temporal.py` (pure functions)

```python
HALF_LIFE_DAYS = 21          # tune via eval; only used for `current`
CANDIDATE_MULTIPLIER = 4     # pool size = top_k * this

def recency_score(date: str, today: date, half_life_days=HALF_LIFE_DAYS) -> float:
    """0.5 ** (age_days / half_life); 1.0 today, →0 for old."""

def select_for_intent(candidates: list[dict], intent: str, top_k: int,
                      today: date) -> tuple[list[dict], str]:
    """Return (final_results, order) where order in {newest_first, chronological,
    relevance}. candidates carry 'distance' and 'episode_date'."""
```

- `current`: blend `sim=(1-distance)` with `recency_score`, take top_k, newest-first.
- `historical`/`timeless`: top_k by similarity (no decay).
- `evolution`: group candidates by episode, keep the best 1–2 per episode, take
  the most-relevant episodes up to a coverage cap, order chronologically.
- `prediction`: top_k by similarity across the full timeline (no decay), order
  chronologically so the model reads prediction → later outcome.

This replaces the rank-based `store._apply_recency_boost` with date-based
exponential decay; `store.query`'s `recency_weight` path can be retired once
`ask` drives reranking (keep the param for the CLI/back-compat, default 0).

## Changes by file

- `query.py`:
  - `preprocess_query`: extend the Haiku prompt + JSON with `temporal_intent`
    (5 enum values) + examples; keep `after_date`/`before_date`. Map legacy
    `prefer_recent` → `current` for safety.
  - `ask`: fetch candidate pool (recency_weight=0, larger k), call
    `temporal.select_for_intent`, pass `order` to `build_context`, append the
    per-intent instruction to the user message. Keep `_retrieve_relaxing_filters`
    (speaker/date relaxation) operating on the candidate fetch.
  - `build_context(results, speaker=None, order="newest_first")`.
  - `SYSTEM_PROMPT`: keep; add a tiny per-intent suffix dict, e.g. evolution →
    "Narrate how the discussion changed over time, citing dates in order";
    prediction → "State the prediction and whether later episodes confirm or
    revise it"; current → "Lead with the most recent take; flag superseded ones".
- `temporal.py`: new (above).
- `config.py`: `HALF_LIFE_DAYS`, `CANDIDATE_MULTIPLIER`.
- `server.py` `/ask`: unchanged (calls `do_ask`).

## MCP path (symmetric, via the same `temporal.py`)

The MCP tool has no server-side preprocessor by design — but its caller is a
frontier model with full conversation context, a *better* intent classifier than
Haiku. So we don't infer intent inside the tool (that would re-add the internal
LLM call we avoided); we **expose it as a parameter** and let the caller drive
the same reranking:

```
search_pep(query, top_k=5, episode_number=None,
           intent=None, after_date=None, before_date=None)
```

- `intent`: one of the 5 enum values; `None` → pure relevance (no temporal bias,
  = today's behavior, so no regression on simple queries). The server runs
  `temporal.select_for_intent` and returns results **already ordered** for that
  intent (e.g. chronological for `evolution`/`prediction`), so the model reads
  them in the intended order.
- `after_date`/`before_date`: ISO hard filters for "in June 2025"-style scoping.
- `episode_number` and the `corpus` hint stay (cover "the latest episode").

Description guidance (appended after the load-bearing trigger): "For 'latest/now'
pass intent='current'; for 'how did X change over time' pass intent='evolution'
(results come back oldest→newest); for 'what did they predict' pass
intent='prediction'; for a date range pass after_date/before_date." This keeps
the tool thin (no internal LLM) while giving the caller the same temporal control
the `/ask` preprocessor gives Haiku. Single source of truth: both paths call
`temporal.select_for_intent`.

Add to `tests/test_mcp_server.py`: `intent` forwarded to the reranker; `evolution`
returns chronological order; default (`intent=None`) unchanged.

## Out of scope (future)

- **Claim-typing at ingest** (prediction/fact-as-of-date/retrospective metadata) —
  the heavier HoH/ConflictBank-motivated lift; do after this if eval shows gaps.
- **Temporal knowledge graph** (TG-RAG/T-GRAG) — only if "evolution" becomes a
  headline feature.

## Verification

- Unit (`tests/test_temporal.py`): `recency_score` decay (today=1.0, half-life→0.5,
  old→~0); `select_for_intent` — `current` puts recent+relevant first; `evolution`
  spreads across episodes in chronological order; `prediction` keeps an old
  high-relevance chunk that a decay would drop; `timeless`/`historical` = pure
  relevance.
- Integration (`tests/test_query.py`): mock Haiku to return each intent + mock
  `store.query`; assert `ask` selects the right strategy/order and that a stale
  chunk is (current) demoted vs (prediction/evolution) retained.
- Intent extraction: mock-Haiku tests for the 5 example phrases above.
- **Temporal eval set** (follow-up, HoH-style): a handful of questions whose
  correct answer changed over PEP's timeline (current-state, evolution,
  prediction-tracking), run live (`-m live`) to confirm the right epoch surfaces.
