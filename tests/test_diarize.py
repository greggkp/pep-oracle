import json
import tempfile
from pathlib import Path

from pep_oracle.models import TranscriptSegment
from pep_oracle.transcripts.diarize import (
    SpeakerSegment,
    align_speakers,
    load_speaker_profiles,
    map_speaker_names,
    save_speaker_profiles,
    _save_cache,
    _load_cached,
)


def test_align_speakers_basic():
    transcript = [
        TranscriptSegment(text="Hello there", start_time=0.0, end_time=5.0),
        TranscriptSegment(text="How are you", start_time=5.0, end_time=10.0),
        TranscriptSegment(text="I'm fine", start_time=10.0, end_time=15.0),
    ]
    speakers = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=8.0),
        SpeakerSegment(speaker="SPEAKER_01", start=8.0, end=15.0),
    ]
    result = align_speakers(transcript, speakers)
    assert result[0].speaker == "SPEAKER_00"
    assert result[1].speaker == "SPEAKER_00"  # 5-8 overlap > 8-10 overlap
    assert result[2].speaker == "SPEAKER_01"


def test_align_speakers_no_timing():
    transcript = [
        TranscriptSegment(text="No timing", start_time=None, end_time=None),
    ]
    speakers = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=10.0),
    ]
    result = align_speakers(transcript, speakers)
    assert result[0].speaker is None


def test_align_speakers_empty():
    result = align_speakers([], [])
    assert result == []


def test_align_speakers_no_overlap():
    transcript = [
        TranscriptSegment(text="Late segment", start_time=100.0, end_time=110.0),
    ]
    speakers = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=5.0),
    ]
    result = align_speakers(transcript, speakers)
    assert result[0].speaker is None  # no overlap found


def test_map_speaker_names_no_profiles():
    segments = [
        TranscriptSegment(text="Hello", start_time=0.0, end_time=5.0, speaker="SPEAKER_00"),
        TranscriptSegment(text="Hi", start_time=5.0, end_time=10.0, speaker="SPEAKER_01"),
    ]
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=5.0),
        SpeakerSegment(speaker="SPEAKER_01", start=5.0, end=10.0),
    ]
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        nonexistent = Path(f.name + ".nonexistent")

    result = map_speaker_names(segments, speaker_segments, profile_path=nonexistent)
    assert result[0].speaker == "Speaker 1"
    assert result[1].speaker == "Speaker 2"


def test_map_speaker_names_with_profiles():
    segments = [
        TranscriptSegment(text="Hello", start_time=0.0, end_time=5.0, speaker="SPEAKER_00"),
        TranscriptSegment(text="Hi", start_time=5.0, end_time=10.0, speaker="SPEAKER_01"),
    ]
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=5.0),
        SpeakerSegment(speaker="SPEAKER_01", start=5.0, end=10.0),
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({
            "speakers": {
                "Chas": {"embedding": []},
                "Dave": {"embedding": []},
            }
        }, f)
        profile_path = Path(f.name)

    result = map_speaker_names(segments, speaker_segments, profile_path=profile_path)
    # With 2 speakers and 2 profiles, should match by speaking time
    names = {result[0].speaker, result[1].speaker}
    assert names == {"Chas", "Dave"}
    profile_path.unlink()


def test_map_speaker_names_with_guest():
    segments = [
        TranscriptSegment(text="Hello", start_time=0.0, end_time=5.0, speaker="SPEAKER_00"),
        TranscriptSegment(text="Hi", start_time=5.0, end_time=10.0, speaker="SPEAKER_01"),
        TranscriptSegment(text="Hey", start_time=10.0, end_time=15.0, speaker="SPEAKER_02"),
    ]
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=5.0),
        SpeakerSegment(speaker="SPEAKER_01", start=5.0, end=10.0),
        SpeakerSegment(speaker="SPEAKER_02", start=10.0, end=15.0),
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({
            "speakers": {
                "Chas": {"embedding": []},
                "Dave": {"embedding": []},
            }
        }, f)
        profile_path = Path(f.name)

    result = map_speaker_names(segments, speaker_segments, profile_path=profile_path)
    names = [r.speaker for r in result]
    assert "Guest" in names
    profile_path.unlink()


def test_speaker_profiles_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "profiles.json"
        profiles = {"Chas": [0.1, 0.2], "Dave": [0.3, 0.4]}
        save_speaker_profiles(profiles, profile_path=path)
        loaded = load_speaker_profiles(profile_path=path)
        assert loaded == profiles


def test_diarization_cache_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.json"
        segments = [
            SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=5.0),
            SpeakerSegment(speaker="SPEAKER_01", start=5.0, end=10.0),
        ]
        _save_cache(segments, path)
        loaded = _load_cached(path)
        assert len(loaded) == 2
        assert loaded[0].speaker == "SPEAKER_00"
        assert loaded[0].start == 0.0
        assert loaded[1].speaker == "SPEAKER_01"


def test_map_speaker_names_preserves_none():
    segments = [
        TranscriptSegment(text="No speaker", start_time=0.0, end_time=5.0, speaker=None),
    ]
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=5.0),
    ]
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        nonexistent = Path(f.name + ".nonexistent")

    result = map_speaker_names(segments, speaker_segments, profile_path=nonexistent)
    assert result[0].speaker is None


def test_diarize_audio_calls_modal(monkeypatch):
    """diarize_audio looks up the deployed Modal function and returns parsed segments."""
    from pep_oracle.transcripts import diarize as diarize_module

    calls = []

    class FakeRemote:
        def remote(self, audio_url, num_speakers):
            calls.append((audio_url, num_speakers))
            return [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.5},
                {"speaker": "SPEAKER_01", "start": 5.5, "end": 10.0},
            ]

    class FakeModal:
        class Function:
            @staticmethod
            def from_name(app_name, func_name):
                assert app_name == "pep-oracle-diarize"
                assert func_name == "diarize"
                return FakeRemote()

    monkeypatch.setattr(diarize_module, "modal", FakeModal)

    result = diarize_module.diarize_audio("https://example.com/ep.mp3", num_speakers=2)

    assert calls == [("https://example.com/ep.mp3", 2)]
    assert len(result) == 2
    assert result[0].speaker == "SPEAKER_00"
    assert result[0].start == 0.0
    assert result[0].end == 5.5
    assert result[1].speaker == "SPEAKER_01"


def test_diarize_audio_no_num_speakers(monkeypatch):
    """num_speakers defaults to None."""
    from pep_oracle.transcripts import diarize as diarize_module

    received = {}

    class FakeRemote:
        def remote(self, audio_url, num_speakers):
            received["num_speakers"] = num_speakers
            return []

    class FakeModal:
        class Function:
            @staticmethod
            def from_name(app_name, func_name):
                return FakeRemote()

    monkeypatch.setattr(diarize_module, "modal", FakeModal)

    diarize_module.diarize_audio("https://example.com/ep.mp3")
    assert received["num_speakers"] is None
