"""Data-integrity checks over the published corpus artifact.

These catch ingestion defects that unit tests (which seed clean fixtures)
cannot see. The motivating bug: a batch of episodes was ingested with raw
diarization labels (has_speaker_speaker_5, has_speaker_speaker_14, ...) instead
of mapped host names (has_speaker_chas / has_speaker_dave). Speaker-filtered
queries then silently matched nothing. This asserts the mapping actually ran,
turning that into an ingest-time failure instead of a query-time mystery.

Reads metadata from the current corpus artifact (config.CORPUS_URI) — the same
InMemoryCorpus the MCP tool serves — since ChromaDB is no longer in the stack.

Opt-in, never part of the default run:

    pytest tests/test_data_integrity_live.py -v -m live
"""

import re
from collections import defaultdict

import pytest

pytestmark = pytest.mark.live

# Raw, unmapped diarization labels look like has_speaker_speaker_<n>.
_RAW_LABEL = re.compile(r"^has_speaker_speaker_\d+$")


def _episodes_with_speaker_metadata():
    """Return {episode_label: set(has_speaker_* keys)} for every ingested
    episode that carries any diarization speaker metadata at all."""
    import pep_oracle.corpus as corpus
    from pep_oracle import config

    collection = corpus.load_current(config.CORPUS_URI)
    got = collection.get(include=["metadatas"])
    by_episode: dict[str, set[str]] = defaultdict(set)
    for meta in got["metadatas"]:
        speaker_keys = {k for k in meta if k.startswith("has_speaker_")}
        if not speaker_keys:
            continue
        num = meta.get("episode_number") or 0
        label = f"Ep {num} ({meta.get('episode_date', '?')})" if num else meta.get("episode_date", "?")
        by_episode[label] |= speaker_keys
    return by_episode


def test_diarized_episodes_have_mapped_speaker_names():
    """No diarized episode should expose raw 'speaker_N' labels — diarization
    speaker-name mapping must have resolved them to host/guest names."""
    by_episode = _episodes_with_speaker_metadata()
    if not by_episode:
        pytest.skip("no diarized episodes in the collection")

    broken = {
        label: sorted(k for k in keys if _RAW_LABEL.match(k))
        for label, keys in by_episode.items()
        if any(_RAW_LABEL.match(k) for k in keys)
    }
    assert not broken, (
        "Episodes ingested with UNMAPPED diarization labels (speaker-name "
        "mapping failed — these need remap/re-ingest):\n"
        + "\n".join(f"  {label}: {labels}" for label, labels in sorted(broken.items()))
    )


def test_diarized_episodes_identify_at_least_one_host():
    """Every diarized episode should attribute some content to a known host
    (Chas or Dave); an episode with only guest/unknown speakers is suspicious."""
    by_episode = _episodes_with_speaker_metadata()
    if not by_episode:
        pytest.skip("no diarized episodes in the collection")

    missing_hosts = {
        label: sorted(keys)
        for label, keys in by_episode.items()
        if not ({"has_speaker_chas", "has_speaker_dave"} & keys)
    }
    assert not missing_hosts, (
        "Diarized episodes with no Chas/Dave attribution (mapping likely "
        "failed):\n"
        + "\n".join(f"  {label}: {keys}" for label, keys in sorted(missing_hosts.items()))
    )
