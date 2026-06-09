import logging

import click

from pep_oracle.feed import fetch_episodes


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """Query the PEP with Chas and Dr Dave podcast."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        level=level,
    )


@cli.command()
@click.option("--limit", default=0, help="Max episodes to show (0 = all).")
def episodes(limit: int) -> None:
    """List episodes from the RSS feed."""
    from pep_oracle.store import get_client, get_collection, get_ingested_guids

    all_episodes = fetch_episodes()
    to_show = all_episodes[:limit] if limit else all_episodes

    try:
        client = get_client()
        collection = get_collection(client)
        ingested = get_ingested_guids(collection)
    except Exception:
        ingested = set()

    for ep in to_show:
        num = f"Ep {ep.episode_number:>3}" if ep.episode_number else "     "
        date = ep.pub_date.strftime("%Y-%m-%d")
        duration = ""
        if ep.duration_seconds:
            h, remainder = divmod(ep.duration_seconds, 3600)
            m, s = divmod(remainder, 60)
            duration = f" [{h}:{m:02d}:{s:02d}]"
        marker = " *" if ep.guid in ingested else "  "
        click.echo(f"{marker} {num}  {date}{duration}  {ep.title}")

    ingested_count = sum(1 for ep in all_episodes if ep.guid in ingested)
    click.echo(f"\n{len(all_episodes)} episodes total, {ingested_count} ingested (*)")


@cli.command()
@click.option("--force", is_flag=True, help="Re-process already ingested episodes.")
@click.option("--episode", "episode_id", type=str, help="Ingest a specific episode by number or GUID.")
@click.option("--diarize", is_flag=True, help="Run speaker diarization on Modal GPU (requires MODAL_TOKEN_ID/SECRET).")
@click.option("--new-only", is_flag=True, help="Only ingest episodes newer than the latest already ingested (skips historical gaps).")
def ingest(force: bool, episode_id: str | None, diarize: bool, new_only: bool) -> None:
    """Fetch and process episodes."""
    from pep_oracle.ingest import ingest_all, ingest_episode

    if episode_id:
        ingest_episode(episode_id, force=force, diarize=diarize)
    else:
        ingest_all(force=force, diarize=diarize, new_only=new_only)


@cli.command()
@click.argument("question")
@click.option("--top-k", default=10, help="Number of chunks to retrieve.")
def ask(question: str, top_k: int) -> None:
    """Ask a question about the podcast."""
    from pep_oracle.query import ask as do_ask

    answer = do_ask(question, top_k=top_k)

    from rich.console import Console
    from rich.markdown import Markdown
    Console().print(Markdown(answer))


@cli.command(name="eval-retrieval")
@click.option("--corpus", "corpus_uri", default=None,
              help="Eval hybrid retrieval over a corpus artifact (local dir or s3:// base) "
                   "instead of the live ChromaDB. Use PEP_ORACLE_EMBED_BACKEND=bedrock so the "
                   "query embedder matches a Titan artifact.")
def eval_retrieval_cmd(corpus_uri: str | None) -> None:
    """Score retrieval quality (recall@k, MRR) on a labeled query set.

    Default: compare semantic-only vs hybrid over the live ChromaDB (bge-large).
    With --corpus: score hybrid over the parquet artifact (Bedrock-embedded), to
    confirm no regression vs the bge-large baseline before promoting the artifact.
    """
    from pep_oracle.eval_retrieval import (
        evaluate_corpus, format_report, format_single, run_comparison,
    )

    if corpus_uri:
        from pep_oracle.corpus import load_current

        corpus = load_current(corpus_uri)
        click.echo(format_single(f"hybrid({corpus.version})", evaluate_corpus(corpus)))
    else:
        click.echo(format_report(run_comparison()))


@cli.command(name="export")
@click.argument("output", type=click.Path())
@click.option("--episode", "episode_nums", type=int, multiple=True, help="Episode number(s) to export (default: all).")
def export_cmd(output: str, episode_nums: tuple[int, ...]) -> None:
    """Export ingested episodes to a JSON file for transfer."""
    import json
    from pep_oracle.store import get_client, get_collection, export_episodes

    client = get_client()
    collection = get_collection(client)
    nums = list(episode_nums) if episode_nums else None
    items = export_episodes(collection, nums)
    if not items:
        click.echo("No matching episodes to export.")
        return

    eps = {it["metadata"].get("episode_number") for it in items}
    with open(output, "w") as f:
        json.dump(items, f)
    click.echo(f"Exported {len(items)} chunks from {len(eps)} episodes to {output}")


@cli.command(name="import")
@click.argument("input_file", type=click.Path(exists=True))
def import_cmd(input_file: str) -> None:
    """Import episodes from an exported JSON file."""
    import json
    from pep_oracle.store import get_client, get_collection, import_chunks

    with open(input_file) as f:
        items = json.load(f)

    eps = {it["metadata"].get("episode_number") for it in items}
    click.echo(f"Importing {len(items)} chunks from {len(eps)} episodes...")

    client = get_client()
    collection = get_collection(client)
    count = import_chunks(collection, items)
    click.echo(f"Done — {count} chunks upserted into {collection.name}")


@cli.command(name="ingest-artifact")
@click.option("--dest", default=None, help="Corpus base (local dir or s3:// URI). Default: PEP_ORACLE_CORPUS_URI.")
@click.option("--no-diarize", is_flag=True, help="Skip speaker diarization.")
@click.option("--backfill", is_flag=True, help="Ingest EVERY feed episode the corpus lacks "
              "(old gaps + unnumbered EXTRAs), not just newer-than-max. Expensive; operator-run.")
def ingest_artifact_cmd(dest: str | None, no_diarize: bool, backfill: bool) -> None:
    """Incremental artifact ingest: publish a new corpus version with new feed episodes.

    Default is newest-forward (only numbered episodes newer than the corpus max). Use
    --backfill for a deliberate, supervised catch-up of old gaps + EXTRA bonus episodes.
    """
    from pep_oracle.ingest_artifact import ingest_artifact_incremental

    manifest = ingest_artifact_incremental(dest=dest, diarize=not no_diarize, backfill=backfill)
    if manifest is None:
        click.echo("No new episodes; corpus unchanged.")
    else:
        click.echo(f"Published {manifest.chunk_count} chunks (episodes {manifest.episode_range}).")


@cli.command()
def status() -> None:
    """Show ingestion statistics."""
    from pep_oracle.store import get_client, get_collection, get_ingested_guids
    from pep_oracle.config import CHROMA_DIR

    try:
        client = get_client()
        collection = get_collection(client)
        ingested = get_ingested_guids(collection)
        chunk_count = collection.count()
    except Exception:
        click.echo("No data yet. Run `pep-oracle ingest` first.")
        return

    all_episodes = fetch_episodes()

    click.echo(f"Episodes in RSS feed:  {len(all_episodes)}")
    click.echo(f"Episodes ingested:     {len(ingested)}")
    click.echo(f"Total chunks stored:   {chunk_count}")

    # DB size on disk
    db_size = sum(f.stat().st_size for f in CHROMA_DIR.rglob("*") if f.is_file())
    if db_size > 1_000_000:
        click.echo(f"Database size:         {db_size / 1_000_000:.1f} MB")
    else:
        click.echo(f"Database size:         {db_size / 1_000:.1f} KB")
