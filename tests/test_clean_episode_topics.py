"""Tests for clean_episode_topics() — the label cleaning pipeline."""

from pep_oracle.topics import clean_episode_topics


def test_strips_segment_prefixes_extracts_subtopics():
    """Segment labels are removed; parenthetical subtopics become individual entries."""
    labels = [
        "Correspondence (Corrections, Stings, Noem v Miller)",
        "Not Normal (Ballroom, Money)",
        "Stats Nug (Emergency Response)",
        "Policy Time (Healthcare Reform)",
        "Cuba",
    ]
    result = clean_episode_topics(labels)
    assert "Cuba" in result
    # Subtopics extracted
    assert "Corrections" in result
    assert "Stings" in result
    assert "Noem v Miller" in result
    assert "Ballroom" in result
    assert "Money" in result
    assert "Emergency Response" in result
    assert "Healthcare Reform" in result
    # Segment names gone
    assert not any(r.startswith("Correspondence") for r in result)
    assert not any(r.startswith("Not Normal") for r in result)
    assert not any(r.startswith("Stats Nug") for r in result)
    assert not any(r.startswith("Policy Time") for r in result)


def test_cleans_unleashed_with_topic():
    """'Unleashed: Topic' becomes 'Topic'."""
    labels = ["Unleashed: Birthright Citizenship", "Cuba"]
    result = clean_episode_topics(labels)
    assert "Birthright Citizenship" in result
    assert not any("Unleashed" in r for r in result)


def test_discards_bare_unleashed():
    """'Unleashed with X' is discarded entirely."""
    labels = ["Unleashed with Lachie", "Cuba"]
    result = clean_episode_topics(labels)
    assert result == ["Cuba"]


def test_strips_cont_suffix():
    """'Topic Cont.' and 'Topic Cont' are cleaned to 'Topic'."""
    labels = ["Birthright Citizenship Cont.", "Iran Latest Cont"]
    result = clean_episode_topics(labels)
    assert "Birthright Citizenship" in result
    assert "Iran Latest" in result
    assert not any("Cont" in r for r in result)


def test_unleashed_with_cont():
    """'Unleashed: Topic Cont.' is cleaned to 'Topic'."""
    labels = ["Unleashed: Birthright Citizenship Cont."]
    result = clean_episode_topics(labels)
    assert result == ["Birthright Citizenship"]


def test_segment_without_parenthetical_is_discarded():
    """A bare segment name with no subtopics is discarded."""
    labels = ["Correspondence", "Cuba"]
    result = clean_episode_topics(labels)
    assert result == ["Cuba"]


def test_empty_input():
    """Empty label list returns empty list."""
    assert clean_episode_topics([]) == []


def test_preserves_order():
    """Output labels maintain input order (timestamp order)."""
    labels = ["Cuba", "Iran Latest", "Unleashed: Hegseth", "Not Normal (Ballroom, Money)"]
    result = clean_episode_topics(labels)
    assert result == ["Cuba", "Iran Latest", "Hegseth", "Ballroom", "Money"]
