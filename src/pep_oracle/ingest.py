import logging
import time
from concurrent.futures import ThreadPoolExecutor

from pep_oracle.chunking import chunk_transcript
from pep_oracle.embeddings import embed_texts
from pep_oracle.transcripts.manager import get_transcript

logger = logging.getLogger(__name__)


def _run_transcribe_and_diarize(
    episode,
    diarize_enabled: bool,
    progress_callback,
) -> tuple[list, str, list | None, float, float]:
    """Run Modal transcription and (optionally) Modal diarization concurrently.

    Returns (segments, source, speaker_segments_or_None, transcribe_elapsed, diarize_elapsed).
    If diarize_enabled is False, speaker_segments is None and diarize_elapsed is 0.0.
    """
    from pep_oracle.transcripts.diarize import get_speaker_segments

    def _time(fn, *a, **k):
        s = time.monotonic()
        r = fn(*a, **k)
        return r, time.monotonic() - s

    with ThreadPoolExecutor(max_workers=2) as pool:
        t_future = pool.submit(_time, get_transcript, episode, progress_callback=progress_callback)
        d_future = None
        if diarize_enabled:
            d_future = pool.submit(
                _time,
                get_speaker_segments,
                episode.audio_url,
                episode.guid,
                progress_callback=progress_callback,
            )

        (segments, source), t_elapsed = t_future.result()
        if d_future:
            speaker_segments, d_elapsed = d_future.result()
        else:
            speaker_segments, d_elapsed = None, 0.0

    return segments, source, speaker_segments, t_elapsed, d_elapsed


def episode_chunks_and_embeddings(
    episode,
    *,
    diarize: bool = False,
    profile_path=None,
    progress_callback=None,
):
    """Transcribe → (optionally) diarize → chunk → embed one episode.

    Returns (chunks, embeddings) — the per-episode work used by the artifact
    ingest (ingest_artifact). Returns ([], []) when the episode yields no chunks.
    No storage writes here.
    """
    segments, _source, speaker_segments, _t_elapsed, _d_elapsed = _run_transcribe_and_diarize(
        episode, diarize, progress_callback
    )
    if diarize:
        from pep_oracle.transcripts.diarize import (
            apply_diarization,
            host_roster_from_title,
            load_cluster_info,
        )

        roster = host_roster_from_title(episode.title)
        clusters = load_cluster_info(episode.guid)
        segments = apply_diarization(
            segments,
            speaker_segments,  # type: ignore[arg-type]
            profile_path=profile_path,
            roster=roster,
            clusters=clusters,
        )
    chunks = chunk_transcript(segments, episode)
    if not chunks:
        return [], []
    embeddings = embed_texts([c.text for c in chunks])
    return chunks, embeddings
