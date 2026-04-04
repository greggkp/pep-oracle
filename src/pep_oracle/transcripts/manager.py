from pathlib import Path

import click
import requests

from pep_oracle.config import AUDIO_CACHE_DIR, TRANSCRIPT_CACHE_DIR, ensure_dirs
from pep_oracle.models import Episode, TranscriptSegment
from pep_oracle.transcripts.whisper import transcribe_episode


def _has_cached_whisper_transcript(episode: Episode) -> bool:
    return (TRANSCRIPT_CACHE_DIR / f"{episode.guid}.whisper.json").exists()


def download_audio(episode: Episode) -> Path:
    """Download episode audio, returning the local path."""
    ensure_dirs()
    audio_path = AUDIO_CACHE_DIR / f"{episode.guid}.mp3"
    if audio_path.exists():
        return audio_path

    response = requests.get(episode.audio_url, stream=True, timeout=30)
    response.raise_for_status()

    with open(audio_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return audio_path


def get_transcript(
    episode: Episode,
    delete_audio_after: bool = True,
) -> tuple[list[TranscriptSegment], str]:
    """Get transcript for an episode via Whisper.

    Returns (segments, source) where source is "whisper" or "whisper_cached".
    """
    # Check Whisper cache first (cheapest check)
    if _has_cached_whisper_transcript(episode):
        from pep_oracle.transcripts.whisper import _load_cached
        cache_path = TRANSCRIPT_CACHE_DIR / f"{episode.guid}.whisper.json"
        return _load_cached(cache_path), "whisper_cached"

    # Whisper transcription
    if not episode.audio_url:
        raise RuntimeError(f"No audio URL for episode: {episode.title}")

    click.echo("  Downloading audio...", nl=False)
    audio_path = download_audio(episode)
    size_mb = audio_path.stat().st_size / 1_000_000
    click.echo(f" {size_mb:.0f} MB")

    try:
        segments = transcribe_episode(audio_path, episode.guid)
    finally:
        if delete_audio_after and audio_path.exists():
            audio_path.unlink()

    return segments, "whisper"
