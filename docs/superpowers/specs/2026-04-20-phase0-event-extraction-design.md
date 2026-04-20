# Phase 0 — Event Extraction Prototype (design)

**Status:** Draft
**Date:** 2026-04-20
**Part of:** pep-oracle new-GUI rollout (see `docs/new-gui/`)
**Scope:** Offline prototype that tests whether LLM-based event extraction over transcripts + show notes produces coherent events with stable canonical names across episodes. No pipeline integration, no UI, no persistent storage.

---

## Goal

Fail-fast test of two foundational bets in the new GUI design:

- **§2 — event-anchoring.** Responses should be organised around discrete dated real-world events rather than topics or chunks. This works only if events can be extracted reliably in the first place.
- **IO-6 — event extraction pipeline.** The open-question document lists this as "probably warrants a small architectural prototype before committing to a pipeline shape." Phase 0 is that prototype.

Phase 0 answers: *on real episodes from this archive, can we extract events that are coherent, canonically named, and linked across episodes?* If yes, event-anchoring is a viable foundation for the rest of the redesign. If no, the cascade of dependent work (Layouts 1–4, coverage/gap detection, first-reaction/settled split, empty-state curation) stops here.

---

## Non-goals

Phase 0 deliberately does none of the following:

- Integration with the ingestion pipeline (no writes into ChromaDB, no scheduling, no incremental update logic).
- Persistent event storage (results are a flat JSON file).
- UI of any kind (no admin surface, no visualisation).
- Retrieval integration (the sanity queries in Phase 0 are eyeballed against the JSON, not routed through an index).
- Stories as first-class entities (the `parent_story` field is a free-text string; upgrading to an entity waits until Phase 0 validates the concept).
- First-reaction/settled-view split detection (IO-1) — a separate downstream concern.
- Show-notes-indexed coverage/gap detection (IO-5) — Phase 3's problem.

---

## Inputs

Ten episodes, all already ingested and cached locally.

### Recent five (current news cycle)

| Episode | Date | Title (abbrev.) |
|---|---|---|
| 252 | 2026-03-27 | HAVE AN ICE FLIGHT! |
| 253 | 2026-04-03 | HAPPY MIDDLE EASTER! |
| 254 | 2026-04-08 | THE YAWN ULTIMATUM! |
| 255 | 2026-04-10 | LIAR, LIAR, CEASE ON FIRE! |
| 256 | 2026-04-17 | THE AUDACITY OF POPE! |

### Historical arc (Biden withdrawal → Harris swap, July–Aug 2024)

| Episode | Date | Title (abbrev.) |
|---|---|---|
| 170 | 2024-07-26 | VEEP IMPACT! (first episode after Biden's Jul 21 withdrawal) |
| 171 | 2024-08-03 | "WEIRD" SCIENCE |
| 172 | 2024-08-10 | BIG PICK ENERGY (Walz VP pick) |
| 173 | 2024-08-14 | THE HARRISMENT OF DONALD TRUMP |
| 174 | 2024-08-17 | FROM MUSK 'TIL DON |

### Why this selection

The two clusters test different linking regimes. Recent episodes test same-wording-week-to-week linking (easy case). The Biden→Harris arc spans four weeks with wording that evolves as the story unfolds, so it stresses canonical-name resolution under realistic drift (harder case).

### Data access

Transcripts: `~/.pep-oracle/cache/transcripts/{guid}.whisper.json` — list of `{text, start_time, end_time}` segments.
Show notes: `Episode.description` from RSS (HTML with a timestamped agenda block in every episode).

---

## Scope of "event"

An **event** is a discrete real-world happening with a date and a canonical name.

**In scope.**
- Specific dated happenings the hosts react to ("Biden withdrew," "Hungary election result," "Pope's public criticism of Trump policy").
- Multi-day happenings with a clear kickoff ("Iran deadline reached," "Strait of Hormuz blockade begins").

**Out of scope.**
- Topics without dated hooks ("Christian Nationalism," "fertility statistics," "Dave's philosophical take on the 25th Amendment").
- Podcast-structural items ("Correspondence," "Grateful," "Unleashed" as labels).

**Crucial distinction.** Show-note segment labels are *navigational furniture*, not event boundaries. The extractor must look inside every segment regardless of its label and extract dated events from wherever they appear. A specific airman incident discussed inside a Correspondence segment IS an event. Dave's opinion piece on the 25th Amendment, if not tied to a recent dated trigger, is NOT an event.

---

## Architecture

### One call per episode, sequential with a growing registry

Process episodes oldest-first (Ep 170 → 256) so earlier episodes seed canonical names that later episodes can link to.

For each episode, one LLM call:

**Input to the LLM:**
1. System prompt with the event scope rules above.
2. Show notes for this episode (full HTML description).
3. Transcript for this episode (full segment list — ≈50–60k tokens for a 3-hour episode).
4. The current event registry (all events extracted so far, with `event_id`, `canonical_name`, `aliases`, `real_world_date`, `parent_story`, `what_happened`). For early episodes this is empty; by the last episode it will contain all unique events extracted from prior episodes.

**Output from the LLM:** a list of event references for this episode. Each reference is exactly one of:

- **New event** — full event fields: `canonical_name`, `aliases`, `real_world_date`, `parent_story`, `what_happened`, plus this episode's reference (`timestamp_start`, `timestamp_end`, `summary`).
- **Link to existing** — `linked_event_id` + this episode's reference (`timestamp_start`, `timestamp_end`, `summary`).

**Post-call update:** the orchestrator adds new events to the registry (assigning the next `evt_NNN` id) and appends this-episode references to linked events.

### Why this architecture

- **Mirrors the production pipeline.** Eventual integration runs per-episode at ingest time with a registry lookup. Phase 0 tests the real shape.
- **Honest cross-episode test.** The extractor sees prior events and must choose: link or create. The failure mode we care about — missing a link when wording diverges — surfaces naturally.
- **Simplest viable shape.** No second pass, no end-of-run reconciliation, no pre-segmentation. If Phase 0 needs those to work, that itself is information — it means the naive pipeline won't cut it in production either.

### Model choice

- **Primary: Sonnet 4.6** (claude-sonnet-4-6). 200k context, strong structured-output, solid cost for a one-off.
- **Optional follow-up: Haiku 4.5** — re-run on the same 10 episodes to learn whether a cheaper production path is viable. Only run if the Sonnet result looks promising.

### Cost estimate (Sonnet)

Per call: ≈80k input tokens (transcript + show notes + registry) + ≈10k output tokens.
Per-call cost: ≈$0.24 + $0.15 = $0.39.
Full 10-episode run: ≈$3.90. Haiku re-run if triggered: ≈$1.30.

---

## Output schema

A single JSON file at `docs/new-gui/phase0-run-<timestamp>.json`:

```json
{
  "run_id": "phase-0-2026-04-20T21-30-00Z",
  "model": "claude-sonnet-4-6",
  "episodes_processed": [170, 171, 172, 173, 174, 252, 253, 254, 255, 256],
  "events": [
    {
      "event_id": "evt_001",
      "canonical_name": "Biden withdraws from 2024 presidential race",
      "aliases": ["Biden dropping out", "Biden stepping aside", "the withdrawal"],
      "real_world_date": "2024-07-21",
      "parent_story": "Biden → Harris swap",
      "what_happened": "Biden announced he was withdrawing from the 2024 race and endorsed Kamala Harris.",
      "references": [
        {
          "episode_number": 170,
          "timestamp_start": 180.5,
          "timestamp_end": 1290.0,
          "summary": "First-reaction walkthrough of Sunday afternoon announcement and the endorsement of Harris."
        },
        {
          "episode_number": 171,
          "timestamp_start": 45.0,
          "timestamp_end": 500.0,
          "summary": "Retrospective on the announcement's timing and political read."
        }
      ]
    }
  ]
}
```

### Field definitions

- `event_id` — `evt_NNN` assigned by the orchestrator, not the LLM. Stable within one run.
- `canonical_name` — LLM-chosen preferred short name, reused across references.
- `aliases` — alternative phrasings the LLM has seen or would expect. Used at evaluation time for semantic matching.
- `real_world_date` — when the happening actually occurred in the world (ISO date string, or `YYYY-MM` if the happening is multi-day without a clear anchor).
- `parent_story` — free-text story name (e.g. "Biden → Harris swap"). Grouping aid; not required to be unique or entity-like in Phase 0.
- `what_happened` — one-sentence factual summary of the real-world event, not the hosts' take.
- `references[]` — one entry per episode that discusses this event. `timestamp_start/end` are seconds from the start of the episode. `summary` is one sentence on what this episode added to the event's coverage.

---

## Evaluation protocol

### Ground truth

Two hand-written ground-truth episode event lists, written by the user **before** looking at the extractor output:

- **Ep 256** (recent, freshest in memory).
- **One of Ep 170–174** (user's choice, whichever is most memorable from the Biden/Harris arc).

Per ground-truth episode: 5–8 events, each with `canonical_name`, `real_world_date`, `what_happened` (one sentence). No `aliases`, no `references`, no `event_id` — ground truth is just "what should have been extracted from this episode."

Expected total: ≈15 ground-truth events. Writing budget: ≈30 minutes.

### Scoring

Manual judgement by the user after the extractor runs. For each ground-truth event:

- Does the extractor output contain an event that semantically matches? (yes / partial / no)
- If yes, does the extractor's `references[]` entry for this episode point to the right timestamp window (within ≈60 seconds tolerance)?

For each extractor event not in ground truth:
- Is it a real event the user forgot to write down? (extractor credit)
- Is it a false positive — topic, opinion, or hallucination? (extractor penalty)

Metrics:
- **Recall** = matched ground-truth events / total ground-truth events.
- **Precision** = real-event extractor events / total extractor events.

Target semantic match is manual judgement, not string equality. The `aliases` field exists explicitly so the extractor isn't penalised for using a different-but-correct phrasing.

### Cross-episode linking audit

Separate evaluation specific to the Biden/Harris arc. Expected distinct events across Ep 170–174:

- **Biden withdraws from the 2024 race** (referenced in all five episodes expected — the arc's anchor).
- **Harris secures the nomination / rapid consolidation** (probably Ep 170–172).
- **Walz VP pick** (Ep 172 onwards).
- **First joint Harris-Walz rally** (Ep 173).
- Possibly 2–4 others (convention build-up, polling shifts, Trump's reaction).

Target: ≈5–8 distinct events with multiple references each across the arc. **Failure mode:** the extractor produces 20+ events that are all really the same underlying happening.

Audit method: user reads the events list filtered to Ep 170–174 references, counts distinct underlying real-world events, checks that the extractor merged them correctly.

### Sanity queries (light C)

Three natural-language queries, manually matched against the extracted event set:

1. "What did they say about Biden dropping out?" → should surface the withdrawal event + references from Ep 170–174.
2. "Hungarian election coverage" → should surface the Hungary election event from Ep 256 (and any earlier references if they exist).
3. "First reaction to the Harris VP pick" → should surface the Walz-pick event with the earliest reference in Ep 172.

No retrieval infrastructure is built. The test is: open the JSON, ctrl-F or eyeball, verify the right events exist and are linked reasonably.

---

## Deliverables

1. **`scripts/phase0_extract.py`** — one-off Python script that:
   - Reads the 10 target episodes from feed + transcript cache.
   - Runs the per-episode extractor loop.
   - Writes the output JSON.

2. **`docs/new-gui/phase0-run-<timestamp>.json`** — the extracted events, full output.

3. **`docs/new-gui/phase0-eval.md`** — short evaluation report:
   - Recall and precision numbers (from the two ground-truth episodes).
   - Cross-episode linking audit result (distinct-event count for the Biden/Harris arc).
   - Sanity query results (eyeballed — pass / partial / fail per query).
   - List of notable misses (ground-truth events the extractor didn't find).
   - List of notable false positives (extractor events that shouldn't be events).
   - Go/no-go recommendation for Phase 1.

4. **Optional: Haiku follow-up run** — only produced if Sonnet result looks promising. Same deliverables, suffixed `-haiku`.

---

## Kill criteria

Any one of these is a no-go for Phase 1:

- **Recall < 70%** — the extractor misses too many events you know are there.
- **Precision < 70%** — the extractor over-extracts or hallucinates.
- **Linking fails** — the Biden withdrawal fragments into 5 separate events instead of 1 event with 5 references.
- **Canonical names drift badly** — aliases don't save it; even by Ep 256 the extractor is coining new names for recurring events.

One retry with a revised extractor prompt is allowed after a first run that misses a threshold. If the revised run still trips any kill criterion, it is a kill — event-anchoring as a foundation is in doubt and the whole new-GUI cascade pauses. A clear failure on the first run (e.g. recall below 50%, or the Biden withdrawal fragmented into 5+ events) can be called a kill without retry.

---

## Error handling

- **Transcript cache miss** — fall back to re-reading from the ingested Chroma chunks if somehow missing; abort with a clear error otherwise. No Modal re-fetch in Phase 0.
- **LLM response parse failure** — retry once with a clarifying instruction. If still broken, log the raw response and skip that episode; the eval report flags it.
- **Registry drift** — if the LLM invents a `linked_event_id` that doesn't exist in the registry, treat as "new event" and flag in the eval report.

---

## Testing

Phase 0 is itself the test. No unit tests for the extractor script; the whole exercise IS validation.

Minor script-internal tests are fine where cheap (e.g. transcript-loading function, show-notes parser) but not required.

---

## Rollout plan

1. Write the extractor script.
2. Hand-write ground truth for Ep 256 + one historical episode (≈30 min).
3. Dry-run on Ep 170 only (single episode, smallest registry) — confirm output shape is correct, cost is as estimated.
4. Full 10-episode run with Sonnet. Inspect output.
5. Manual scoring against ground truth. Write `phase0-eval.md`.
6. Go/no-go decision. If go, optionally re-run on Haiku to inform production cost.

---

## Success criteria

- Recall ≥ 70% on both ground-truth episodes.
- Precision ≥ 70% on both ground-truth episodes.
- Biden withdrawal appears as ≤ 2 distinct events across Ep 170–174 (target: exactly 1).
- Canonical names for cross-episode events are stable (same name used in first reference and subsequent ones).
- Sanity queries return the expected events on eyeball.

If all four hold, the foundational event-anchoring bet is validated enough to commit to Phase 1 (event-anchored retrieval + minimal Layout 1).
