import json
import re
from pathlib import Path

from pep_oracle.models import Episode

_TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s*-\s*(.+)")
_SKIP_LABELS = ("Introducing", "Grateful")
# Recurring segment names — strip prefix, preserve parenthetical subtopics
_SEGMENT_PREFIXES = ("Correspondence", "Not Normal", "Stats Nug", "Policy Time")
_UNLEASHED_RE = re.compile(r"^Unleashed\s*:\s*(.+)", re.IGNORECASE)
_CONT_RE = re.compile(r"\s+Cont\.?\s*$")
_PARENS_RE = re.compile(r"\(([^)]+)\)")


def _default_topics_path(path: Path | None) -> Path:
    if path is None:
        from pep_oracle.config import TOPICS_PATH
        return TOPICS_PATH
    return path

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


def clean_episode_topics(labels: list[str]) -> list[str]:
    """Clean a single episode's parsed labels for display as topic chips.

    Processing:
    1. Strip segment prefixes (Correspondence, Not Normal, Stats Nug, Policy Time)
       and extract parenthetical subtopics as individual labels
    2. Clean Unleashed: "Unleashed: Topic" -> "Topic"; bare "Unleashed with X" discarded
    3. Strip "Cont." suffix from continuations
    """
    cleaned: list[str] = []
    for label in labels:
        # Segment prefixes: extract subtopics, discard segment name
        if any(label.startswith(prefix) for prefix in _SEGMENT_PREFIXES):
            match = _PARENS_RE.search(label)
            if match:
                for sub in match.group(1).split(","):
                    sub = sub.strip()
                    if sub:
                        cleaned.append(sub)
            continue

        # Unleashed: extract topic or discard bare form
        unleashed = _UNLEASHED_RE.match(label)
        if unleashed:
            label = unleashed.group(1).strip()
        elif label.lower().startswith("unleashed"):
            continue

        # Strip Cont. suffix
        label = _CONT_RE.sub("", label)

        cleaned.append(label)
    return cleaned


def load_topics(path: Path | None = None) -> list[dict]:
    """Load episode topics from disk. Returns list of episode dicts, or empty list if missing/corrupt."""
    path = _default_topics_path(path)
    try:
        data = json.loads(path.read_text())
        return data.get("episodes", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_topics(new_episodes: list[dict], path: Path | None = None) -> None:
    """Save episode topics to disk, merging with existing data.

    New episodes overwrite existing entries with the same episode_number.
    Result is sorted newest-first by episode_number.
    """
    path = _default_topics_path(path)
    existing = load_topics(path)
    # Build map: existing episodes, then overlay new ones
    by_num = {ep["episode_number"]: ep for ep in existing}
    for ep in new_episodes:
        by_num[ep["episode_number"]] = ep
    merged = sorted(by_num.values(), key=lambda e: e["episode_number"], reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"episodes": merged}, indent=2) + "\n")


def bootstrap_topics(episodes: list[Episode], path: Path | None = None) -> None:
    """Generate topics.json from all episodes' descriptions.

    Used on first /topics request when topics.json doesn't exist yet.
    Parses timestamp labels and cleans them for each episode.
    """
    path = _default_topics_path(path)
    entries: list[dict] = []
    for ep in episodes:
        if ep.episode_number is None:
            continue
        labels = parse_description_topics(ep.description or "")
        cleaned = clean_episode_topics(labels)
        if cleaned:
            entries.append({
                "episode_number": ep.episode_number,
                "date": ep.pub_date.strftime("%Y-%m-%d"),
                "topics": cleaned,
            })
    save_topics(entries, path)
