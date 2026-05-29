import json

from pep_oracle.models import Chunk
from pep_oracle.remap_speakers import (
    relabel_speaker_text,
    relabel_turns,
    remap_collection,
    speaking_times_from_turns,
)
from pep_oracle.store import add_chunks, get_client


def test_speaking_times_aggregates_across_chunks():
    turn_lists = [
        [{"speaker": "Speaker 1", "start": 0.0, "end": 10.0}],
        [{"speaker": "Speaker 1", "start": 0.0, "end": 5.0},
         {"speaker": "Speaker 2", "start": 5.0, "end": 7.0}],
    ]
    times = speaking_times_from_turns(turn_lists)
    assert times == {"Speaker 1": 15.0, "Speaker 2": 2.0}


def test_relabel_speaker_text_rewrites_only_known_labels():
    text = "[Speaker 1] hello there [Speaker 2] hi back [Speaker 3] guest"
    name_map = {"Speaker 1": "Chas", "Speaker 2": "Dave"}
    out = relabel_speaker_text(text, name_map)
    assert out == "[Chas] hello there [Dave] hi back [Speaker 3] guest"


def test_relabel_turns_preserves_timing():
    turns = [{"speaker": "Speaker 1", "start": 0.0, "end": 5.0}]
    out = relabel_turns(turns, {"Speaker 1": "Chas"})
    assert out == [{"speaker": "Chas", "start": 0.0, "end": 5.0}]


_counter = 0


def _fresh_collection():
    global _counter
    _counter += 1
    client = get_client(persistent=False)
    return client.get_or_create_collection(
        name=f"test_remap_{_counter}", metadata={"hnsw:space": "cosine"}
    )


def _seed_generic_episode(col, title):
    """Seed one diarized chunk that uses generic 'Speaker N' labels (the bug
    state): Speaker 1 talks more than Speaker 2."""
    chunk = Chunk(
        chunk_id="g1_0000", episode_guid="g1",
        text="some transcript text about politics",
        episode_title=title, episode_date="2026-05-01",
        start_time=0.0, end_time=20.0, episode_number=263,
        speaker_text="[Speaker 1] long turn here [Speaker 2] short",
        speaker_turns=[
            {"speaker": "Speaker 1", "start": 0.0, "end": 15.0},
            {"speaker": "Speaker 2", "start": 15.0, "end": 20.0},
        ],
    )
    add_chunks(col, [chunk], [[1.0] + [0.0] * 9])


def test_remap_collection_maps_generic_to_hosts_and_clears_stale_keys():
    col = _fresh_collection()
    _seed_generic_episode(col, "PEP with Chas & Dr Dave (Ep 263)")

    summary = remap_collection(col)
    assert summary["g1"]["name_map"] == {"Speaker 1": "Chas", "Speaker 2": "Dave"}

    got = col.get(include=["metadatas", "documents"])
    meta = got["metadatas"][0]
    # Mapped host keys present, raw labels gone.
    assert meta.get("has_speaker_chas") is True
    assert meta.get("has_speaker_dave") is True
    assert not any(k.startswith("has_speaker_speaker_") for k in meta)
    # speaker_text relabeled; embeddings/text untouched.
    assert "[Chas]" in meta["speaker_text"] and "[Speaker 1]" not in meta["speaker_text"]
    assert got["documents"][0] == "some transcript text about politics"
    turns = json.loads(meta["speakers"])
    assert {t["speaker"] for t in turns} == {"Chas", "Dave"}


def test_remap_collection_guest_episode_no_false_dave():
    col = _fresh_collection()
    _seed_generic_episode(col, "PEP with Chas and Melina Wicks")
    summary = remap_collection(col)
    # Dave absent from the title -> second speaker becomes Guest, not Dave.
    assert summary["g1"]["name_map"] == {"Speaker 1": "Chas", "Speaker 2": "Guest"}
    meta = col.get(include=["metadatas"])["metadatas"][0]
    assert meta.get("has_speaker_chas") is True
    assert meta.get("has_speaker_guest") is True
    assert "has_speaker_dave" not in meta


def test_remap_collection_is_idempotent():
    col = _fresh_collection()
    _seed_generic_episode(col, "PEP with Chas & Dr Dave (Ep 263)")
    remap_collection(col)
    # Second pass: already mapped, nothing generic left -> no-op.
    summary = remap_collection(col)
    assert summary == {}
