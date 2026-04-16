"""Tests for parse_episode_input() — range/comma episode string parsing."""

import pytest

from pep_oracle.server import parse_episode_input


def test_single_number():
    assert parse_episode_input("210") == [210]


def test_comma_list():
    assert parse_episode_input("210, 215, 220") == [210, 215, 220]


def test_range():
    assert parse_episode_input("150-155") == [150, 151, 152, 153, 154, 155]


def test_mixed():
    assert parse_episode_input("150-155, 210, 220-222") == [
        150, 151, 152, 153, 154, 155, 210, 220, 221, 222,
    ]


def test_whitespace_tolerance():
    assert parse_episode_input(" 150 - 155 , 210 ") == [
        150, 151, 152, 153, 154, 155, 210,
    ]


def test_duplicates_removed():
    assert parse_episode_input("150, 150-152") == [150, 151, 152]


def test_empty_string():
    assert parse_episode_input("") == []


def test_whitespace_only():
    assert parse_episode_input("   ") == []


def test_backwards_range_raises():
    with pytest.raises(ValueError, match="200-150"):
        parse_episode_input("200-150")


def test_non_numeric_raises():
    with pytest.raises(ValueError, match="abc"):
        parse_episode_input("abc")


def test_negative_number_raises():
    with pytest.raises(ValueError, match="Invalid episode number"):
        parse_episode_input("-5")


def test_range_too_large():
    with pytest.raises(ValueError, match="too large"):
        parse_episode_input("1-2000")
