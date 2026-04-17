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
        patch("pep_oracle.transcripts.manager._load_cached", return_value=FAKE_SEGMENTS),
    ):
        segments, source = get_transcript(_make_episode())
    assert source == "whisper_cached"
    assert len(segments) == 2


def test_invokes_modal_transcribe(tmp_path, monkeypatch):
    """get_transcript calls the Modal transcribe function and caches the result."""
    from pep_oracle.transcripts import whisper as whisper_module

    monkeypatch.setattr(whisper_module, "TRANSCRIPT_CACHE_DIR", tmp_path)
    monkeypatch.setattr("pep_oracle.transcripts.manager.TRANSCRIPT_CACHE_DIR", tmp_path)

    calls = []

    class FakeRemote:
        def remote(self, audio_url):
            calls.append(audio_url)
            return [
                {"text": "Hello", "start_time": 0.0, "end_time": 1.0},
                {"text": "World", "start_time": 1.0, "end_time": 2.0},
            ]

    class FakeModal:
        class Function:
            @staticmethod
            def from_name(app_name, func_name):
                assert app_name == "pep-oracle-transcribe"
                assert func_name == "transcribe"
                return FakeRemote()

    monkeypatch.setattr(whisper_module, "modal", FakeModal)

    segments, source = get_transcript(_make_episode())

    assert calls == ["https://example.com/test.mp3"]
    assert source == "whisper"
    assert len(segments) == 2
    assert segments[0].text == "Hello"
    assert segments[1].start_time == 1.0
    assert (tmp_path / "test-guid-123.whisper.json").exists()


def test_transcribe_cache_round_trip(tmp_path, monkeypatch):
    """Second call with cached transcript returns from disk without touching Modal."""
    from pep_oracle.transcripts import whisper as whisper_module

    monkeypatch.setattr(whisper_module, "TRANSCRIPT_CACHE_DIR", tmp_path)
    monkeypatch.setattr("pep_oracle.transcripts.manager.TRANSCRIPT_CACHE_DIR", tmp_path)

    cache_path = tmp_path / "test-guid-123.whisper.json"
    cache_path.write_text(
        '[{"text": "Cached", "start_time": 0.0, "end_time": 1.0}]'
    )

    class ExplodingModal:
        class Function:
            @staticmethod
            def from_name(*a, **kw):
                raise AssertionError("Modal should not be called when cache exists")

    monkeypatch.setattr(whisper_module, "modal", ExplodingModal)

    segments, source = get_transcript(_make_episode())
    assert source == "whisper_cached"
    assert segments[0].text == "Cached"
