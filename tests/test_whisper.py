import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from pep_oracle.models import TranscriptSegment
from pep_oracle.transcripts.whisper import (
    _load_cached,
    _save_cache,
    split_audio,
    transcribe_chunk,
)


def _make_test_mp3(path: Path, duration_seconds: int = 5) -> None:
    """Generate a test MP3 using ffmpeg."""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_seconds}",
         "-acodec", "libmp3lame", "-b:a", "128k", str(path)],
        check=True,
    )


def test_split_audio_small_file(tmp_path):
    """A file under 24MB should not be split."""
    audio_path = tmp_path / "small.mp3"
    _make_test_mp3(audio_path, duration_seconds=5)
    chunks = split_audio(audio_path, tmp_path)
    assert len(chunks) == 1
    assert chunks[0] == (audio_path, 0.0)


def test_split_audio_produces_chunk_files(tmp_path):
    """Forced splitting should produce multiple chunk files."""
    audio_path = tmp_path / "test.mp3"
    _make_test_mp3(audio_path, duration_seconds=10)

    output_dir = tmp_path / "chunks"
    output_dir.mkdir()

    # Set limit to half the file size so we get 2-3 chunks
    half_size = audio_path.stat().st_size // 2
    with patch("pep_oracle.transcripts.whisper.MAX_CHUNK_BYTES", half_size):
        chunks = split_audio(audio_path, output_dir)

    assert len(chunks) > 1
    for chunk_path, offset in chunks:
        assert chunk_path.exists()
        assert chunk_path.suffix == ".mp3"

    # Offsets should be ordered
    offsets = [offset for _, offset in chunks]
    assert offsets == sorted(offsets)
    assert offsets[0] == 0.0


def test_cache_round_trip(tmp_path):
    segments = [
        TranscriptSegment(text="Hello world", start_time=0.0, end_time=2.5),
        TranscriptSegment(text="Second segment", start_time=2.5, end_time=5.0),
    ]
    cache_path = tmp_path / "test.whisper.json"
    _save_cache(segments, cache_path)
    loaded = _load_cached(cache_path)

    assert len(loaded) == 2
    assert loaded[0].text == "Hello world"
    assert loaded[0].start_time == 0.0
    assert loaded[1].end_time == 5.0


def test_transcribe_chunk_applies_offset(tmp_path):
    """Verify that offset is added to segment timestamps."""
    chunk_path = tmp_path / "chunk.mp3"
    _make_test_mp3(chunk_path, duration_seconds=2)
    offset = 120.0

    mock_segment = MagicMock()
    mock_segment.text = " Some text "
    mock_segment.start = 0.0
    mock_segment.end = 2.0

    mock_response = MagicMock()
    mock_response.segments = [mock_segment]

    mock_client = MagicMock()
    mock_client.audio.transcriptions.create.return_value = mock_response

    segments = transcribe_chunk(chunk_path, offset, mock_client)

    assert len(segments) == 1
    assert segments[0].text == "Some text"
    assert segments[0].start_time == 120.0
    assert segments[0].end_time == 122.0
