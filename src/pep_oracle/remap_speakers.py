"""One-off remediation: relabel diarized chunks whose speakers are generic
``Speaker N`` labels to host/guest names, in place, without re-embedding.

Chunk embeddings derive from the transcript text, not the speaker labels
(`ingest.py`), so we reuse the existing vectors and only rewrite
`speaker_text` / `speaker_turns` (and, via `add_chunks`, the `has_speaker_*`
metadata). Per episode we delete and re-add the chunks with corrected metadata
and their original embeddings, which clears the stale `has_speaker_speaker_N`
keys that a metadata merge would leave behind.
"""

import json
import re

from pep_oracle.models import Chunk
from pep_oracle.store import SENTINEL_NO_TIME, add_chunks, delete_episode
from pep_oracle.transcripts.diarize import (
    assign_names_by_speaking_time,
    host_roster_from_title,
)

_GENERIC_LABEL = re.compile(r"^Speaker \d+$")
_LABEL_IN_TEXT = re.compile(r"\[([^\]]+)\]")


def _parse_turns(raw) -> list[dict]:
    if not raw:
        return []
    return json.loads(raw) if isinstance(raw, str) else raw


def speaking_times_from_turns(turn_lists: list[list[dict]]) -> dict[str, float]:
    """Aggregate per-label speaking time across an episode's chunks.

    Chunk overlap double-counts some regions, but that only affects absolute
    durations, not the relative ranking used for assignment.
    """
    times: dict[str, float] = {}
    for turns in turn_lists:
        for t in turns:
            spk = t.get("speaker")
            if spk is None:
                continue
            times[spk] = times.get(spk, 0.0) + (t.get("end", 0.0) - t.get("start", 0.0))
    return times


def relabel_speaker_text(text: str | None, name_map: dict[str, str]) -> str | None:
    if text is None:
        return None
    return _LABEL_IN_TEXT.sub(lambda m: f"[{name_map.get(m.group(1), m.group(1))}]", text)


def relabel_turns(turns: list[dict], name_map: dict[str, str]) -> list[dict]:
    return [
        {**t, "speaker": name_map.get(t.get("speaker"), t.get("speaker"))}
        for t in turns
    ]


def remap_collection(collection) -> dict:
    """Relabel every diarized episode that still uses generic ``Speaker N``
    labels. Idempotent: episodes already mapped (or not diarized) are skipped.

    Returns {guid: {title, name_map, chunks}} for each remapped episode.
    """
    got = collection.get(include=["embeddings", "documents", "metadatas"])
    ids, embs, docs, metas = got["ids"], got["embeddings"], got["documents"], got["metadatas"]

    by_guid: dict[str, list[int]] = {}
    for i, m in enumerate(metas):
        by_guid.setdefault(m["episode_guid"], []).append(i)

    summary: dict[str, dict] = {}
    for guid, idxs in by_guid.items():
        turn_lists = [_parse_turns(metas[i].get("speakers")) for i in idxs]
        label_times = speaking_times_from_turns(turn_lists)
        if not label_times:
            continue  # not diarized
        if not any(_GENERIC_LABEL.match(lbl) for lbl in label_times):
            continue  # already mapped (or no generic labels)

        title = metas[idxs[0]]["episode_title"]
        roster = host_roster_from_title(title)
        name_map = assign_names_by_speaking_time(label_times, roster)

        chunks: list[Chunk] = []
        chunk_embs: list[list[float]] = []
        for i in idxs:
            m = metas[i]
            st, et = m["start_time"], m["end_time"]
            turns = _parse_turns(m.get("speakers"))
            chunks.append(Chunk(
                chunk_id=ids[i],
                episode_guid=guid,
                text=docs[i],
                episode_title=m["episode_title"],
                episode_date=m["episode_date"],
                start_time=None if st == SENTINEL_NO_TIME else st,
                end_time=None if et == SENTINEL_NO_TIME else et,
                episode_number=m.get("episode_number") or None,
                speaker_text=relabel_speaker_text(m.get("speaker_text"), name_map),
                speaker_turns=relabel_turns(turns, name_map) if turns else None,
            ))
            chunk_embs.append(list(embs[i]))

        delete_episode(collection, guid)
        add_chunks(collection, chunks, chunk_embs)
        summary[guid] = {"title": title, "name_map": name_map, "chunks": len(chunks)}

    return summary
