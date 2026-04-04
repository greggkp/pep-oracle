from datetime import datetime, timezone
from unittest.mock import patch

from pep_oracle.models import Episode, TranscriptSegment
from pep_oracle.transcripts.manager import get_transcript


def _make_episode(**overrides) -> Episode:
    defaults = dict(
        guid="test-guid-123",
        title="TEST EPISODE (Ep 1, 1 Jan)",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        audio_url="https://example.com/test.mp3",
        description="Test episode",
    )
    defaults.update(overrides)
    return Episode(**defaults)


FAKE_SEGMENTS = [
    TranscriptSegment(text="Hello", start_time=0.0, end_time=1.0),
    TranscriptSegment(text="World", start_time=1.0, end_time=2.0),
]


def test_returns_whisper_cache_if_available(tmp_path):
    with (
        patch("pep_oracle.transcripts.manager._has_cached_whisper_transcript", return_value=True),
        patch("pep_oracle.transcripts.manager.TRANSCRIPT_CACHE_DIR", tmp_path),
        patch("pep_oracle.transcripts.whisper._load_cached", return_value=FAKE_SEGMENTS),
    ):
        segments, source = get_transcript(_make_episode())
    assert source == "whisper_cached"
    assert len(segments) == 2


def test_falls_back_to_whisper(tmp_path):
    audio_path = tmp_path / "test.mp3"
    audio_path.write_bytes(b"fake audio")

    with (
        patch("pep_oracle.transcripts.manager._has_cached_whisper_transcript", return_value=False),

        patch("pep_oracle.transcripts.manager.download_audio", return_value=audio_path),
        patch("pep_oracle.transcripts.manager.transcribe_episode", return_value=FAKE_SEGMENTS),
    ):
        segments, source = get_transcript(_make_episode(), delete_audio_after=False)
    assert source == "whisper"
    assert segments == FAKE_SEGMENTS


def test_deletes_audio_after_whisper(tmp_path):
    audio_path = tmp_path / "test.mp3"
    audio_path.write_bytes(b"fake audio")

    with (
        patch("pep_oracle.transcripts.manager._has_cached_whisper_transcript", return_value=False),

        patch("pep_oracle.transcripts.manager.download_audio", return_value=audio_path),
        patch("pep_oracle.transcripts.manager.transcribe_episode", return_value=FAKE_SEGMENTS),
    ):
        get_transcript(_make_episode(), delete_audio_after=True)
    assert not audio_path.exists()
