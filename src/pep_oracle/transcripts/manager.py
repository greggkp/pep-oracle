from pep_oracle.config import TRANSCRIPT_CACHE_DIR
from pep_oracle.models import Episode, TranscriptSegment
from pep_oracle.transcripts.whisper import _load_cached, transcribe_episode


def _has_cached_whisper_transcript(episode: Episode) -> bool:
    return (TRANSCRIPT_CACHE_DIR / f"{episode.guid}.whisper.json").exists()


def get_transcript(
    episode: Episode,
    progress_callback=None,
) -> tuple[list[TranscriptSegment], str]:
    """Get transcript for an episode via Whisper (Modal).

    Returns (segments, source) where source is "whisper" or "whisper_cached".
    """
    if _has_cached_whisper_transcript(episode):
        cache_path = TRANSCRIPT_CACHE_DIR / f"{episode.guid}.whisper.json"
        return _load_cached(cache_path), "whisper_cached"

    if not episode.audio_url:
        raise RuntimeError(f"No audio URL for episode: {episode.title}")

    segments = transcribe_episode(
        episode.audio_url, episode.guid, progress_callback=progress_callback,
    )
    return segments, "whisper"
