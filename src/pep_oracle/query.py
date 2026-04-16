import json

import anthropic

from pep_oracle.config import QUERY_MODEL
from pep_oracle.embeddings import embed_texts
from pep_oracle.store import (
    get_client,
    get_collection,
    get_ingestion_stats,
    query as store_query,
)

PREPROCESS_MODEL = "claude-haiku-4-5-20251001"

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

IMPORTANT: Set after_date for questions about current/recent/ongoing events. Words like \
"soon", "will", "currently", "right now", "these days", "latest", "recent", present tense \
questions about evolving situations — all imply the user wants RECENT episodes. \
Use after_date = 60 days before today for these. Only leave after_date as null for \
timeless/historical questions like "who is X?" or "when did they first discuss Y?".

IMPORTANT: Strip speaker names from search_query. "What did Chas say about tariffs?" → \
search_query should be "tariffs", NOT "Chas tariffs". The speaker field handles filtering.

If conversation history is provided, use it to resolve pronouns and references \
in the question. For example, if the user previously asked about "Pete Hegseth" \
and now asks "what does he think?", rewrite the search query to include "Pete Hegseth".

Examples:
- "what did they say about Iran in episode 248?" → {{"episode_numbers": [248], "after_date": null, "before_date": null, "search_query": "Iran", "prefer_recent": false, "speaker": null, "compare_speakers": false}}
- "will the war in Iran end soon?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "Iran war ending", "prefer_recent": true, "speaker": null, "compare_speakers": false}}
- "what did Chas say about tariffs?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "tariffs", "prefer_recent": false, "speaker": "Chas", "compare_speakers": false}}
- "does Dave think Trump will win?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Trump winning election", "prefer_recent": false, "speaker": "Dave", "compare_speakers": false}}
- "Chas vs Dave on immigration" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "immigration", "prefer_recent": false, "speaker": null, "compare_speakers": true}}
- "what are they saying about tariffs?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "tariffs trade policy", "prefer_recent": true, "speaker": null, "compare_speakers": false}}
- "latest on Iran?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "Iran latest developments", "prefer_recent": true, "speaker": null, "compare_speakers": false}}
- "what were the main topics last month?" → {{"episode_numbers": [], "after_date": "{last_month_start}", "before_date": "{last_month_end}", "search_query": "main topics discussed", "prefer_recent": false, "speaker": null, "compare_speakers": false}}
- "who is Dr Dave?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Dr Dave background who is", "prefer_recent": false, "speaker": null, "compare_speakers": false}}
- Conversation: User asked about Pete Hegseth. Question: "what does he think about tariffs?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Pete Hegseth tariffs opinion", "prefer_recent": false, "speaker": null, "compare_speakers": false}}

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


def build_context(results: list[dict], speaker: str | None = None) -> str:
    # Sort by episode date descending so Claude sees recent info first
    sorted_results = sorted(
        results,
        key=lambda r: r.get("episode_date", ""),
        reverse=True,
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
    client = get_client()
    collection = get_collection(client)
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
        }

    return {
        "episode_numbers": parsed.get("episode_numbers", []),
        "after_date": parsed.get("after_date"),
        "before_date": parsed.get("before_date"),
        "search_query": parsed.get("search_query", question),
        "prefer_recent": parsed.get("prefer_recent", False),
        "speaker": parsed.get("speaker"),
        "compare_speakers": parsed.get("compare_speakers", False),
    }


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
    filters = preprocess_query(
        question,
        anthropic_client=anthropic_client,
        history=history,
    )

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
