# Speaker Diarization Design

## Context

The pep-oracle transcription pipeline uses OpenAI Whisper, which produces timestamped text segments but no speaker attribution. The podcast has two regular hosts (Chas Licciardello and Dr David Smith) plus occasional guests. Without speaker labels, query responses can't distinguish who said what — a significant gap for a two-host political commentary podcast where hosts often hold different positions.

**Goal:** Add speaker diarization so Claude can attribute statements to specific speakers in query responses (e.g., "Chas argued X while Dave countered with Y").

## Approach

Use **pyannote-audio** locally to diarize audio files, then align speaker segments with existing Whisper transcripts. This runs as a new step between transcription and chunking, without replacing the Whisper API pipeline.

## Data Model Changes

### `TranscriptSegment` (models.py)

Add optional `speaker` field:

```python
@dataclass
class TranscriptSegment:
    text: str
    start_time: float | None = None
    end_time: float | None = None
    speaker: str | None = None  # "Chas", "Dave", "Guest", or None
```

### `Chunk` (models.py)

Add optional `speaker_text` and `speaker_turns` fields:

```python
@dataclass
class Chunk:
    chunk_id: str
    episode_guid: str
    text: str                                    # clean text (for embeddings)
    episode_title: str
    episode_date: str
    start_time: float | None = None
    end_time: float | None = None
    episode_number: int | None = None
    speaker_text: str | None = None              # text with [Chas]/[Dave] labels (for Claude prompts)
    speaker_turns: list[dict] | None = None      # [{"speaker": "Dave", "start": 120.5, "end": 135.2}, ...]
```

### ChromaDB Metadata

`_chunk_metadata()` in `store.py` adds:

- `speaker_text` (str) — the speaker-annotated version of the chunk text
- `speakers` (str) — JSON-serialized speaker turn list
- `speaker_list` (str) — comma-separated unique speakers for filtering (e.g., `"Chas,Dave"`)

The `documents` field in ChromaDB remains clean text (no speaker labels), preserving embedding quality.

## New Module: `transcripts/diarize.py`

### Core Functions

- **`diarize_audio(audio_path: Path, num_speakers: int | None = None) -> list[SpeakerSegment]`**
  Runs pyannote-audio's diarization pipeline on an audio file. Returns a list of `SpeakerSegment(speaker: str, start: float, end: float)` where speaker is pyannote's raw label (e.g., "SPEAKER_00").

- **`align_speakers(transcript_segments: list[TranscriptSegment], speaker_segments: list[SpeakerSegment]) -> list[TranscriptSegment]`**
  Assigns a speaker to each transcript segment by finding the speaker segment with maximum time overlap. Returns new `TranscriptSegment` objects with the `speaker` field populated.

- **`map_speaker_names(segments: list[TranscriptSegment], profile_path: Path) -> list[TranscriptSegment]`**
  Maps pyannote's generic labels ("SPEAKER_00") to real names ("Chas", "Dave") using stored voice profiles. Unknown speakers become "Guest" or "Speaker N".

### Caching

Diarization results cached at `~/.pep-oracle/cache/diarization/{guid}.json` — a list of `SpeakerSegment` dicts. If cached, diarization is skipped on re-ingest.

## Speaker Identification

### Voice Profiles

Stored at `~/.pep-oracle/speaker_profiles.json`:

```json
{
  "speakers": {
    "Chas": {"embedding": [...]},
    "Dave": {"embedding": [...]}
  }
}
```

pyannote-audio produces speaker embeddings as part of diarization. During the `identify-speakers` setup command, the user labels a few segments, and we store the average embedding for each known speaker.

### CLI Command: `pep-oracle identify-speakers`

```
pep-oracle identify-speakers --episode 251
```

1. Diarizes the episode audio (or uses cached diarization)
2. Picks representative segments for each detected speaker
3. Plays short clips and asks the user to label: "Who is this? [Chas/Dave/Guest/skip]"
4. Computes and stores the average voice embedding per labeled speaker
5. Once profiles exist, future episodes are auto-matched

**Fallback:** If no profiles exist, speakers are labeled "Speaker 1", "Speaker 2", etc. The system still works — just without name attribution.

## Pipeline Integration

### Modified Flow in `_ingest_one()` (ingest.py)

```
1. get_transcript()           → Whisper segments (unchanged)
2. diarize_segments()         → adds speaker labels to segments  [NEW]
3. chunk_transcript()         → chunks include speaker_text      [MODIFIED]
4. embed_texts()              → embeddings on clean text          (unchanged)
5. add_chunks()               → stores with speaker metadata      [MODIFIED]
```

### Audio Lifecycle Change

Currently, `get_transcript()` can delete the audio file after transcription (`delete_audio_after=True`). With diarization, the audio must be retained until diarization completes. The `delete_audio_after` parameter moves to `_ingest_one()` so it controls cleanup after both transcription and diarization are done.

## Chunking Changes (chunking.py)

`_make_chunk()` builds `speaker_text` by joining segments with speaker prefixes:

- When consecutive segments share a speaker, they're joined without repeating the label
- Speaker label inserted at each speaker change: `"[Dave] I think that's right. And furthermore... [Chas] But what about..."`

The clean `text` field remains a plain join of segment text (no labels), used for embeddings.

## Query Changes (query.py)

### `build_context()`

When building the prompt for Claude, use `speaker_text` instead of `text` if available:

```python
text = r.get("speaker_text") or r["text"]
```

This is backward-compatible — chunks without diarization still work, they just lack speaker attribution.

### `store.py` Query Changes

The `query()` function returns items that include metadata. `speaker_text` and `speaker_list` are added to the returned dicts when present in chunk metadata. `build_context()` reads `speaker_text` from these dicts.

### System Prompt Update

Add to `SYSTEM_PROMPT`:

```
When transcript excerpts include speaker labels like [Chas] or [Dave], attribute 
statements to the specific speaker. Use phrases like "Chas noted that..." or 
"According to Dave..." rather than the generic "they discussed".
```

## CLI Changes

### `pep-oracle ingest`

- Add `--diarize` flag (default: off)
- When `--diarize` is passed, audio is kept after transcription for the diarization step
- Works with `--force` for re-processing existing episodes: `pep-oracle ingest --force --diarize --episode 251`
- If `--diarize` is used but no speaker profiles exist, diarization still runs — speakers are labeled "Speaker 1", "Speaker 2", etc. A warning is printed suggesting `identify-speakers`.

### `pep-oracle identify-speakers --episode <N>`

New command for one-time speaker profile setup (detailed above).

## Dependencies

Add to `pyproject.toml`:

- `pyannote.audio` — speaker diarization pipeline
- `torch` / `torchaudio` — required by pyannote (heavy, ~2GB)

These are large dependencies. Consider making them optional (`pip install pep-oracle[diarize]`) so the base install stays light.

## Environment

New optional env var:
- `HF_TOKEN` — Hugging Face token required to download pyannote models (user must accept the model license at huggingface.co/pyannote/speaker-diarization-3.1)

## Backward Compatibility

- Chunks without speaker data continue to work — all new fields are optional/nullable
- The query pipeline gracefully falls back to clean text when `speaker_text` is absent
- Export/import handles the new metadata fields (present or absent)
- No migration needed for existing ChromaDB data

## Verification

1. **Unit tests:** Mock pyannote pipeline output, test alignment logic, test chunk text generation with speaker labels
2. **Integration test:** Diarize a short test audio file (can use ffmpeg-generated multi-tone audio to simulate speakers), verify segments get labels
3. **End-to-end:** Ingest one episode with `--diarize`, then `pep-oracle ask "what did Chas say about X?"` and verify the response attributes statements to speakers
4. **Backward compat:** Verify existing non-diarized episodes still query correctly
5. **Cache test:** Run diarization twice, verify second run uses cache
