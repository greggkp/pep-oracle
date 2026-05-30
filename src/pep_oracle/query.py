import json
from datetime import date

import anthropic

from pep_oracle import temporal
from pep_oracle.config import QUERY_MODEL
from pep_oracle.embeddings import embed_texts
from pep_oracle.hybrid import hybrid_search
from pep_oracle.store import get_fresh_collection, get_ingestion_stats
from pep_oracle.temporal import VALID_INTENTS

PREPROCESS_MODEL = "claude-haiku-4-5-20251001"

# Per-intent steer appended to the prompt so the model reads the excerpts in the
# right temporal frame. timeless/historical need none.
_INTENT_GUIDANCE = {
    "current": "These excerpts are ordered newest-first. Lead with the most "
               "recent take and flag where earlier statements have been superseded. ",
    "evolution": "These excerpts are ordered oldest-first. Trace how the "
                 "discussion changed over time, citing each episode's date in order. ",
    "prediction": "These excerpts are ordered oldest-first. Identify what was "
                  "predicted and whether later episodes confirmed or revised it. ",
}

SYSTEM_PROMPT = """\
You are a helpful assistant that answers questions about the podcast \
"PEP with Chas and Dr Dave" (a podcast about American politics by \
Australian journalists Chas Licciardello and Dr David Smith).

Answer the question based ONLY on the provided transcript excerpts. \
If the information is not in the excerpts, say so. Always cite which \
episode(s) your answer comes from, including the episode title and date.

When the question is about current or recent events, prefer information \
from the most recent episodes. If older episodes discuss the same topic \
differently, note the progression over time.

When transcript excerpts include speaker labels like [Chas] or [Dave], \
attribute statements to the specific speaker. Use phrases like \
"Chas noted that..." or "According to Dave..." rather than the generic \
"they discussed"."""

PREPROCESS_PROMPT = """\
Extract search filters from this podcast question. Today's date is {today}.
The podcast has episodes from {earliest_date} to {latest_date} (episodes {earliest_ep} to {latest_ep}).
The podcast hosts are Chas Licciardello and Dr David Smith (Dave).

Return a JSON object with these fields:
- "episode_numbers": list of specific episode numbers mentioned (empty list if none)
- "after_date": earliest date to include as "YYYY-MM-DD" (null if no time constraint)
- "before_date": latest date to include as "YYYY-MM-DD" (null if no time constraint)
- "search_query": the core topic to search for (rewrite the question as a concise search phrase, EXCLUDING speaker names)
- "prefer_recent": true if the user wants the LATEST/most recent information, false otherwise
- "speaker": "Chas" or "Dave" if the question targets a specific speaker, null otherwise
- "compare_speakers": true if the question asks to compare what Chas said vs Dave (or vice versa), false otherwise
- "temporal_intent": one of "current", "historical", "evolution", "prediction", "timeless" (see below)

IMPORTANT: "temporal_intent" controls how time is used in ranking:
- "current": the user wants the LATEST/now state of an evolving situation ("latest on X", \
"what's happening with X now", present-tense "will X happen", "these days"). Recency is \
handled by ranking, so do NOT set after_date for these.
- "historical": about a specific PAST time ("what did they say about X back in June", "in 2025"). \
Set after_date/before_date to that window.
- "evolution": how the discussion or their view CHANGED over time ("how has their view on X \
evolved", "trace the X discussion over time").
- "prediction": what they PREDICTED and whether it panned out ("what did they predict about X", \
"did their X call come true").
- "timeless": time-independent background/identity ("who is Dr Dave"), or a specific-episode \
lookup, or a plain topic question with no time signal.
Use after_date/before_date ONLY for explicit time windows ("last month", "in June") — NOT for \
"latest/recent" (use temporal_intent="current" instead).

IMPORTANT: Strip speaker names from search_query. "What did Chas say about tariffs?" → \
search_query should be "tariffs", NOT "Chas tariffs". The speaker field handles filtering.

If conversation history is provided, use it to resolve pronouns and references \
in the question. For example, if the user previously asked about "Pete Hegseth" \
and now asks "what does he think?", rewrite the search query to include "Pete Hegseth".

Examples:
- "what did they say about Iran in episode 248?" → {{"episode_numbers": [248], "after_date": null, "before_date": null, "search_query": "Iran", "prefer_recent": false, "speaker": null, "compare_speakers": false, "temporal_intent": "timeless"}}
- "will the war in Iran end soon?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Iran war ending", "prefer_recent": true, "speaker": null, "compare_speakers": false, "temporal_intent": "current"}}
- "what did Chas say about tariffs?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "tariffs", "prefer_recent": false, "speaker": "Chas", "compare_speakers": false, "temporal_intent": "timeless"}}
- "latest on Iran?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Iran latest developments", "prefer_recent": true, "speaker": null, "compare_speakers": false, "temporal_intent": "current"}}
- "how has their view on Trump's Iran policy changed?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Trump Iran policy", "prefer_recent": false, "speaker": null, "compare_speakers": false, "temporal_intent": "evolution"}}
- "what did they predict would happen with the Iran war?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Iran war outcome", "prefer_recent": false, "speaker": null, "compare_speakers": false, "temporal_intent": "prediction"}}
- "Chas vs Dave on immigration" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "immigration", "prefer_recent": false, "speaker": null, "compare_speakers": true, "temporal_intent": "timeless"}}
- "what were the main topics last month?" → {{"episode_numbers": [], "after_date": "{last_month_start}", "before_date": "{last_month_end}", "search_query": "main topics discussed", "prefer_recent": false, "speaker": null, "compare_speakers": false, "temporal_intent": "historical"}}
- "who is Dr Dave?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Dr Dave background who is", "prefer_recent": false, "speaker": null, "compare_speakers": false, "temporal_intent": "timeless"}}
- Conversation: User asked about Pete Hegseth. Question: "what does he think about tariffs?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Pete Hegseth tariffs opinion", "prefer_recent": false, "speaker": null, "compare_speakers": false, "temporal_intent": "timeless"}}

Respond with ONLY the JSON object, no other text.

{history_block}Question: {question}"""


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _trim_to_speaker(text: str, speaker: str) -> str:
    """Extract only the target speaker's portions from speaker-labeled text.

    Text format: "[Chas] I think so. [Dave] Me too. [Chas] Right."
    For speaker="Chas", returns: "[Chas] I think so. [Chas] Right."
    """
    import re
    # Split on speaker labels, keeping the labels
    parts = re.split(r"(\[[^\]]+\])", text)
    result = []
    include = False
    for part in parts:
        if part.startswith("[") and part.endswith("]"):
            label = part[1:-1]
            include = label.lower() == speaker.lower()
            if include:
                result.append(part)
        elif include:
            result.append(part)
    return " ".join("".join(result).split())


def build_context(
    results: list[dict],
    speaker: str | None = None,
    order: str = "newest_first",
) -> str:
    # newest_first: recent info first (default). chronological: oldest first, so
    # the model can narrate evolution / prediction -> outcome in order.
    sorted_results = sorted(
        results,
        key=lambda r: r.get("episode_date", ""),
        reverse=(order != "chronological"),
    )
    sections = []
    for r in sorted_results:
        ep_num = f"Ep {r['episode_number']}, " if r.get("episode_number") else ""
        start = format_timestamp(r["start_time"])
        end = format_timestamp(r["end_time"])
        header = f"[{r['episode_title']} ({ep_num}{r['episode_date']}), {start}–{end}]"
        text = r.get("speaker_text") or r["text"]
        if speaker and r.get("speakers") and r.get("speaker_text"):
            trimmed = _trim_to_speaker(r["speaker_text"], speaker)
            if trimmed:
                text = trimmed
        sections.append(f"---\n{header}\n{text}\n---")
    return "\n\n".join(sections)


def preprocess_query(
    question: str,
    anthropic_client: anthropic.Anthropic | None = None,
    history: list[dict] | None = None,
) -> dict:
    """Use a fast Claude model to extract time/episode filters from the question."""
    from datetime import date, timedelta

    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    today = date.today()
    # Get ingestion stats for context
    collection = get_fresh_collection()
    stats = get_ingestion_stats(collection)

    earliest_date = stats["earliest_date"] or "unknown"
    latest_date = stats["latest_date"] or "unknown"
    earliest_ep = stats["earliest_episode"] or "unknown"
    latest_ep = stats["latest_episode"] or "unknown"

    # Dates for the prompt examples
    recent_date = (today - timedelta(days=60)).isoformat()
    last_month_start = today.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_start.replace(day=1).isoformat()
    last_month_end = (today.replace(day=1) - timedelta(days=1)).isoformat()

    # Format conversation history for the prompt
    history_block = ""
    if history:
        lines = []
        for msg in history:
            role = "User" if msg["role"] == "user" else "Assistant"
            lines.append(f"{role}: {msg['content']}")
        history_block = "Conversation so far:\n" + "\n".join(lines) + "\n\n"

    prompt = PREPROCESS_PROMPT.format(
        today=today.isoformat(),
        earliest_date=earliest_date,
        latest_date=latest_date,
        earliest_ep=earliest_ep,
        latest_ep=latest_ep,
        recent_date=recent_date,
        last_month_start=last_month_start,
        last_month_end=last_month_end,
        history_block=history_block,
        question=question,
    )

    response = anthropic_client.messages.create(
        model=PREPROCESS_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]  # remove opening ```json line
            raw = raw.rsplit("```", 1)[0]  # remove closing ```
        parsed = json.loads(raw)
    except (json.JSONDecodeError, IndexError, ValueError):
        # Fall back to unfiltered search
        return {
            "episode_numbers": [],
            "after_date": None,
            "before_date": None,
            "search_query": question,
            "prefer_recent": False,
            "speaker": None,
            "compare_speakers": False,
            "temporal_intent": "timeless",
        }

    intent = parsed.get("temporal_intent")
    if intent not in VALID_INTENTS:
        # Derive from legacy signals if the model omitted/mis-set it.
        intent = "current" if parsed.get("prefer_recent") else "timeless"
    return {
        "episode_numbers": parsed.get("episode_numbers", []),
        "after_date": parsed.get("after_date"),
        "before_date": parsed.get("before_date"),
        "search_query": parsed.get("search_query", question),
        "prefer_recent": parsed.get("prefer_recent", False),
        "speaker": parsed.get("speaker"),
        "compare_speakers": parsed.get("compare_speakers", False),
        "temporal_intent": intent,
    }


def _retrieve_relaxing_filters(
    collection,
    query_text: str,
    embedding: list[float],
    *,
    top_k: int,
    episode_numbers: list[int],
    after_date: str | None,
    before_date: str | None,
    speaker: str | None,
) -> tuple[list[dict], str | None]:
    """Retrieve chunks (hybrid semantic+lexical), relaxing fragile filters if
    they eliminate every hit.

    Speaker filtering is dropped first: diarized speaker-name mapping is fragile
    and some episodes carry only raw 'speaker_N' labels, so a has_speaker_chas
    clause can match nothing even when the topic is clearly present. The date
    floor is dropped next. An explicit episode_numbers filter is never relaxed —
    "what did they say in ep 248" must not silently answer from other episodes.

    Returns (results, effective_speaker); effective_speaker is None once the
    speaker filter has been dropped, so the caller skips speaker-trimming.
    """
    def run(spk: str | None, after: str | None, before: str | None) -> list[dict]:
        return hybrid_search(
            collection, query_text, embedding, top_k=top_k,
            episode_numbers=episode_numbers or None,
            after_date=after, before_date=before, speaker=spk,
        )

    results = run(speaker, after_date, before_date)
    if results:
        return results, speaker
    if speaker:
        results = run(None, after_date, before_date)
        if results:
            return results, None
    if after_date or before_date:
        results = run(None, None, None)
        if results:
            return results, None
    return [], speaker


def ask(
    question: str,
    top_k: int = 10,
    model: str = QUERY_MODEL,
    anthropic_client: anthropic.Anthropic | None = None,
    history: list[dict] | None = None,
) -> str:
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Pre-process to extract filters
    filters = preprocess_query(
        question,
        anthropic_client=anthropic_client,
        history=history,
    )

    # Embed the search query (may be rewritten by pre-processor)
    query_embedding = embed_texts([filters["search_query"]])[0]

    # Retrieve relevant chunks with filters
    collection = get_fresh_collection()
    speaker = filters.get("speaker")
    compare = filters.get("compare_speakers", False)
    intent = filters.get("temporal_intent", "timeless")

    search_query = filters["search_query"]
    if compare:
        # Dual retrieval: half for Chas, half for Dave
        half_k = max(top_k // 2, 1)
        chas_results = hybrid_search(
            collection, search_query, query_embedding, top_k=half_k,
            episode_numbers=filters["episode_numbers"] or None,
            after_date=filters["after_date"],
            before_date=filters["before_date"],
            speaker="Chas",
        )
        dave_results = hybrid_search(
            collection, search_query, query_embedding, top_k=half_k,
            episode_numbers=filters["episode_numbers"] or None,
            after_date=filters["after_date"],
            before_date=filters["before_date"],
            speaker="Dave",
        )
        chas_context = build_context(chas_results, speaker="Chas")
        dave_context = build_context(dave_results, speaker="Dave")
        context = f"CHAS'S STATEMENTS:\n\n{chas_context}\n\nDAVE'S STATEMENTS:\n\n{dave_context}"
        if not chas_results and not dave_results:
            return "No relevant content found. Have you ingested any episodes yet?"
    else:
        # Fetch a larger candidate pool via hybrid (semantic+BM25) retrieval, then
        # let the temporal layer select + order the final top_k by intent (recency
        # only for 'current'; chronological for evolution/prediction).
        candidates, eff_speaker = _retrieve_relaxing_filters(
            collection, search_query, query_embedding,
            top_k=top_k * temporal.CANDIDATE_MULTIPLIER,
            episode_numbers=filters["episode_numbers"],
            after_date=filters["after_date"],
            before_date=filters["before_date"],
            speaker=speaker,
        )
        if not candidates:
            return "No relevant content found. Have you ingested any episodes yet?"
        results, order = temporal.select_for_intent(candidates, intent, top_k, date.today())
        context = build_context(results, speaker=eff_speaker, order=order)

    # Build prompt and call Claude
    guidance = _INTENT_GUIDANCE.get(intent, "")
    user_message = f"TRANSCRIPT EXCERPTS:\n\n{context}\n\n{guidance}QUESTION: {question}"
    messages = list(history or []) + [{"role": "user", "content": user_message}]
    response = anthropic_client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text
