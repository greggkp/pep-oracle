import json
import tempfile
from pathlib import Path

from pep_oracle.models import TranscriptSegment
from pep_oracle.transcripts.diarize import (
    SpeakerSegment,
    align_speakers,
    host_roster_from_title,
    load_speaker_profiles,
    map_speaker_names,
    save_speaker_profiles,
    _save_cache,
    _load_cached,
)


def _nonexistent_profile_path():
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        return Path(f.name + ".nonexistent")


# --- title-aware host roster ---


def test_host_roster_chas_and_dave():
    assert host_roster_from_title(
        "I'LL HAVE WHAT XI'S HAVING! PEP with Chas & Dr Dave (Ep 262, 22 May)"
    ) == ["Chas", "Dave"]


def test_host_roster_david_variant():
    assert host_roster_from_title("PEP with Chas and Dr David Smith (Ep 1)") == ["Chas", "Dave"]


def test_host_roster_guest_no_dave():
    # Guest co-hosts must NOT pull in 'Dave'.
    assert host_roster_from_title("PEP with Chas and Melina Wicks (14 July)") == ["Chas"]
    assert host_roster_from_title("N.S.ACKED! PEP with Chas & Elle Hardy (Ep 210)") == ["Chas"]


def test_map_speaker_names_roster_assigns_by_speaking_time():
    # SPEAKER_01 talks far more than SPEAKER_00, so it must become the first
    # roster name (Chas); the other becomes Dave.
    segments = [
        TranscriptSegment(text="a", start_time=0.0, end_time=2.0, speaker="SPEAKER_00"),
        TranscriptSegment(text="b", start_time=2.0, end_time=12.0, speaker="SPEAKER_01"),
    ]
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=2.0),    # 2s
        SpeakerSegment(speaker="SPEAKER_01", start=2.0, end=12.0),   # 10s
    ]
    result = map_speaker_names(
        segments, speaker_segments,
        profile_path=_nonexistent_profile_path(), roster=["Chas", "Dave"],
    )
    assert result[1].speaker == "Chas"  # most speaking time
    assert result[0].speaker == "Dave"


def test_map_speaker_names_roster_chas_only_substantive_guest():
    # Dave absent from roster; a substantive (>=15%) second speaker -> Guest.
    segments = [
        TranscriptSegment(text="a", start_time=0.0, end_time=8.0, speaker="SPEAKER_00"),
        TranscriptSegment(text="b", start_time=8.0, end_time=12.0, speaker="SPEAKER_01"),
    ]
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=8.0),    # 8s (67%)
        SpeakerSegment(speaker="SPEAKER_01", start=8.0, end=12.0),   # 4s (33%)
    ]
    result = map_speaker_names(
        segments, speaker_segments,
        profile_path=_nonexistent_profile_path(), roster=["Chas"],
    )
    assert result[0].speaker == "Chas"
    assert result[1].speaker == "Guest"


def test_map_speaker_names_skips_small_tail_cluster():
    # A tiny non-top cluster (Lachie/fragment, <15%) is skipped (speaker=None),
    # not mislabeled Dave/Guest.
    segments = [
        TranscriptSegment(text="lots", start_time=0.0, end_time=90.0, speaker="SPEAKER_00"),
        TranscriptSegment(text="bit", start_time=90.0, end_time=95.0, speaker="SPEAKER_01"),
    ]
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=90.0),   # 90s (95%)
        SpeakerSegment(speaker="SPEAKER_01", start=90.0, end=95.0),  # 5s  (5%)
    ]
    result = map_speaker_names(
        segments, speaker_segments,
        profile_path=_nonexistent_profile_path(), roster=["Chas", "Dave"],
    )
    assert result[0].speaker == "Chas"
    assert result[1].speaker is None  # 5% tail -> skipped, NOT 'Dave'


def test_map_speaker_names_no_roster_still_generic():
    # Without roster or profiles, behavior is unchanged (generic labels).
    segments = [
        TranscriptSegment(text="a", start_time=0.0, end_time=5.0, speaker="SPEAKER_00"),
    ]
    speaker_segments = [SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=5.0)]
    result = map_speaker_names(segments, speaker_segments, profile_path=_nonexistent_profile_path())
    assert result[0].speaker == "Speaker 1"


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
        def remote(self, audio_url, num_speakers, max_speakers):
            calls.append((audio_url, num_speakers, max_speakers))
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

    assert calls == [("https://example.com/ep.mp3", 2, None)]
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
        def remote(self, audio_url, num_speakers, max_speakers):
            received["num_speakers"] = num_speakers
            received["max_speakers"] = max_speakers
            return []

    class FakeModal:
        class Function:
            @staticmethod
            def from_name(app_name, func_name):
                return FakeRemote()

    monkeypatch.setattr(diarize_module, "modal", FakeModal)

    diarize_module.diarize_audio("https://example.com/ep.mp3")
    assert received["num_speakers"] is None
    assert received["max_speakers"] is None


def test_get_speaker_segments_diarizes_uncapped_by_default(tmp_path, monkeypatch):
    """Default diarization is unconstrained (DEFAULT_MAX_SPEAKERS is None);
    over-clustering is handled by the mapping, not by capping."""
    from pep_oracle.transcripts import diarize as diarize_mod

    monkeypatch.setattr(diarize_mod, "DIARIZATION_CACHE_DIR", tmp_path)
    captured = {}

    def _fake_diarize_audio(audio_url, num_speakers=None, max_speakers=None):
        captured["max_speakers"] = max_speakers
        return [diarize_mod.SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=5.0)]

    monkeypatch.setattr(diarize_mod, "diarize_audio", _fake_diarize_audio)
    diarize_mod.get_speaker_segments(audio_url="https://x", episode_guid="g-cap")
    assert diarize_mod.DEFAULT_MAX_SPEAKERS is None
    assert captured["max_speakers"] is None


def test_get_speaker_segments_uses_cache(tmp_path, monkeypatch):
    """If a diarization cache file exists, get_speaker_segments returns it without calling Modal."""
    from pep_oracle.transcripts.diarize import (
        SpeakerSegment, get_speaker_segments, _save_cache,
    )
    from pep_oracle import config

    monkeypatch.setattr(config, "DIARIZATION_CACHE_DIR", tmp_path)
    # Also patch the name imported into diarize.py at import time
    from pep_oracle.transcripts import diarize as diarize_mod
    monkeypatch.setattr(diarize_mod, "DIARIZATION_CACHE_DIR", tmp_path)

    cached = [SpeakerSegment(speaker="S1", start=0.0, end=10.0)]
    _save_cache(cached, tmp_path / "guid-x.json")

    def _boom(*a, **k):
        raise AssertionError("diarize_audio should not be called on cache hit")

    monkeypatch.setattr(diarize_mod, "diarize_audio", _boom)

    result = get_speaker_segments(audio_url="https://x", episode_guid="guid-x")
    assert len(result) == 1
    assert result[0].speaker == "S1"
