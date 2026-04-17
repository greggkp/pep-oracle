import json
from pathlib import Path

import modal

from pep_oracle.config import TRANSCRIPT_CACHE_DIR, ensure_dirs
from pep_oracle.models import TranscriptSegment


def transcribe_episode(
    audio_url: str,
    episode_guid: str,
    progress_callback=None,
) -> list[TranscriptSegment]:
    """Transcribe an episode via Modal (faster-whisper large-v3 on L4). Caches the result."""
    ensure_dirs()

    cache_path = TRANSCRIPT_CACHE_DIR / f"{episode_guid}.whisper.json"
    if cache_path.exists():
        return _load_cached(cache_path)

    if progress_callback:
        progress_callback("transcribing (Modal)")

    f = modal.Function.from_name("pep-oracle-transcribe", "transcribe")
    raw = f.remote(audio_url)
    segments = [
        TranscriptSegment(
            text=r["text"],
            start_time=r["start_time"],
            end_time=r["end_time"],
        )
        for r in raw
    ]
    _save_cache(segments, cache_path)
    return segments


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
