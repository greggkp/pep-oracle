"""Tests for topics.json file I/O (save_topics / load_topics / bootstrap_topics)."""

import json
from datetime import datetime, timezone

from pep_oracle.models import Episode
from pep_oracle.topics import bootstrap_topics, load_topics, save_topics


def _make_episode(num, description=""):
    return Episode(
        guid=f"guid-{num}",
        title=f"Test Episode (Ep {num})",
        pub_date=datetime(2026, 1, num, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=description,
        duration_seconds=3600,
        episode_number=num,
    )


def test_save_and_load_roundtrip(tmp_path):
    """Save topics, then load — data matches."""
    path = tmp_path / "topics.json"
    episodes = [
        {"episode_number": 255, "date": "2026-04-10", "topics": ["Cuba", "Iran Latest"]},
        {"episode_number": 254, "date": "2026-04-07", "topics": ["Hegseth Issues"]},
    ]
    save_topics(episodes, path)
    loaded = load_topics(path)
    assert loaded == episodes


def test_load_nonexistent_returns_empty(tmp_path):
    """Loading from a nonexistent file returns empty list."""
    path = tmp_path / "nonexistent.json"
    assert load_topics(path) == []


def test_save_merges_with_existing(tmp_path):
    """Saving new episodes merges with existing, sorted newest-first."""
    path = tmp_path / "topics.json"
    # Pre-populate with episode 254
    existing = [{"episode_number": 254, "date": "2026-04-07", "topics": ["Hegseth Issues"]}]
    path.write_text(json.dumps({"episodes": existing}))

    # Save episode 255
    new = [{"episode_number": 255, "date": "2026-04-10", "topics": ["Cuba"]}]
    save_topics(new, path)

    loaded = load_topics(path)
    assert len(loaded) == 2
    assert loaded[0]["episode_number"] == 255  # newest first
    assert loaded[1]["episode_number"] == 254


def test_save_overwrites_existing_episode(tmp_path):
    """Re-saving an episode number replaces its topics (for --force re-ingestion)."""
    path = tmp_path / "topics.json"
    existing = [{"episode_number": 255, "date": "2026-04-10", "topics": ["Old Topic"]}]
    path.write_text(json.dumps({"episodes": existing}))

    new = [{"episode_number": 255, "date": "2026-04-10", "topics": ["New Topic"]}]
    save_topics(new, path)

    loaded = load_topics(path)
    assert len(loaded) == 1
    assert loaded[0]["topics"] == ["New Topic"]


def test_save_sorts_newest_first(tmp_path):
    """Episodes are sorted by episode_number descending after merge."""
    path = tmp_path / "topics.json"
    episodes = [
        {"episode_number": 250, "date": "2026-03-20", "topics": ["A"]},
        {"episode_number": 253, "date": "2026-04-01", "topics": ["B"]},
        {"episode_number": 251, "date": "2026-03-25", "topics": ["C"]},
    ]
    save_topics(episodes, path)
    loaded = load_topics(path)
    numbers = [e["episode_number"] for e in loaded]
    assert numbers == [253, 251, 250]


def test_load_corrupt_json_returns_empty(tmp_path):
    """Corrupt JSON file returns empty list."""
    path = tmp_path / "topics.json"
    path.write_text("not valid json")
    assert load_topics(path) == []


def test_save_creates_parent_dirs(tmp_path):
    """save_topics creates parent directories if they don't exist."""
    path = tmp_path / "subdir" / "topics.json"
    episodes = [{"episode_number": 1, "date": "2026-01-01", "topics": ["A"]}]
    save_topics(episodes, path)
    assert path.exists()
    assert load_topics(path) == episodes


def test_bootstrap_creates_topics_file(tmp_path):
    """bootstrap_topics creates topics.json from episode descriptions."""
    path = tmp_path / "topics.json"
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "1:06:30 - Cuba<br />"
            "1:23:04 - Iran Latest</p>",
        ),
        _make_episode(
            2,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Host<br />"
            "10:00 - Hegseth Issues</p>",
        ),
    ]
    bootstrap_topics(episodes, path)
    loaded = load_topics(path)
    assert len(loaded) == 2
    assert loaded[0]["episode_number"] == 3  # newest first
    assert "Cuba" in loaded[0]["topics"]
    assert "Iran Latest" in loaded[0]["topics"]
    assert loaded[1]["episode_number"] == 2
    assert "Hegseth Issues" in loaded[1]["topics"]


def test_bootstrap_skips_episodes_without_timestamps(tmp_path):
    """Episodes with no timestamp section produce no entry."""
    path = tmp_path / "topics.json"
    episodes = [
        _make_episode(3, "No timestamps here"),
        _make_episode(
            2,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Host<br />"
            "10:00 - Cuba</p>",
        ),
    ]
    bootstrap_topics(episodes, path)
    loaded = load_topics(path)
    assert len(loaded) == 1
    assert loaded[0]["episode_number"] == 2


def test_bootstrap_skips_episodes_without_episode_number(tmp_path):
    """Episodes with episode_number=None are skipped."""
    path = tmp_path / "topics.json"
    bonus = Episode(
        guid="guid-bonus",
        title="Bonus",
        pub_date=datetime(2026, 1, 10, tzinfo=timezone.utc),
        audio_url="https://example.com/bonus.mp3",
        description="<p>Timestamps:<br />0:00 - Introducing: Host<br />10:00 - Topic</p>",
        duration_seconds=1800,
        episode_number=None,
    )
    episodes = [bonus]
    bootstrap_topics(episodes, path)
    loaded = load_topics(path)
    assert len(loaded) == 0


def test_bootstrap_cleans_labels(tmp_path):
    """bootstrap_topics applies clean_episode_topics to parsed labels."""
    path = tmp_path / "topics.json"
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "10:00 - Not Normal (Ballroom, Money)<br />"
            "20:00 - Unleashed: Hegseth Cont.<br />"
            "1:06:30 - Cuba</p>",
        ),
    ]
    bootstrap_topics(episodes, path)
    loaded = load_topics(path)
    topics = loaded[0]["topics"]
    assert "Cuba" in topics
    assert "Hegseth" in topics
    assert "Ballroom" in topics
    assert "Money" in topics
    assert not any("Not Normal" in t for t in topics)
    assert not any("Unleashed" in t for t in topics)
    assert not any("Cont" in t for t in topics)
