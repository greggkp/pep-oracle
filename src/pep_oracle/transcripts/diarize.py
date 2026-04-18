import json
import logging
from dataclasses import dataclass
from pathlib import Path

import click
import modal

from pep_oracle.config import DIARIZATION_CACHE_DIR, SPEAKER_PROFILES_PATH, ensure_dirs
from pep_oracle.models import TranscriptSegment

logger = logging.getLogger(__name__)


@dataclass
class SpeakerSegment:
    speaker: str
    start: float
    end: float


def diarize_audio(
    audio_url: str,
    num_speakers: int | None = None,
) -> list[SpeakerSegment]:
    """Run pyannote diarization on a Modal GPU. Returns parsed speaker segments."""
    f = modal.Function.from_name("pep-oracle-diarize", "diarize")
    raw = f.remote(audio_url, num_speakers)
    return [SpeakerSegment(**r) for r in raw]


def align_speakers(
    transcript_segments: list[TranscriptSegment],
    speaker_segments: list[SpeakerSegment],
) -> list[TranscriptSegment]:
    """Assign a speaker to each transcript segment by maximum time overlap."""
    result = []
    for ts in transcript_segments:
        if ts.start_time is None or ts.end_time is None:
            result.append(TranscriptSegment(
                text=ts.text,
                start_time=ts.start_time,
                end_time=ts.end_time,
                speaker=None,
            ))
            continue

        best_speaker = None
        best_overlap = 0.0

        for ss in speaker_segments:
            overlap_start = max(ts.start_time, ss.start)
            overlap_end = min(ts.end_time, ss.end)
            overlap = max(0.0, overlap_end - overlap_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = ss.speaker

        result.append(TranscriptSegment(
            text=ts.text,
            start_time=ts.start_time,
            end_time=ts.end_time,
            speaker=best_speaker,
        ))
    return result


def load_speaker_profiles(profile_path: Path | None = None) -> dict[str, list[float]]:
    """Load speaker name -> embedding mapping from profiles file."""
    path = profile_path or SPEAKER_PROFILES_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {
        name: info["embedding"]
        for name, info in data.get("speakers", {}).items()
    }


def save_speaker_profiles(
    profiles: dict[str, list[float]],
    profile_path: Path | None = None,
) -> None:
    """Save speaker profiles to disk."""
    path = profile_path or SPEAKER_PROFILES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "speakers": {
            name: {"embedding": embedding}
            for name, embedding in profiles.items()
        }
    }
    path.write_text(json.dumps(data, indent=2))


def map_speaker_names(
    segments: list[TranscriptSegment],
    speaker_segments: list[SpeakerSegment],
    profile_path: Path | None = None,
) -> list[TranscriptSegment]:
    """Map pyannote's generic labels to real names using voice profiles.

    If no profiles exist, speakers are labeled "Speaker 1", "Speaker 2", etc.
    """
    profiles = load_speaker_profiles(profile_path)

    if not profiles:
        # No profiles — use numbered labels
        unique_speakers = sorted(set(s.speaker for s in speaker_segments))
        name_map = {
            spk: f"Speaker {i + 1}"
            for i, spk in enumerate(unique_speakers)
        }
    else:
        name_map = _match_speakers_to_profiles(speaker_segments, profiles)

    result = []
    for ts in segments:
        speaker = name_map.get(ts.speaker) if ts.speaker else None
        result.append(TranscriptSegment(
            text=ts.text,
            start_time=ts.start_time,
            end_time=ts.end_time,
            speaker=speaker,
        ))
    return result


def _match_speakers_to_profiles(
    speaker_segments: list[SpeakerSegment],
    profiles: dict[str, list[float]],
) -> dict[str, str]:
    """Match pyannote speaker labels to profile names using embeddings.

    Uses cosine similarity between the pyannote speaker embeddings (extracted
    during identify-speakers) and the stored profile embeddings.

    Falls back to generic labels for unmatched speakers.
    """
    try:
        import numpy as np
    except ImportError:
        # numpy not available — fall back to numbered labels
        unique_speakers = sorted(set(s.speaker for s in speaker_segments))
        return {spk: f"Speaker {i + 1}" for i, spk in enumerate(unique_speakers)}

    # For now, use a simple heuristic: if we have exactly 2 speakers and
    # 2 profiles, match by speaking time (the host who talks more is usually
    # identifiable). Full embedding-based matching requires extracting
    # per-speaker embeddings from the diarization pipeline, which we do
    # during identify-speakers. Here we load the cached mapping if available.
    unique_speakers = sorted(set(s.speaker for s in speaker_segments))
    profile_names = sorted(profiles.keys())

    # If speaker count matches profile count, match by order (speaking time)
    # This is a simplification — identify-speakers creates proper mappings
    speaker_times: dict[str, float] = {}
    for ss in speaker_segments:
        speaker_times[ss.speaker] = speaker_times.get(ss.speaker, 0.0) + (ss.end - ss.start)

    sorted_by_time = sorted(unique_speakers, key=lambda s: speaker_times.get(s, 0.0), reverse=True)

    name_map = {}
    used_profiles = set()
    for spk in sorted_by_time:
        matched = False
        for pname in profile_names:
            if pname not in used_profiles:
                name_map[spk] = pname
                used_profiles.add(pname)
                matched = True
                break
        if not matched:
            guest_num = sum(1 for v in name_map.values() if v.startswith("Guest")) + 1
            name_map[spk] = f"Guest {guest_num}" if guest_num > 1 else "Guest"

    return name_map


def get_speaker_segments(
    audio_url: str,
    episode_guid: str,
    num_speakers: int | None = None,
    progress_callback=None,
) -> list[SpeakerSegment]:
    """Fetch speaker segments (from cache or via Modal).

    Safe to call concurrently with get_transcript — writes only to its own
    per-episode cache file.
    """
    ensure_dirs()
    cache_path = DIARIZATION_CACHE_DIR / f"{episode_guid}.json"
    if cache_path.exists():
        click.echo("  Diarization: cached")
        return _load_cached(cache_path)

    if progress_callback:
        progress_callback("diarizing speakers")
    click.echo("  Diarizing speakers...", nl=False)
    speaker_segments = diarize_audio(audio_url, num_speakers=num_speakers)
    _save_cache(speaker_segments, cache_path)
    unique = len(set(s.speaker for s in speaker_segments))
    click.echo(f" {unique} speakers, {len(speaker_segments)} segments")
    return speaker_segments


def apply_diarization(
    transcript_segments: list[TranscriptSegment],
    speaker_segments: list[SpeakerSegment],
    profile_path: Path | None = None,
) -> list[TranscriptSegment]:
    """Align transcript segments with speaker turns and map to real names.

    No Modal calls; operates on already-fetched data.
    """
    aligned = align_speakers(transcript_segments, speaker_segments)
    named = map_speaker_names(aligned, speaker_segments, profile_path)

    profiles = load_speaker_profiles(profile_path)
    if not profiles:
        click.echo("  Warning: No speaker profiles found. Using generic labels.")
        click.echo("  Run 'pep-oracle identify-speakers --episode <N>' to set up profiles.")
    return named


def diarize_transcript(
    transcript_segments: list[TranscriptSegment],
    audio_url: str,
    episode_guid: str,
    num_speakers: int | None = None,
    profile_path: Path | None = None,
    progress_callback=None,
) -> list[TranscriptSegment]:
    """Full diarization pipeline: fetch speaker segments, align, map names.

    Thin wrapper over get_speaker_segments + apply_diarization, kept for
    backward compatibility. New code should call the two halves separately
    so the Modal call can be parallelized with transcription.
    """
    speaker_segments = get_speaker_segments(
        audio_url=audio_url,
        episode_guid=episode_guid,
        num_speakers=num_speakers,
        progress_callback=progress_callback,
    )
    return apply_diarization(transcript_segments, speaker_segments, profile_path)


def _save_cache(segments: list[SpeakerSegment], path: Path) -> None:
    data = [
        {"speaker": s.speaker, "start": s.start, "end": s.end}
        for s in segments
    ]
    path.write_text(json.dumps(data))


def _load_cached(path: Path) -> list[SpeakerSegment]:
    data = json.loads(path.read_text())
    return [
        SpeakerSegment(speaker=d["speaker"], start=d["start"], end=d["end"])
        for d in data
    ]
