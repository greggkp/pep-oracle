# Voice-embedding speaker ID — implementation plan

## Why
Diarization clustering can't separate Chas/Dave (over-splits to 16-30 clusters or
merges them), so today's mapping labels only the top-2 clusters by speaking time —
leaving ~10-40% of host speech unattributed and risking host swaps. A spike proved
that pyannote's per-cluster **embeddings** separate the voices cleanly:
- same host across episodes: cosine distance 0.005-0.026
- Chas vs Dave: 0.85-0.89
- guests vs either host: ≥0.90
- the **intro speaker** (first ~60s) is Chas in every episode (≤0.026), so Chas can
  be identified with zero manual labeling.

So: match each cluster to reference Chas/Dave voices. This collapses all of a host's
over-split fragments into the right person (near-complete coverage) and auto-excludes
guests.

## Design

**Modal `diarize_modal.diarize`** — return `{"segments": [...], "clusters":
[{"speaker", "seconds", "intro_seconds", "embedding"}]}` via `return_embeddings=True`
(intro_seconds = time spoken in first 60s). Keeps `num_speakers`/`max_speakers`.

**`diarize.py`**
- `diarize_audio` parses the new shape into a `DiarizationData(segments, clusters)`.
- Diarization cache v2 stores both; loader stays back-compatible with the old
  bare-list (segments-only) caches → `clusters=None`.
- `get_speaker_segments` still returns `list[SpeakerSegment]`; add
  `load_cluster_info(guid)` to read the cached clusters.
- `assign_by_voice(clusters, references, total, dist_threshold=0.5, share=0.15)`:
  each cluster → nearest reference; ≤ threshold → that host (collapsing fragments);
  else substantive (≥ share) → Guest; else → None (skip). New primary mapping.
- `map_speaker_names(..., clusters=None)`: if real reference embeddings exist AND
  clusters provided → `assign_by_voice`; else fall back to today's
  `assign_substantive_speakers` (speaking-time) for back-compat.

**References** — `build_references(episodes_with_clusters)`:
- Chas = embedding of the max-`intro_seconds` cluster, averaged over several episodes.
- Dave = on `Dr Dave`-titled episodes, the substantive non-Chas top cluster, averaged.
- Persist to `speaker_profiles.json` (existing `{name: {embedding}}` schema, real
  vectors now). CLI: `pep-oracle build-references`.

**Ingest / remap** — pass `load_cluster_info(guid)` into `apply_diarization`.
`remap-speakers` (reprocess from caches) now voice-matches.

## Rollout
1. Build code + unit tests (cosine match, intro→Chas, reference building, back-compat).
2. `modal deploy cloud/diarize_modal.py`.
3. Clear the 14 diarized caches; re-diarize (populates cluster embeddings).
4. `build-references`; then `remap-speakers` (voice-match) — verify coverage jumps,
   guests excluded, host self-consistency.
5. Commit + push.

## Out of scope
Re-diarizing non-diarized episodes; multi-guest naming (guests stay "Guest").
