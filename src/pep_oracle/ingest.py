import click

from pep_oracle.chunking import chunk_transcript
from pep_oracle.embeddings import embed_texts
from pep_oracle.feed import fetch_episodes
from pep_oracle.models import Episode
from pep_oracle.store import add_chunks, delete_episode, get_client, get_collection, get_ingested_guids
from pep_oracle.transcripts.manager import get_transcript

WHISPER_COST_PER_MINUTE = 0.006


def estimate_whisper_cost(episodes: list[Episode]) -> float:
    total_minutes = sum(
        (ep.duration_seconds or 0) / 60
        for ep in episodes
    )
    return total_minutes * WHISPER_COST_PER_MINUTE


def _ingest_one(episode: Episode, collection, force: bool = False, progress_callback=None) -> bool:
    """Ingest a single episode. Returns True on success."""
    label = f"Ep {episode.episode_number or '?'}: {episode.title[:50]}"

    if force:
        delete_episode(collection, episode.guid)

    if progress_callback:
        progress_callback("transcribing")
    segments, source = get_transcript(episode, progress_callback=progress_callback)
    click.echo(f"  Transcript: {source} ({len(segments)} segments)")

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
    return True


def ingest_all(force: bool = False, confirm_cost: bool = True, episode_numbers: list[int] | None = None, progress_callback=None) -> dict:
    """Ingest all episodes. Returns summary stats."""
    episodes = fetch_episodes()
    client = get_client()
    collection = get_collection(client)
    ingested_guids = get_ingested_guids(collection)

    if force:
        to_process = episodes
    else:
        to_process = [ep for ep in episodes if ep.guid not in ingested_guids]

    if episode_numbers:
        ep_set = set(episode_numbers)
        to_process = [ep for ep in to_process if ep.episode_number in ep_set]

    if not to_process:
        click.echo("All episodes already ingested.")
        return {"processed": 0, "skipped": len(episodes), "failed": 0}

    already = len(episodes) - len(to_process)
    click.echo(f"{len(to_process)} episodes to process ({already} already ingested)")

    # Estimate cost for episodes that will need Whisper
    if confirm_cost:
        cost = estimate_whisper_cost(to_process)
        if cost > 0.50:
            click.echo(f"Estimated max Whisper cost: ${cost:.2f}")
            click.echo("(Episodes with Apple transcripts will be free)")
            if not click.confirm("Proceed?"):
                return {"processed": 0, "skipped": len(episodes), "failed": 0}

    # Process oldest first
    to_process.sort(key=lambda ep: ep.pub_date)

    succeeded = 0
    failed = 0
    for i, episode in enumerate(to_process, 1):
        label = f"Ep {episode.episode_number or '?'}"
        click.echo(f"[{i}/{len(to_process)}] {label}: {episode.title[:60]}")
        if progress_callback:
            progress_callback(f"[{i}/{len(to_process)}] {label}: {episode.title[:60]}")
        try:
            if _ingest_one(episode, collection, force=force, progress_callback=progress_callback):
                succeeded += 1
        except Exception as e:
            click.echo(f"  FAILED: {e}")
            failed += 1

    click.echo(f"\nDone: {succeeded} ingested, {failed} failed, {already} already up-to-date")
    return {"processed": succeeded, "skipped": already, "failed": failed}


def ingest_episode(episode_id: str, force: bool = False) -> bool:
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
    for match in matches:
        if match.guid in ingested and not force:
            click.echo(f"Already ingested: {match.title}")
            continue

        click.echo(f"Ingesting: {match.title}")
        if _ingest_one(match, collection, force=force):
            any_succeeded = True

    if not any_succeeded and all(m.guid in ingested for m in matches):
        click.echo("Use --force to re-ingest.")

    return any_succeeded
