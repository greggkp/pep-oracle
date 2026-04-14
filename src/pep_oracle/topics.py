import json
import re

import anthropic

from pep_oracle.models import Episode

_TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s*-\s*(.+)")
_SKIP_LABELS = ("Introducing", "Grateful")

TOPIC_MODEL = "claude-haiku-4-5-20251001"

TOPIC_PROMPT = """\
Extract 5-8 distinct discussion topics from these podcast episode descriptions. \
The first episode listed is the LATEST. Extract as many topics as possible from \
the LATEST episode first. Only use older episodes to fill remaining slots if the \
latest episode yields fewer than 5 topics.

Return a JSON array of objects, each with:
- "topic": a short label (3-6 words)
- "question": a natural question a podcast listener might ask about this topic \
(include a recency word like "latest", "recent", or "currently" since these are recent episodes)
- "episode_number": the episode number where this topic appears

Deduplicate: if multiple episodes discuss the same topic, pick the most recent one. \
No overlapping or redundant topics.

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
) -> list[dict]:
    """Extract discussion topics from recent episode descriptions via Haiku."""
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Filter to episodes with descriptions, take most recent `count`
    with_desc = [ep for ep in episodes if ep.description and ep.description.strip()]
    recent = sorted(with_desc, key=lambda ep: ep.pub_date, reverse=True)[:count]

    if not recent:
        return []

    episodes_text = "\n".join(
        f"- Ep {ep.episode_number} ({ep.pub_date.strftime('%Y-%m-%d')}): {ep.description}"
        for ep in recent
    )

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

        return json.loads(raw)
    except Exception:
        return []
