from pathlib import Path

from pep_oracle.feed import extract_episode_number, fetch_episodes, parse_duration

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_duration_hhmmss():
    assert parse_duration("03:22:55") == 12175


def test_parse_duration_mmss():
    assert parse_duration("45:30") == 2730


def test_parse_duration_invalid():
    assert parse_duration("bogus") is None


def test_extract_episode_number():
    assert extract_episode_number("MINING NEMO? PEP with Chas & Dr Dave (Ep 251, 20 March)") == 251


def test_extract_episode_number_spanish():
    assert extract_episode_number("PEP CON CHAS Y EL DR DAVE (Episodio 244, 13 De Febrero)") == 244


def test_extract_episode_number_missing():
    assert extract_episode_number("Bonus: A Special Chat") is None


def test_fetch_episodes_from_fixture():
    fixture_path = FIXTURES / "rss_feed.xml"
    episodes = fetch_episodes(feed_url=str(fixture_path))

    assert len(episodes) == 4

    # Sorted by pub_date descending
    assert episodes[0].episode_number == 251
    assert episodes[1].episode_number == 250
    assert episodes[2].episode_number == 249
    assert episodes[3].episode_number is None

    ep251 = episodes[0]
    assert ep251.guid == "63857ef5-723d-4d90-86d3-af128612b2d1"
    assert ep251.title == "MINING NEMO? PEP with Chas & Dr Dave (Ep 251, 20 March)"
    assert ep251.duration_seconds == 12175
    assert "PEP251_AUD.mp3" in ep251.audio_url
    assert ep251.description == "Episode 251 description here."


def test_fetch_episodes_bonus_has_no_episode_number():
    episodes = fetch_episodes(feed_url=str(FIXTURES / "rss_feed.xml"))
    bonus = [e for e in episodes if "Bonus" in e.title]
    assert len(bonus) == 1
    assert bonus[0].episode_number is None
    assert bonus[0].duration_seconds == 2730
