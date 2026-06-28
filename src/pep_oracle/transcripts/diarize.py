import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

import click
import modal

from pep_oracle.config import DIARIZATION_CACHE_DIR, SPEAKER_PROFILES_PATH, ensure_dirs
from pep_oracle.models import TranscriptSegment

logger = logging.getLogger(__name__)

# pyannote over-segments this podcast audio into 16-30 micro-clusters, BUT the
# two largest clusters still correspond to the substantive speakers (Chas ~58%,
# Dave ~34%); the long tail is fragments / music / Lachie (the skippable foil).
# Capping max_speakers is the wrong fix — it merges Chas and Dave into a single
# blob. So we leave diarization unconstrained and instead label only the top
# substantive clusters (see assign_substantive_speakers), skipping the rest.
DEFAULT_MAX_SPEAKERS = None

# A non-top cluster must hold at least this share of speaking time to count as a
# real second host/guest; below it we treat it as Lachie/fragments and skip it.
SUBSTANTIVE_SPEAKER_SHARE = 0.15

# Cosine-distance ceiling for matching a cluster to a reference voice. The spike
# measured same-host ≤0.026 and different-voice ≥0.85, so 0.5 is a wide margin.
VOICE_MATCH_MAX_DISTANCE = 0.5


@dataclass
class SpeakerSegment:
    speaker: str
    start: float
    end: float


@dataclass
class DiarizationData:
    """Diarization output: turn segments plus, when available, per-cluster
    centroid voice embeddings keyed by raw label
    ({label: {"embedding", "seconds", "intro_seconds"}}). ``clusters`` is None
    for legacy caches produced before embeddings were captured."""

    segments: list[SpeakerSegment]
    clusters: dict[str, dict] | None = None


def cos_dist(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)


def diarize_audio(
    audio_url: str,
    num_speakers: int | None = None,
    max_speakers: int | None = None,
) -> DiarizationData:
    """Run pyannote diarization on a Modal GPU; returns segments + cluster embeddings."""
    f = modal.Function.from_name("pep-oracle-diarize", "diarize")
    raw = f.remote(audio_url, num_speakers, max_speakers)
    return _parse_diarization(raw)


def _parse_diarization(raw) -> DiarizationData:
    """Parse the Modal return shape. Accepts the legacy bare-segment list too."""
    if isinstance(raw, list):
        return DiarizationData(segments=[SpeakerSegment(**r) for r in raw])
    segments = [SpeakerSegment(**r) for r in raw["segments"]]
    clusters = {
        c["speaker"]: {
            "embedding": c["embedding"],
            "seconds": c["seconds"],
            "intro_seconds": c["intro_seconds"],
        }
        for c in raw.get("clusters", [])
    }
    return DiarizationData(segments=segments, clusters=clusters or None)


def align_speakers(
    transcript_segments: list[TranscriptSegment],
    speaker_segments: list[SpeakerSegment],
) -> list[TranscriptSegment]:
    """Assign a speaker to each transcript segment by maximum time overlap."""
    result = []
    for ts in transcript_segments:
        if ts.start_time is None or ts.end_time is None:
            result.append(
                TranscriptSegment(
                    text=ts.text,
                    start_time=ts.start_time,
                    end_time=ts.end_time,
                    speaker=None,
                )
            )
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

        result.append(
            TranscriptSegment(
                text=ts.text,
                start_time=ts.start_time,
                end_time=ts.end_time,
                speaker=best_speaker,
            )
        )
    return result


def load_speaker_profiles(profile_path: Path | None = None) -> dict[str, list[float]]:
    """Load speaker name -> embedding mapping from profiles file."""
    path = profile_path or SPEAKER_PROFILES_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {name: info["embedding"] for name, info in data.get("speakers", {}).items()}


def save_speaker_profiles(
    profiles: dict[str, list[float]],
    profile_path: Path | None = None,
) -> None:
    """Save speaker profiles to disk."""
    path = profile_path or SPEAKER_PROFILES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"speakers": {name: {"embedding": embedding} for name, embedding in profiles.items()}}
    path.write_text(json.dumps(data, indent=2))


# Chas hosts every episode; Dr Dave (David Smith) co-hosts most but not all.
_DAVE_IN_TITLE = re.compile(r"\bdav(e|id)\b", re.IGNORECASE)


def host_roster_from_title(title: str) -> list[str]:
    """Derive the ordered host roster for an episode from its title.

    Chas is always the primary host. Dr Dave only counts when the title names
    him — otherwise a guest co-host must not be mislabeled as "Dave". The order
    is the priority for speaking-time assignment (roster[0] -> most-talking
    voice). Speakers beyond the roster become Guests.
    """
    roster = ["Chas"]
    if _DAVE_IN_TITLE.search(title or ""):
        roster.append("Dave")
    return roster


def assign_substantive_speakers(
    label_to_time: dict[str, float],
    names: list[str],
) -> dict[str, str | None]:
    """Map only the substantive diarization clusters to ``names``; skip the rest.

    The loudest cluster is always the primary host (names[0] = Chas). Subsequent
    clusters map to names[1], names[2], ... (then "Guest", "Guest 2", ...) ONLY
    if they hold at least SUBSTANTIVE_SPEAKER_SHARE of total speaking time; below
    that they are Lachie / music / over-split fragments and map to None (skipped,
    so they get no speaker label and no has_speaker_* metadata).

    This favors precision over recall: an over-split fragment of a host is left
    unattributed rather than mislabeled as a different person. Real fix for full
    coverage is voice-embedding speaker ID.
    """
    total = sum(label_to_time.values()) or 1.0
    ordered = sorted(label_to_time, key=lambda s: label_to_time.get(s, 0.0), reverse=True)
    name_map: dict[str, str | None] = {}
    guest_count = 0
    for i, spk in enumerate(ordered):
        share = label_to_time.get(spk, 0.0) / total
        if i == 0:
            name_map[spk] = names[0] if names else None
        elif share >= SUBSTANTIVE_SPEAKER_SHARE:
            if i < len(names):
                name_map[spk] = names[i]
            else:
                guest_count += 1
                name_map[spk] = "Guest" if guest_count == 1 else f"Guest {guest_count}"
        else:
            name_map[spk] = None  # Lachie / fragment / music — skip
    return name_map


def assign_by_voice(
    clusters: dict[str, dict],
    references: dict[str, list[float]],
    total_seconds: float,
    max_distance: float = VOICE_MATCH_MAX_DISTANCE,
    substantive_share: float = SUBSTANTIVE_SPEAKER_SHARE,
) -> dict[str, str | None]:
    """Map clusters to reference voices by cosine distance.

    Each cluster -> nearest reference (Chas/Dave) when within ``max_distance``;
    this collapses pyannote's over-split fragments of a host back into that host.
    A non-matching cluster becomes a "Guest" if it is substantive (>= share of
    speaking time), else None (skipped — Lachie / music / fragments).
    """
    name_map: dict[str, str | None] = {}
    guest_count = 0
    ordered = sorted(clusters.items(), key=lambda kv: kv[1].get("seconds", 0.0), reverse=True)
    for label, info in ordered:
        emb = info.get("embedding")
        best_name, best_dist = None, float("inf")
        if emb:
            for name, ref in references.items():
                d = cos_dist(emb, ref)
                if d < best_dist:
                    best_dist, best_name = d, name
        share = info.get("seconds", 0.0) / total_seconds if total_seconds else 0.0
        if best_name is not None and best_dist <= max_distance:
            name_map[label] = best_name
        elif share >= substantive_share:
            guest_count += 1
            name_map[label] = "Guest" if guest_count == 1 else f"Guest {guest_count}"
        else:
            name_map[label] = None
    return name_map


def map_speaker_names(
    segments: list[TranscriptSegment],
    speaker_segments: list[SpeakerSegment],
    profile_path: Path | None = None,
    roster: list[str] | None = None,
    clusters: dict[str, dict] | None = None,
) -> list[TranscriptSegment]:
    """Map pyannote's generic labels to real names.

    Precedence: reference voice embeddings (built by `build-references`) matched
    against the diarization cluster embeddings win when both are available; else
    an episode `roster` (from the title) drives substantive-speaking-time
    assignment; else speakers fall back to "Speaker 1", "Speaker 2", ...
    """
    references = {n: e for n, e in load_speaker_profiles(profile_path).items() if e}

    if references and clusters:
        total = sum(_speaking_times(speaker_segments).values())
        name_map = assign_by_voice(clusters, references, total)
    elif roster:
        name_map = assign_substantive_speakers(_speaking_times(speaker_segments), list(roster))
    else:
        unique_speakers = sorted(set(s.speaker for s in speaker_segments))
        name_map = {spk: f"Speaker {i + 1}" for i, spk in enumerate(unique_speakers)}

    result = []
    for ts in segments:
        speaker = name_map.get(ts.speaker) if ts.speaker else None
        result.append(
            TranscriptSegment(
                text=ts.text,
                start_time=ts.start_time,
                end_time=ts.end_time,
                speaker=speaker,
            )
        )
    return result


def _speaking_times(speaker_segments: list[SpeakerSegment]) -> dict[str, float]:
    times: dict[str, float] = {}
    for ss in speaker_segments:
        times[ss.speaker] = times.get(ss.speaker, 0.0) + (ss.end - ss.start)
    return times


def get_speaker_segments(
    audio_url: str,
    episode_guid: str,
    num_speakers: int | None = None,
    max_speakers: int | None = DEFAULT_MAX_SPEAKERS,
    progress_callback=None,
) -> list[SpeakerSegment]:
    """Fetch speaker segments (from cache or via Modal).

    Safe to call concurrently with get_transcript — writes only to its own
    per-episode cache file. Diarization runs unconstrained by default
    (DEFAULT_MAX_SPEAKERS is None); over-clustering is handled downstream by
    labeling only the substantive clusters, not by capping (which merges hosts).
    """
    ensure_dirs()
    cache_path = DIARIZATION_CACHE_DIR / f"{episode_guid}.json"
    if cache_path.exists():
        click.echo("  Diarization: cached")
        return _load_cached(cache_path).segments

    if progress_callback:
        progress_callback("diarizing speakers")
    click.echo("  Diarizing speakers...", nl=False)
    data = diarize_audio(audio_url, num_speakers=num_speakers, max_speakers=max_speakers)
    _save_cache(data, cache_path)
    click.echo(f" {len(data.clusters or {})} clusters, {len(data.segments)} segments")
    return data.segments


def load_cluster_info(episode_guid: str) -> dict | None:
    """Return cached per-cluster voice info for an episode, or None if the cache
    is absent or predates embedding capture."""
    cache_path = DIARIZATION_CACHE_DIR / f"{episode_guid}.json"
    if not cache_path.exists():
        return None
    return _load_cached(cache_path).clusters


def apply_diarization(
    transcript_segments: list[TranscriptSegment],
    speaker_segments: list[SpeakerSegment],
    profile_path: Path | None = None,
    roster: list[str] | None = None,
    clusters: dict[str, dict] | None = None,
) -> list[TranscriptSegment]:
    """Align transcript segments with speaker turns and map to real names.

    No Modal calls; operates on already-fetched data. Pass `clusters` (from
    `load_cluster_info`) for voice-embedding matching, and `roster` (from
    `host_roster_from_title`) as the speaking-time fallback.
    """
    aligned = align_speakers(transcript_segments, speaker_segments)
    named = map_speaker_names(aligned, speaker_segments, profile_path, roster, clusters)

    references = {n: e for n, e in load_speaker_profiles(profile_path).items() if e}
    if not (references and clusters) and not roster:
        click.echo("  Warning: no voice references or roster; using generic labels.")
        click.echo("  Run 'pep-oracle build-references' to set up voice profiles.")
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
    clusters = load_cluster_info(episode_guid)
    return apply_diarization(transcript_segments, speaker_segments, profile_path, clusters=clusters)


def _save_cache(data: DiarizationData, path: Path) -> None:
    payload = {
        "segments": [{"speaker": s.speaker, "start": s.start, "end": s.end} for s in data.segments],
        "clusters": [{"speaker": label, **info} for label, info in (data.clusters or {}).items()],
    }
    path.write_text(json.dumps(payload))


def _load_cached(path: Path) -> DiarizationData:
    return _parse_diarization(json.loads(path.read_text()))
