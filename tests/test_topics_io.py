"""Tests for topics.json file I/O (save_topics / load_topics)."""

import json

from pep_oracle.topics import load_topics, save_topics


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
