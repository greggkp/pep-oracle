import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from pep_oracle.config import RSS_FEED_URL
from pep_oracle.models import Episode

logger = logging.getLogger(__name__)

EPISODE_NUMBER_RE = re.compile(r"\((?:Ep|Episodio)\s*(\d+)", re.IGNORECASE)


def parse_duration(raw: str) -> int | None:
    """Convert HH:MM:SS or MM:SS to total seconds."""
    parts = raw.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return None


def extract_episode_number(title: str) -> int | None:
    match = EPISODE_NUMBER_RE.search(title)
    return int(match.group(1)) if match else None


def parse_entry(entry: feedparser.FeedParserDict) -> Episode:
    enclosures = entry.get("enclosures", [])
    audio_url = enclosures[0]["href"] if enclosures else ""

    raw_duration = entry.get("itunes_duration", "")
    duration = parse_duration(raw_duration) if raw_duration else None

    pub_date = parsedate_to_datetime(entry["published"])
    if pub_date.tzinfo is None:
        pub_date = pub_date.replace(tzinfo=timezone.utc)

    return Episode(
        guid=entry["id"],
        title=entry["title"],
        pub_date=pub_date,
        audio_url=audio_url,
        description=entry.get("summary", ""),
        duration_seconds=duration,
        episode_number=extract_episode_number(entry["title"]),
    )


def fetch_episodes(feed_url: str = RSS_FEED_URL, timeout: int = 15) -> list[Episode]:
    logger.info("Fetching RSS feed from %s", feed_url)
    if feed_url.startswith(("http://", "https://")):
        import requests
        response = requests.get(feed_url, timeout=timeout)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    else:
        feed = feedparser.parse(feed_url)
    if feed.bozo:
        logger.warning("Feed bozo error: %s", feed.bozo_exception)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"Failed to parse RSS feed: {feed.bozo_exception}")
    logger.info("Parsed %d entries from RSS feed", len(feed.entries))
    episodes = [parse_entry(e) for e in feed.entries]
    episodes.sort(key=lambda ep: ep.pub_date, reverse=True)
    if episodes:
        logger.info(
            "Latest episode: Ep %s — %s (%s)",
            episodes[0].episode_number or "?",
            episodes[0].title[:60],
            episodes[0].pub_date.isoformat(),
        )
    return episodes
