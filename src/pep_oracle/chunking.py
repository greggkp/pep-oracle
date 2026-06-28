from pep_oracle.models import Chunk, Episode, TranscriptSegment

TARGET_CHUNK_SECONDS = 240  # 4 minutes
OVERLAP_SECONDS = 30
PAUSE_THRESHOLD_SECONDS = 2.0

# Fallback for segments without timing
TARGET_CHUNK_WORDS = 800
OVERLAP_WORDS = 100


def _has_timing(segments: list[TranscriptSegment]) -> bool:
    return any(s.start_time is not None for s in segments)


def _find_pause_boundary(
    segments: list[TranscriptSegment],
    start_idx: int,
    target_idx: int,
) -> int:
    """Find the best split point near target_idx, preferring pauses > 2s.

    Searches within ±25% of the target window for a pause boundary.
    Returns the index of the last segment before the split.
    """
    search_range = max(1, (target_idx - start_idx) // 4)
    lo = max(start_idx + 1, target_idx - search_range)
    hi = min(len(segments) - 1, target_idx + search_range)

    best_idx = target_idx
    best_gap = 0.0

    for i in range(lo, hi + 1):
        if i >= len(segments):
            continue
        st = segments[i].start_time
        if st is None:
            continue
        prev = segments[i - 1]
        pt = prev.end_time
        if pt is None:
            continue
        gap = st - pt
        if gap > best_gap:
            best_gap = gap
            best_idx = i
    return best_idx


def _build_speaker_text(segments: list[TranscriptSegment]) -> str | None:
    """Build text with speaker labels, merging consecutive same-speaker segments."""
    if not any(s.speaker for s in segments):
        return None

    parts = []
    current_speaker = None
    for s in segments:
        if s.speaker and s.speaker != current_speaker:
            parts.append(f"[{s.speaker}] {s.text}")
            current_speaker = s.speaker
        else:
            parts.append(s.text)
    return " ".join(parts)


def _build_speaker_turns(segments: list[TranscriptSegment]) -> list[dict] | None:
    """Build speaker turn list from segments."""
    if not any(s.speaker for s in segments):
        return None

    turns = []
    current_speaker = None
    turn_start = None
    for s in segments:
        if s.speaker != current_speaker:
            if current_speaker is not None and turn_start is not None:
                turns.append({
                    "speaker": current_speaker,
                    "start": turn_start,
                    "end": s.start_time or turn_start,
                })
            current_speaker = s.speaker
            turn_start = s.start_time
    # Final turn
    if current_speaker is not None and turn_start is not None:
        turns.append({
            "speaker": current_speaker,
            "start": turn_start,
            "end": segments[-1].end_time or turn_start,
        })
    return turns or None


def _make_chunk(
    segments: list[TranscriptSegment],
    episode: Episode,
    chunk_index: int,
) -> Chunk:
    text = " ".join(s.text for s in segments)
    return Chunk(
        chunk_id=f"{episode.guid}_{chunk_index:04d}",
        episode_guid=episode.guid,
        text=text,
        episode_title=episode.title,
        episode_date=episode.pub_date.strftime("%Y-%m-%d"),
        start_time=segments[0].start_time,
        end_time=segments[-1].end_time,
        episode_number=episode.episode_number,
        speaker_text=_build_speaker_text(segments),
        speaker_turns=_build_speaker_turns(segments),
    )


def _chunk_by_time(
    segments: list[TranscriptSegment],
    episode: Episode,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    chunk_start_idx = 0

    while chunk_start_idx < len(segments):
        start_time = segments[chunk_start_idx].start_time or 0.0
        target_end_time = start_time + TARGET_CHUNK_SECONDS

        # Find the segment index closest to our target end time
        target_idx = chunk_start_idx
        for i in range(chunk_start_idx, len(segments)):
            target_idx = i
            seg_time = segments[i].start_time or segments[i].end_time or 0.0
            if seg_time >= target_end_time:
                break

        # If we're at the end, take everything remaining
        if target_idx >= len(segments) - 1:
            chunk_segments = segments[chunk_start_idx:]
            chunks.append(_make_chunk(chunk_segments, episode, len(chunks)))
            break

        # Find a good split point near the target
        split_idx = _find_pause_boundary(segments, chunk_start_idx, target_idx)
        chunk_segments = segments[chunk_start_idx:split_idx]
        if not chunk_segments:
            chunk_segments = segments[chunk_start_idx:chunk_start_idx + 1]
            split_idx = chunk_start_idx + 1

        chunks.append(_make_chunk(chunk_segments, episode, len(chunks)))

        # Move forward, backing up by overlap duration
        overlap_start_time = (segments[split_idx].start_time or 0.0) - OVERLAP_SECONDS
        next_start = split_idx
        for i in range(split_idx - 1, chunk_start_idx, -1):
            seg_time = segments[i].start_time or 0.0
            if seg_time <= overlap_start_time:
                break
            next_start = i
        chunk_start_idx = next_start

    return chunks


def _chunk_by_words(
    segments: list[TranscriptSegment],
    episode: Episode,
) -> list[Chunk]:
    all_words: list[str] = []
    for seg in segments:
        all_words.extend(seg.text.split())

    if not all_words:
        return []

    chunks: list[Chunk] = []
    word_start = 0

    while word_start < len(all_words):
        word_end = min(word_start + TARGET_CHUNK_WORDS, len(all_words))
        text = " ".join(all_words[word_start:word_end])
        chunks.append(Chunk(
            chunk_id=f"{episode.guid}_{len(chunks):04d}",
            episode_guid=episode.guid,
            text=text,
            episode_title=episode.title,
            episode_date=episode.pub_date.strftime("%Y-%m-%d"),
            episode_number=episode.episode_number,
        ))

        if word_end >= len(all_words):
            break
        word_start = word_end - OVERLAP_WORDS

    return chunks


def chunk_transcript(
    segments: list[TranscriptSegment],
    episode: Episode,
) -> list[Chunk]:
    if not segments:
        return []
    if _has_timing(segments):
        return _chunk_by_time(segments, episode)
    return _chunk_by_words(segments, episode)
