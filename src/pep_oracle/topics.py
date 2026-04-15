import json
import re

import anthropic

from pep_oracle.models import Episode

_TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s*-\s*(.+)")
_SKIP_LABELS = ("Introducing", "Grateful")
# Recurring segment names — strip prefix, preserve parenthetical subtopics
_SEGMENT_PREFIXES = ("Correspondence", "Not Normal", "Stats Nug", "Policy Time")
_UNLEASHED_RE = re.compile(r"^Unleashed\s*:\s*(.+)", re.IGNORECASE)
_CONT_RE = re.compile(r"\s+Cont\.?\s*$")
_PARENS_RE = re.compile(r"\(([^)]+)\)")

TOPIC_MODEL = "claude-haiku-4-5-20251001"

TOPIC_PROMPT = """\
You are selecting discussion topics from a political podcast. Below are topic \
labels extracted from episode timestamps. Select 5-8 of the most interesting \
and substantive topics for a listener to explore.

SHOW-SPECIFIC SEGMENTS — these are recurring segment names with special meaning:
- "Unleashed": A deep-dive or continuation segment. The topic after the colon \
is the actual subject (e.g., "Unleashed: Birthright Citizenship Cont." is about \
birthright citizenship). Merge with the main topic if one exists.
- "Correspondence": Listener mail and corrections. Subtopics in parentheses \
may be worth surfacing as standalone topics if they are substantive.
- "Not Normal": A roundup of abnormal political events. Subtopics in \
parentheses are the individual items.
- "Stats Nug": A statistics-focused mini-segment.
- "Policy Time": A segment focused on specific policy discussion. The \
parenthetical describes the policy area.

RULES:
- Use the human-written labels as the chip text. Do NOT paraphrase or reinterpret.
- Deduplicate: if multiple episodes discuss the same topic (e.g., "Hegseth Issues" \
and "Hegseth Issues Cont."), pick the most recent episode and use one label.
- Prioritize topics from the LATEST (first-listed) episode.
- Only use older episodes to fill remaining slots.

Return a JSON array of objects, each with:
- "topic": the label text (from the timestamps, verbatim or minimally trimmed)
- "question": a natural question a listener might ask (include a recency word \
like "latest", "recent", or "currently")
- "episode_number": the episode number where this topic appears

Episodes:
{episodes_text}

Respond with ONLY the JSON array, no other text."""


def parse_description_topics(description: str) -> list[str]:
    """Extract topic labels from timestamp lines in an episode description.

    Parses HTML descriptions for lines like "1:23:04 - Iran Latest" between
    a "Timestamps:" marker and the next non-timestamp content. Filters out
    meta-segments (Introducing, Gratefuls).
    """
    if not description or not description.strip():
        return []

    # Replace HTML tags with newlines
    text = re.sub(r"<[^>]+>", "\n", description)
    lines = text.split("\n")

    in_timestamps = False
    labels: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r"(?i)timestamps?\s*:", line):
            in_timestamps = True
            continue
        if not in_timestamps:
            continue
        match = _TIMESTAMP_RE.match(line)
        if match:
            label = match.group(1).strip()
            # Clean trailing noise (e.g., "Iran Latest Homework:" or "PBS/NPR Victory SHOW LINKS:")
            label = re.split(r"\s+(?:Homework|SHOW LINKS)\s*:", label)[0].strip()
            if label:
                labels.append(label)
        else:
            # Non-timestamp line after timestamps section — stop
            break

    # Filter meta-segments
    return [
        label for label in labels
        if not any(label.startswith(skip) for skip in _SKIP_LABELS)
    ]


def extract_topics(
    episodes: list[Episode],
    count: int = 5,
    anthropic_client: anthropic.Anthropic | None = None,
) -> dict:
    """Extract discussion topics from recent episodes via timestamp parsing + Haiku curation.

    Returns a dict with:
    - "topics": Haiku-curated list of topic dicts (label, question, episode_number)
    - "pool": remaining parsed labels not selected by Haiku, with template questions
    """
    _empty = {"topics": [], "pool": []}

    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Filter to episodes with descriptions, take most recent `count`
    with_desc = [ep for ep in episodes if ep.description and ep.description.strip()]
    recent = sorted(with_desc, key=lambda ep: ep.pub_date, reverse=True)[:count]

    if not recent:
        return _empty

    # Parse timestamp labels from each episode, tracking all labels with episode numbers
    episode_lines = []
    all_labels: list[dict] = []
    for ep in recent:
        labels = parse_description_topics(ep.description)
        if labels:
            header = f"Ep {ep.episode_number} ({ep.pub_date.strftime('%Y-%m-%d')}):"
            bullet_list = "\n".join(f"  - {label}" for label in labels)
            episode_lines.append(f"{header}\n{bullet_list}")
            for label in labels:
                all_labels.append({"topic": label, "episode_number": ep.episode_number})

    if not episode_lines:
        return _empty

    episodes_text = "\n\n".join(episode_lines)

    try:
        response = anthropic_client.messages.create(
            model=TOPIC_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": TOPIC_PROMPT.format(episodes_text=episodes_text),
                }
            ],
        )

        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0]

        topics = json.loads(raw)

        # Post-filter curated topics: remove segment names, extract subtopics to pool
        filtered_topics = []
        extracted_subtopics: list[dict] = []
        for t in topics:
            if any(t["topic"].startswith(p) for p in _SEGMENT_PREFIXES):
                # Extract parenthetical subtopics as individual pool entries
                match = _PARENS_RE.search(t["topic"])
                if match:
                    for sub in match.group(1).split(","):
                        sub = sub.strip()
                        if sub:
                            extracted_subtopics.append({
                                "topic": sub,
                                "question": f"What did they discuss about {sub} on the latest episode?",
                                "episode_number": t["episode_number"],
                            })
            else:
                filtered_topics.append(t)
        topics = filtered_topics

        # Build pool from labels Haiku didn't select, filtering segment names
        selected_labels = {t["topic"] for t in topics}
        pool = list(extracted_subtopics)
        for entry in all_labels:
            if entry["topic"] in selected_labels:
                continue
            label = entry["topic"]
            # Extract subtopics from segment labels, skip the segment name itself
            if any(label.startswith(prefix) for prefix in _SEGMENT_PREFIXES):
                match = _PARENS_RE.search(label)
                if match:
                    for sub in match.group(1).split(","):
                        sub = sub.strip()
                        if sub:
                            pool.append({
                                "topic": sub,
                                "question": f"What did they discuss about {sub} on the latest episode?",
                                "episode_number": entry["episode_number"],
                            })
                continue
            # Clean "Unleashed: Topic" → "Topic", skip bare "Unleashed with X"
            unleashed = _UNLEASHED_RE.match(label)
            if unleashed:
                label = unleashed.group(1).strip()
            elif label.lower().startswith("unleashed"):
                continue
            # Strip "Cont." suffix from continuations
            label = _CONT_RE.sub("", label)
            pool.append({
                "topic": label,
                "question": f"What did they discuss about {label} on the latest episode?",
                "episode_number": entry["episode_number"],
            })

        return {"topics": topics, "pool": pool}
    except Exception:
        return _empty
