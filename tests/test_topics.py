"""Tests for parse_description_topics() — timestamp label extraction from episode HTML."""

from pep_oracle.topics import parse_description_topics


def test_extracts_labels_from_html():
    """Extracts topic labels from HTML description with timestamps."""
    description = (
        "<p>Chas &amp; Dr Dave discuss things.</p> "
        "<p>Timestamps:<br />"
        "0:00 - Introducing: Dr Dave<br />"
        "3:57 - Gratefuls (Sliwa, Colbert)<br />"
        "16:28 - Correspondence (Corrections, Stings, Noem v Miller)<br />"
        "25:19 - Not Normal (Ballroom, Money)<br />"
        "1:06:30 - Cuba<br />"
        "1:23:04 - Iran Latest</p> "
        "<p>Homework:</p>"
    )
    result = parse_description_topics(description)
    assert result == [
        "Correspondence (Corrections, Stings, Noem v Miller)",
        "Not Normal (Ballroom, Money)",
        "Cuba",
        "Iran Latest",
    ]


def test_cleans_trailing_noise():
    """Trailing 'Homework:' or 'SHOW LINKS:' appended to the last label is stripped."""
    description = (
        "<p>Timestamps:<br />"
        "0:00 - Introducing: Dr Dave<br />"
        "27:09 - Polling Update<br />"
        "3:15:17 - PBS/NPR Court Victory Homework:</p>"
    )
    result = parse_description_topics(description)
    assert result == ["Polling Update", "PBS/NPR Court Victory"]


def test_no_timestamps_section():
    """Description without 'Timestamps:' marker returns empty list."""
    description = "<p>Just a plain episode description with no timestamps.</p>"
    assert parse_description_topics(description) == []


def test_empty_description():
    """Empty or whitespace description returns empty list."""
    assert parse_description_topics("") == []
    assert parse_description_topics("   ") == []


def test_filters_grateful_variants():
    """Both 'Grateful' and 'Gratefuls' are filtered out."""
    description = (
        "<p>Timestamps:<br />"
        "0:00 - Introducing: Elle Hardy<br />"
        "1:43 - Grateful (Andrew Lownie, Footy Players)<br />"
        "8:57 - Kristi Noem Sacked<br />"
        "1:10:19 - Ukraine Corner</p>"
    )
    result = parse_description_topics(description)
    assert result == ["Kristi Noem Sacked", "Ukraine Corner"]
