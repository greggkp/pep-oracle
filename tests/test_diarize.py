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
    _activity_by_label,
    _turns_overlap,
    _stitch_equivalences,
    _relabel_and_merge,
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


def test_activity_by_label_clips_to_window():
    segs = [
        SpeakerSegment(speaker="A", start=100, end=150),  # fully inside
        SpeakerSegment(speaker="A", start=140, end=160),  # partial overlap
        SpeakerSegment(speaker="B", start=200, end=210),  # fully outside
    ]
    act = _activity_by_label(segs, window_start=120, window_end=155)
    assert act["A"] == [(120, 150), (140, 155)]
    assert "B" not in act


def test_turns_overlap_sums_pairwise():
    a = [(100.0, 120.0), (140.0, 150.0)]
    b = [(110.0, 145.0)]
    # (100-120)∩(110-145)=10 + (140-150)∩(110-145)=5 → 15
    assert _turns_overlap(a, b) == 15.0


def test_stitch_equivalences_swapped_labels():
    """Chunk 1 calls Chas SPEAKER_00; chunk 2 calls him SPEAKER_01 — stitch
    must catch the swap via overlap-window activity."""
    chunk0 = (0.0, 1530.0, [
        SpeakerSegment(speaker="c0_SPEAKER_00", start=1500, end=1515),  # Chas in overlap
        SpeakerSegment(speaker="c0_SPEAKER_01", start=1520, end=1530),  # Dave in overlap
    ])
    chunk1 = (1500.0, 3030.0, [
        SpeakerSegment(speaker="c1_SPEAKER_01", start=1500, end=1515),  # Chas labelled 01 here
        SpeakerSegment(speaker="c1_SPEAKER_00", start=1520, end=1530),  # Dave labelled 00 here
    ])
    pairs = _stitch_equivalences([chunk0, chunk1])
    assert ("c0_SPEAKER_00", "c1_SPEAKER_01") in pairs
    assert ("c0_SPEAKER_01", "c1_SPEAKER_00") in pairs


def test_stitch_equivalences_empty_overlap():
    chunk0 = (0.0, 10.0, [SpeakerSegment(speaker="c0_SPEAKER_00", start=0, end=5)])
    chunk1 = (20.0, 30.0, [SpeakerSegment(speaker="c1_SPEAKER_00", start=20, end=25)])
    # chunks don't overlap (10 < 20) — no equivalences
    assert _stitch_equivalences([chunk0, chunk1]) == []


def test_relabel_and_merge_unions_equivalent_speakers():
    segs = [
        SpeakerSegment(speaker="c0_SPEAKER_00", start=0, end=100),
        SpeakerSegment(speaker="c0_SPEAKER_01", start=100, end=200),
        SpeakerSegment(speaker="c1_SPEAKER_01", start=200, end=300),  # same as c0_SPEAKER_00
        SpeakerSegment(speaker="c1_SPEAKER_00", start=300, end=400),  # same as c0_SPEAKER_01
    ]
    equivs = [("c0_SPEAKER_00", "c1_SPEAKER_01"), ("c0_SPEAKER_01", "c1_SPEAKER_00")]
    out = _relabel_and_merge(segs, equivs)
    # Two global speakers only
    assert len({s.speaker for s in out}) == 2
    # Time totals per global label should match the real person's total
    totals = {}
    for s in out:
        totals[s.speaker] = totals.get(s.speaker, 0) + (s.end - s.start)
    assert sorted(totals.values()) == [200, 200]


def test_relabel_and_merge_merges_adjacent_same_speaker():
    segs = [
        SpeakerSegment(speaker="c0_SPEAKER_00", start=1495, end=1510),
        SpeakerSegment(speaker="c1_SPEAKER_00", start=1500, end=1520),  # overlapping in time
    ]
    equivs = [("c0_SPEAKER_00", "c1_SPEAKER_00")]
    out = _relabel_and_merge(segs, equivs)
    assert len(out) == 1
    assert out[0].start == 1495
    assert out[0].end == 1520


def test_relabel_and_merge_preserves_concurrent_speakers():
    # Two speakers overlapping in time should remain as two segments.
    segs = [
        SpeakerSegment(speaker="c0_SPEAKER_00", start=0, end=10),
        SpeakerSegment(speaker="c0_SPEAKER_01", start=5, end=15),
    ]
    out = _relabel_and_merge(segs, [])
    assert len(out) == 2
    assert len({s.speaker for s in out}) == 2
