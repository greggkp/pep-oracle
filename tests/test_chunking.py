from datetime import datetime, timezone
from pathlib import Path

from pep_oracle.chunking import (
    OVERLAP_SECONDS,
    TARGET_CHUNK_SECONDS,
    chunk_transcript,
)
from pep_oracle.models import Chunk, Episode, TranscriptSegment

FIXTURES = Path(__file__).parent / "fixtures"


def _make_episode() -> Episode:
    return Episode(
        guid="test-guid",
        title="TEST (Ep 1, 1 Jan)",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        audio_url="https://example.com/test.mp3",
        description="Test",
        episode_number=1,
    )


def _timed_segments(count: int, duration_each: float = 13.0, gap: float = 0.5) -> list[TranscriptSegment]:
    """Generate evenly-spaced timed segments."""
    segments = []
    t = 0.0
    for i in range(count):
        segments.append(TranscriptSegment(
            text=f"Segment {i} with some words to fill space.",
            start_time=t,
            end_time=t + duration_each,
        ))
        t += duration_each + gap
    return segments


def _untimed_segments(count: int, words_each: int = 50) -> list[TranscriptSegment]:
    """Generate segments with no timing info."""
    return [
        TranscriptSegment(text=" ".join(f"word{j}" for j in range(words_each)))
        for _ in range(count)
    ]


def test_empty_segments():
    assert chunk_transcript([], _make_episode()) == []


def test_short_transcript_single_chunk():
    segments = _timed_segments(5)  # ~67s total, well under 4min target
    chunks = chunk_transcript(segments, _make_episode())
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "test-guid_0000"
    assert chunks[0].episode_number == 1


def test_long_transcript_produces_multiple_chunks():
    # 60 segments × 13s = 780s ~= 13 min → should produce 3-5 chunks
    segments = _timed_segments(60)
    chunks = chunk_transcript(segments, _make_episode())
    assert len(chunks) >= 3
    assert len(chunks) <= 6


def test_chunks_cover_full_transcript():
    segments = _timed_segments(60)
    chunks = chunk_transcript(segments, _make_episode())

    # Every segment's text should appear in at least one chunk
    for seg in segments:
        assert any(seg.text in chunk.text for chunk in chunks)


def test_chunks_have_overlap():
    segments = _timed_segments(60)
    chunks = chunk_transcript(segments, _make_episode())

    if len(chunks) < 2:
        return

    # Adjacent chunks should share some text (overlap)
    for i in range(len(chunks) - 1):
        words_a = set(chunks[i].text.split())
        words_b = set(chunks[i + 1].text.split())
        overlap = words_a & words_b
        assert len(overlap) > 0, f"Chunks {i} and {i+1} have no overlap"


def test_chunk_metadata():
    segments = _timed_segments(5)
    episode = _make_episode()
    chunks = chunk_transcript(segments, episode)

    assert chunks[0].episode_guid == "test-guid"
    assert chunks[0].episode_title == "TEST (Ep 1, 1 Jan)"
    assert chunks[0].episode_date == "2026-01-01"
    assert chunks[0].start_time == 0.0


def test_word_based_chunking_without_timing():
    # 40 segments × 50 words = 2000 words → should produce 2-4 chunks
    segments = _untimed_segments(40)
    chunks = chunk_transcript(segments, _make_episode())
    assert len(chunks) >= 2
    assert len(chunks) <= 5


def test_speaker_text_generated():
    """Chunks should have speaker_text when segments have speaker labels."""
    segments = [
        TranscriptSegment(text="I think so.", start_time=0.0, end_time=5.0, speaker="Chas"),
        TranscriptSegment(text="Me too.", start_time=5.0, end_time=10.0, speaker="Chas"),
        TranscriptSegment(text="But wait.", start_time=10.0, end_time=15.0, speaker="Dave"),
    ]
    chunks = chunk_transcript(segments, _make_episode())
    assert len(chunks) == 1
    assert chunks[0].speaker_text == "[Chas] I think so. Me too. [Dave] But wait."
    assert chunks[0].text == "I think so. Me too. But wait."


def test_no_speaker_text_without_labels():
    """Chunks should have None speaker_text when segments lack speaker labels."""
    segments = _timed_segments(5)
    chunks = chunk_transcript(segments, _make_episode())
    assert chunks[0].speaker_text is None
    assert chunks[0].speaker_turns is None


def test_speaker_turns_generated():
    segments = [
        TranscriptSegment(text="Hello.", start_time=0.0, end_time=5.0, speaker="Chas"),
        TranscriptSegment(text="Hi.", start_time=5.0, end_time=10.0, speaker="Dave"),
    ]
    chunks = chunk_transcript(segments, _make_episode())
    turns = chunks[0].speaker_turns
    assert turns is not None
    assert len(turns) == 2
    assert turns[0]["speaker"] == "Chas"
    assert turns[1]["speaker"] == "Dave"


def test_pause_boundary_preferred():
    """A big gap should be preferred as a split point."""
    segments = []
    t = 0.0
    for i in range(40):
        segments.append(TranscriptSegment(
            text=f"Segment {i}.",
            start_time=t,
            end_time=t + 10.0,
        ))
        # Insert a 5-second gap at segment 18 (around the 4-min mark)
        gap = 5.0 if i == 18 else 0.5
        t += 10.0 + gap

    chunks = chunk_transcript(segments, _make_episode())
    # The first chunk should end near the pause at segment 18
    assert chunks[0].end_time is not None
    first_chunk_end = chunks[0].end_time
    pause_time = 18 * 10.5 + 10.0  # approximate time of segment 18's end
    assert abs(first_chunk_end - pause_time) < 60  # within a minute of the pause
