import logging

import click

from pep_oracle.chunking import chunk_transcript
from pep_oracle.config import TOPICS_PATH
from pep_oracle.embeddings import embed_texts
from pep_oracle.feed import fetch_episodes
from pep_oracle.models import Episode
from pep_oracle.store import add_chunks, delete_episode, get_client, get_collection, get_ingested_guids
from pep_oracle.topics import clean_episode_topics, parse_description_topics, save_topics
from pep_oracle.transcripts.manager import get_transcript

logger = logging.getLogger(__name__)

WHISPER_COST_PER_MINUTE = 0.001  # Modal L4 ~$0.06/hr of audio


def estimate_whisper_cost(episodes: list[Episode]) -> float:
    total_minutes = sum(
        (ep.duration_seconds or 0) / 60
        for ep in episodes
    )
    return total_minutes * WHISPER_COST_PER_MINUTE


def _ingest_one(episode: Episode, collection, force: bool = False, diarize: bool = False, progress_callback=None) -> bool:
    """Ingest a single episode. Returns True on success."""
    label = f"Ep {episode.episode_number or '?'}: {episode.title[:50]}"

    if force:
        delete_episode(collection, episode.guid)

    if progress_callback:
        progress_callback("transcribing")
    segments, source = get_transcript(
        episode, progress_callback=progress_callback,
    )
    click.echo(f"  Transcript: {source} ({len(segments)} segments)")

    if diarize:
        from pep_oracle.transcripts.diarize import diarize_transcript

        segments = diarize_transcript(
            segments, episode.audio_url, episode.guid,
            progress_callback=progress_callback,
        )

    chunks = chunk_transcript(segments, episode)
    if not chunks:
        click.echo(f"  Skipped (no excerpts produced)")
        return False

    if progress_callback:
        progress_callback(f"embedding {len(chunks)} excerpts")
    click.echo(f"  Embedding {len(chunks)} excerpts...", nl=False)
    embeddings = embed_texts([c.text for c in chunks])
    click.echo(" done")
    if progress_callback:
        progress_callback(f"storing {len(chunks)} excerpts")
    add_chunks(collection, chunks, embeddings)
    click.echo(f"  Stored {len(chunks)} excerpts")

    # Extract topics from show notes (caller batches the save)
    raw_labels = parse_description_topics(episode.description or "")
    cleaned = clean_episode_topics(raw_labels)
    topic_entry = None
    if cleaned:
        topic_entry = {
            "episode_number": episode.episode_number,
            "date": episode.pub_date.strftime("%Y-%m-%d"),
            "topics": cleaned,
        }
    return True, topic_entry


def ingest_all(force: bool = False, confirm_cost: bool = True, episode_numbers: list[int] | None = None, diarize: bool = False, new_only: bool = False, progress_callback=None) -> dict:
    """Ingest all episodes. Returns summary stats."""
    episodes = fetch_episodes()
    logger.info("Fetched %d episodes from RSS feed", len(episodes))
    client = get_client()
    collection = get_collection(client)
    ingested_guids = get_ingested_guids(collection)
    logger.info("Found %d already-ingested GUIDs in ChromaDB", len(ingested_guids))

    if force:
        to_process = episodes
    else:
        to_process = [ep for ep in episodes if ep.guid not in ingested_guids]
        skipped = [ep for ep in episodes if ep.guid in ingested_guids]
        logger.info(
            "%d new episodes to process, %d already ingested",
            len(to_process), len(skipped),
        )
        for ep in to_process:
            logger.info(
                "  New: Ep %s — %s (guid=%s)",
                ep.episode_number or "?", ep.title[:60], ep.guid,
            )
        if not to_process:
            latest_feed = episodes[0] if episodes else None
            latest_ingested = max(
                (ep for ep in episodes if ep.guid in ingested_guids),
                key=lambda ep: ep.pub_date,
                default=None,
            )
            logger.info(
                "Latest in feed: Ep %s (%s, guid=%s)",
                latest_feed.episode_number if latest_feed else "?",
                latest_feed.pub_date.isoformat() if latest_feed else "?",
                latest_feed.guid if latest_feed else "?",
            )
            logger.info(
                "Latest ingested: Ep %s (%s, guid=%s)",
                latest_ingested.episode_number if latest_ingested else "?",
                latest_ingested.pub_date.isoformat() if latest_ingested else "?",
                latest_ingested.guid if latest_ingested else "?",
            )

    if new_only and not force:
        latest_ingested = max(
            (ep for ep in episodes if ep.guid in ingested_guids),
            key=lambda ep: ep.pub_date,
            default=None,
        )
        if latest_ingested is None:
            logger.info("--new-only: no episodes ingested yet; skipping.")
            to_process = []
        else:
            before = len(to_process)
            to_process = [ep for ep in to_process if ep.pub_date > latest_ingested.pub_date]
            logger.info(
                "--new-only: keeping episodes newer than Ep %s (%s): %d → %d",
                latest_ingested.episode_number, latest_ingested.pub_date.isoformat(),
                before, len(to_process),
            )

    if episode_numbers:
        ep_set = set(episode_numbers)
        before = len(to_process)
        to_process = [ep for ep in to_process if ep.episode_number in ep_set]
        logger.info(
            "Filtered by episode_numbers=%s: %d → %d",
            episode_numbers, before, len(to_process),
        )

    if not to_process:
        click.echo("All episodes already ingested.")
        return {"processed": 0, "skipped": len(episodes), "failed": 0}

    already = len(episodes) - len(to_process)
    click.echo(f"{len(to_process)} episodes to process ({already} already ingested)")

    # Estimate Modal GPU cost; gate a large backfill.
    if confirm_cost:
        cost = estimate_whisper_cost(to_process)
        if cost > 10.00:
            click.echo(f"Estimated Modal GPU cost: ${cost:.2f}")
            if not click.confirm("Proceed?"):
                return {"processed": 0, "skipped": len(episodes), "failed": 0}

    # Process oldest first
    to_process.sort(key=lambda ep: ep.pub_date)

    succeeded = 0
    failed = 0
    topic_entries: list[dict] = []
    for i, episode in enumerate(to_process, 1):
        label = f"Ep {episode.episode_number or '?'}"
        click.echo(f"[{i}/{len(to_process)}] {label}: {episode.title[:60]}")
        if progress_callback:
            progress_callback(f"[{i}/{len(to_process)}] {label}: {episode.title[:60]}")
        try:
            ok, topic_entry = _ingest_one(episode, collection, force=force, diarize=diarize, progress_callback=progress_callback)
            if ok:
                succeeded += 1
                if topic_entry:
                    topic_entries.append(topic_entry)
        except Exception as e:
            click.echo(f"  FAILED: {e}")
            failed += 1

    if topic_entries:
        save_topics(topic_entries, TOPICS_PATH)

    click.echo(f"\nDone: {succeeded} ingested, {failed} failed, {already} already up-to-date")
    return {"processed": succeeded, "skipped": already, "failed": failed}


def ingest_episode(episode_id: str, force: bool = False, diarize: bool = False) -> bool:
    """Ingest episode(s) by episode number or GUID."""
    episodes = fetch_episodes()
    client = get_client()
    collection = get_collection(client)

    # Try matching by episode number first (may return multiple for multi-part episodes)
    matches = []
    try:
        num = int(episode_id)
        matches = [ep for ep in episodes if ep.episode_number == num]
    except ValueError:
        pass

    # Fall back to GUID match
    if not matches:
        match = next((ep for ep in episodes if ep.guid == episode_id), None)
        if match:
            matches = [match]

    if not matches:
        raise ValueError(f"No episode found matching: {episode_id}")

    # Sort by pub_date so parts are processed in order
    matches.sort(key=lambda ep: ep.pub_date)

    ingested = get_ingested_guids(collection)
    any_succeeded = False
    topic_entries: list[dict] = []
    for match in matches:
        if match.guid in ingested and not force:
            click.echo(f"Already ingested: {match.title}")
            continue

        click.echo(f"Ingesting: {match.title}")
        ok, topic_entry = _ingest_one(match, collection, force=force, diarize=diarize)
        if ok:
            any_succeeded = True
            if topic_entry:
                topic_entries.append(topic_entry)

    if topic_entries:
        save_topics(topic_entries, TOPICS_PATH)

    if not any_succeeded and all(m.guid in ingested for m in matches):
        click.echo("Use --force to re-ingest.")

    return any_succeeded
