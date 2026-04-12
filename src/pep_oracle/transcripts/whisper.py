import json
import subprocess
import tempfile
from pathlib import Path

import click
from openai import OpenAI

from pep_oracle.config import TRANSCRIPT_CACHE_DIR, ensure_dirs
from pep_oracle.models import TranscriptSegment

MAX_CHUNK_BYTES = 24 * 1024 * 1024  # 24MB to stay under 25MB API limit


def _get_duration_seconds(audio_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def split_audio(audio_path: Path, output_dir: Path) -> list[tuple[Path, float]]:
    """Split audio into chunks under MAX_CHUNK_BYTES using ffmpeg.

    Returns list of (chunk_path, offset_seconds) tuples.
    """
    file_size = audio_path.stat().st_size
    if file_size <= MAX_CHUNK_BYTES:
        return [(audio_path, 0.0)]

    duration = _get_duration_seconds(audio_path)
    num_chunks = (file_size // MAX_CHUNK_BYTES) + 1
    chunk_duration = duration / num_chunks

    chunks = []
    for i in range(num_chunks):
        offset = i * chunk_duration
        chunk_path = output_dir / f"chunk_{i:03d}.mp3"
        subprocess.run(
            ["ffmpeg", "-v", "quiet", "-y",
             "-ss", str(offset), "-t", str(chunk_duration),
             "-i", str(audio_path), "-acodec", "copy", str(chunk_path)],
            check=True,
        )
        chunks.append((chunk_path, offset))

    return chunks


def transcribe_chunk(chunk_path: Path, offset_seconds: float, client: OpenAI) -> list[TranscriptSegment]:
    """Transcribe a single audio chunk via OpenAI Whisper API."""
    with open(chunk_path, "rb") as audio_file:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    segments = []
    for seg in response.segments:
        segments.append(TranscriptSegment(
            text=seg.text.strip(),
            start_time=seg.start + offset_seconds,
            end_time=seg.end + offset_seconds,
        ))
    return segments


def transcribe_episode(audio_path: Path, episode_guid: str, client: OpenAI | None = None, progress_callback=None) -> list[TranscriptSegment]:
    """Transcribe an audio file, splitting if needed. Caches the result."""
    ensure_dirs()

    cache_path = TRANSCRIPT_CACHE_DIR / f"{episode_guid}.whisper.json"
    if cache_path.exists():
        return _load_cached(cache_path)

    if client is None:
        client = OpenAI()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        if progress_callback:
            progress_callback("splitting audio")
        click.echo("  Splitting audio...", nl=False)
        chunks = split_audio(audio_path, tmp_path)
        click.echo(f" {len(chunks)} parts")

        duration = _get_duration_seconds(audio_path) if len(chunks) > 1 else None
        chunk_duration = duration / len(chunks) if duration else None

        all_segments: list[TranscriptSegment] = []
        for i, (chunk_path, offset) in enumerate(chunks):
            dur_label = f"{chunk_duration / 60:.0f} min" if chunk_duration else "?"
            if progress_callback:
                progress_callback(f"transcribing part {i + 1}/{len(chunks)}")
            click.echo(f"  Transcribing part {i + 1}/{len(chunks)} ({dur_label})...", nl=False)
            new_segments = transcribe_chunk(chunk_path, offset, client)
            all_segments.extend(new_segments)
            click.echo(f" {len(new_segments)} segments")

    _save_cache(all_segments, cache_path)
    return all_segments


def _save_cache(segments: list[TranscriptSegment], path: Path) -> None:
    data = []
    for s in segments:
        entry = {"text": s.text, "start_time": s.start_time, "end_time": s.end_time}
        if s.speaker is not None:
            entry["speaker"] = s.speaker
        data.append(entry)
    path.write_text(json.dumps(data))


def _load_cached(path: Path) -> list[TranscriptSegment]:
    data = json.loads(path.read_text())
    return [
        TranscriptSegment(
            text=d["text"],
            start_time=d["start_time"],
            end_time=d["end_time"],
            speaker=d.get("speaker"),
        )
        for d in data
    ]
