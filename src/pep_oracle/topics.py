import json
import re

import anthropic

from pep_oracle.models import Episode

_TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s*-\s*(.+)")
_SKIP_LABELS = ("Introducing", "Grateful")

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
) -> list[dict]:
    """Extract discussion topics from recent episodes via timestamp parsing + Haiku curation."""
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Filter to episodes with descriptions, take most recent `count`
    with_desc = [ep for ep in episodes if ep.description and ep.description.strip()]
    recent = sorted(with_desc, key=lambda ep: ep.pub_date, reverse=True)[:count]

    if not recent:
        return []

    # Parse timestamp labels from each episode
    episode_lines = []
    for ep in recent:
        labels = parse_description_topics(ep.description)
        if labels:
            header = f"Ep {ep.episode_number} ({ep.pub_date.strftime('%Y-%m-%d')}):"
            bullet_list = "\n".join(f"  - {label}" for label in labels)
            episode_lines.append(f"{header}\n{bullet_list}")

    if not episode_lines:
        return []

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

        return json.loads(raw)
    except Exception:
        return []
