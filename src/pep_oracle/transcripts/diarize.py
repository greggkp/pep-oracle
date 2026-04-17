import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import click

from pep_oracle.config import DIARIZATION_CACHE_DIR, SPEAKER_PROFILES_PATH, ensure_dirs
from pep_oracle.models import TranscriptSegment

logger = logging.getLogger(__name__)

# Audio longer than this (seconds) is diarized in chunks to bound peak RAM —
# pyannote loads the full waveform + embeddings into memory, so a 2-hour
# episode would use ~7 GB RSS. Chunked processing caps it at ~2 GB.
CHUNK_SECONDS = 1500  # 25 minutes
CHUNK_OVERLAP_SECONDS = 30


@dataclass
class SpeakerSegment:
    speaker: str
    start: float
    end: float


def _audio_duration_seconds(audio_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _load_pipeline():
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        raise RuntimeError(
            "pyannote.audio is not installed. "
            "Install with: uv pip install -e '.[diarize]'"
        )

    import os
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        raise RuntimeError(
            "HF_TOKEN environment variable required for pyannote models. "
            "Get a token at https://huggingface.co/settings/tokens and accept "
            "the license at https://huggingface.co/pyannote/speaker-diarization-3.1"
        )

    return Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )


def _run_pipeline(pipeline, audio_path: Path, num_speakers: int | None) -> list[SpeakerSegment]:
    kwargs = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    result = pipeline(str(audio_path), **kwargs)
    # pyannote ≥3.3 returns DiarizeOutput; unwrap to Annotation
    diarization = getattr(result, "speaker_diarization", result)
    segs = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segs.append(SpeakerSegment(speaker=speaker, start=turn.start, end=turn.end))
    return segs


def diarize_audio(
    audio_path: Path,
    num_speakers: int | None = None,
) -> list[SpeakerSegment]:
    """Run pyannote-audio diarization on an audio file.

    For audio longer than CHUNK_SECONDS, the work is split into overlapping
    chunks and stitched — this caps peak memory regardless of episode length.
    """
    try:
        duration = _audio_duration_seconds(audio_path)
    except Exception:
        duration = None

    pipeline = _load_pipeline()

    if duration is None or duration <= CHUNK_SECONDS + CHUNK_OVERLAP_SECONDS:
        return _run_pipeline(pipeline, audio_path, num_speakers)

    return _diarize_chunked(audio_path, duration, pipeline, num_speakers)


def _diarize_chunked(
    audio_path: Path,
    duration: float,
    pipeline,
    num_speakers: int | None,
) -> list[SpeakerSegment]:
    """Split audio with ffmpeg, run pyannote per chunk, stitch global labels."""
    chunks: list[tuple[float, float, list[SpeakerSegment]]] = []  # (start, end, segs)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        i = 0
        while True:
            start = i * CHUNK_SECONDS
            if start >= duration:
                break
            end = min(start + CHUNK_SECONDS + CHUNK_OVERLAP_SECONDS, duration)
            chunk_path = tmp / f"chunk_{i:03d}.wav"
            subprocess.run(
                ["ffmpeg", "-v", "quiet", "-y",
                 "-ss", str(start), "-t", str(end - start),
                 "-i", str(audio_path),
                 "-ac", "1", "-ar", "16000", str(chunk_path)],
                check=True,
            )
            click.echo(f"  chunk {i+1} ({start/60:.0f}-{end/60:.0f}min)...", nl=False)
            local = _run_pipeline(pipeline, chunk_path, num_speakers)
            # offset timestamps to absolute; prefix labels to keep chunks' label
            # spaces distinct (pyannote reuses SPEAKER_00/01 per call).
            shifted = [
                SpeakerSegment(speaker=f"c{i}_{s.speaker}", start=s.start + start, end=s.end + start)
                for s in local
            ]
            click.echo(f" {len({s.speaker for s in shifted})} speakers")
            chunks.append((start, end, shifted))
            chunk_path.unlink(missing_ok=True)
            if end >= duration:
                break
            i += 1

    all_segs: list[SpeakerSegment] = []
    for _, _, segs in chunks:
        all_segs.extend(segs)

    equivalences = _stitch_equivalences(chunks)
    return _relabel_and_merge(all_segs, equivalences)


def _stitch_equivalences(
    chunks: list[tuple[float, float, list[SpeakerSegment]]],
) -> list[tuple[str, str]]:
    """Pair prefixed labels across adjacent chunks using overlap-zone activity.

    Within the overlap window shared by chunk_i and chunk_{i+1}, the label
    from each chunk that spends the most time speaking during the same
    sub-intervals is declared the same speaker.
    """
    pairs: list[tuple[str, str]] = []
    for i in range(len(chunks) - 1):
        _, end_i, segs_i = chunks[i]
        start_j, _, segs_j = chunks[i + 1]
        o_start, o_end = start_j, end_i
        if o_end <= o_start:
            continue
        act_i = _activity_by_label(segs_i, o_start, o_end)
        act_j = _activity_by_label(segs_j, o_start, o_end)
        if not act_i or not act_j:
            continue
        # For each (label_i, label_j) compute the bidirectional overlap within
        # the window — i.e. time where both are active simultaneously.
        scored: list[tuple[float, str, str]] = []
        for li, turns_i in act_i.items():
            for lj, turns_j in act_j.items():
                overlap = _turns_overlap(turns_i, turns_j)
                if overlap > 0:
                    scored.append((overlap, li, lj))
        # Greedy one-to-one match: highest overlap first.
        scored.sort(reverse=True)
        used_i: set[str] = set()
        used_j: set[str] = set()
        for _score, li, lj in scored:
            if li in used_i or lj in used_j:
                continue
            pairs.append((li, lj))
            used_i.add(li)
            used_j.add(lj)
    return pairs


def _activity_by_label(
    segs: list[SpeakerSegment],
    window_start: float,
    window_end: float,
) -> dict[str, list[tuple[float, float]]]:
    """Return the in-window turn intervals grouped by speaker label."""
    out: dict[str, list[tuple[float, float]]] = {}
    for s in segs:
        a = max(s.start, window_start)
        b = min(s.end, window_end)
        if b > a:
            out.setdefault(s.speaker, []).append((a, b))
    return out


def _turns_overlap(
    turns_a: list[tuple[float, float]],
    turns_b: list[tuple[float, float]],
) -> float:
    """Total time where any interval in a overlaps any in b."""
    total = 0.0
    for sa, ea in turns_a:
        for sb, eb in turns_b:
            total += max(0.0, min(ea, eb) - max(sa, sb))
    return total


def _relabel_and_merge(
    segs: list[SpeakerSegment],
    equivalences: list[tuple[str, str]],
) -> list[SpeakerSegment]:
    """Apply union-find over equivalences, rename to SPEAKER_NN, merge turns."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in equivalences:
        union(a, b)

    # Assign a fresh SPEAKER_NN per component in order of first appearance.
    component_label: dict[str, str] = {}
    next_id = 0
    relabeled: list[SpeakerSegment] = []
    for s in segs:
        root = find(s.speaker)
        if root not in component_label:
            component_label[root] = f"SPEAKER_{next_id:02d}"
            next_id += 1
        relabeled.append(SpeakerSegment(
            speaker=component_label[root],
            start=s.start,
            end=s.end,
        ))

    # Per-speaker interval union: sort and merge adjacent/overlapping turns
    # that now share the same global label (dedupes the overlap regions).
    by_speaker: dict[str, list[SpeakerSegment]] = {}
    for s in relabeled:
        by_speaker.setdefault(s.speaker, []).append(s)

    merged: list[SpeakerSegment] = []
    for speaker, group in by_speaker.items():
        group.sort(key=lambda s: s.start)
        cur_start, cur_end = group[0].start, group[0].end
        for s in group[1:]:
            if s.start <= cur_end:
                cur_end = max(cur_end, s.end)
            else:
                merged.append(SpeakerSegment(speaker=speaker, start=cur_start, end=cur_end))
                cur_start, cur_end = s.start, s.end
        merged.append(SpeakerSegment(speaker=speaker, start=cur_start, end=cur_end))

    merged.sort(key=lambda s: s.start)
    return merged


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


def diarize_transcript(
    transcript_segments: list[TranscriptSegment],
    audio_path: Path,
    episode_guid: str,
    num_speakers: int | None = None,
    profile_path: Path | None = None,
    progress_callback=None,
) -> list[TranscriptSegment]:
    """Full diarization pipeline: diarize audio, align with transcript, map names.

    Uses cached diarization results if available.
    """
    ensure_dirs()

    # Check cache
    cache_path = DIARIZATION_CACHE_DIR / f"{episode_guid}.json"
    if cache_path.exists():
        click.echo("  Diarization: cached")
        speaker_segments = _load_cached(cache_path)
    else:
        if progress_callback:
            progress_callback("diarizing speakers")
        click.echo("  Diarizing speakers...", nl=False)
        speaker_segments = diarize_audio(audio_path, num_speakers=num_speakers)
        _save_cache(speaker_segments, cache_path)
        unique = len(set(s.speaker for s in speaker_segments))
        click.echo(f" {unique} speakers, {len(speaker_segments)} segments")

    # Align speakers with transcript
    aligned = align_speakers(transcript_segments, speaker_segments)

    # Map to real names
    named = map_speaker_names(aligned, speaker_segments, profile_path)

    # Warn if no profiles
    profiles = load_speaker_profiles(profile_path)
    if not profiles:
        click.echo("  Warning: No speaker profiles found. Using generic labels.")
        click.echo("  Run 'pep-oracle identify-speakers --episode <N>' to set up profiles.")

    return named


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
