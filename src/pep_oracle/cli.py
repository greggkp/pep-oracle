import logging

import click


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """Query the PEP with Chas and Dr Dave podcast."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        level=level,
    )


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
