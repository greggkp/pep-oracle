"""MCP server exposing pep-oracle citation search to MCP-capable clients.

Registers a single tool, ``search_pep``, that returns short transcript
citations from the "PEP with Chas and Dr Dave" podcast. Its exported metadata
is intentionally load-bearing: MCP-capable clients use it to decide whether and
how to invoke the tool.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict

from pep_oracle import config, temporal
from pep_oracle import corpus as corpus_mod
from pep_oracle.embeddings import embed_texts
from pep_oracle.hybrid import hybrid_search
from pep_oracle.store import get_ingestion_stats
from pep_oracle.timing import timed


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h}:{m:02d}:{s:02d}"


# NOTE: This string is load-bearing AND front-loaded on purpose. Some MCP clients
# defer tools and initially see only the tool name plus truncated metadata. The
# user-goal trigger therefore leads, while source/mechanics detail comes later.
# The exported name is explicit for the same reason. If you edit this, keep the
# trigger in sentence one and re-test direct, indirect, and negative prompts.
SEARCH_PEP_DESCRIPTION = (
    "Use this when a user asks about US politics, elections, federal policy, political "
    "figures, the Supreme Court, or federal agencies—or shares a news article on one of "
    "those subjects—and would benefit from background, interpretation, predictions, or "
    "contrasting viewpoints, even if they do not name this source. For breaking news or "
    "current facts, use this alongside current sources rather than as the sole factual "
    "source. Do not use this for non-US politics. It searches the 'PEP with Chas and Dr "
    "Dave' podcast and returns citable transcript excerpts with episode, date, speaker, "
    "and timestamp. Results default to relevance rather than recency; use intent='current' "
    "for evolving current topics, intent='evolution' or intent='prediction' for "
    "oldest-first timelines, episode_number for a specific episode, and date filters for "
    "an explicit window. The corpus summary identifies the newest indexed episode."
)

SEARCH_TOOL_NAME = "search_us_politics_commentary"
SEARCH_TOOL_TITLE = "Search PEP US Politics Commentary"
MAX_QUERY_LENGTH = 4_000
MAX_TOP_K = 20
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

SearchIntent = Literal["current", "historical", "evolution", "prediction", "timeless"]


class CorpusCoverage(TypedDict):
    """Episode coverage returned with every search."""

    newest_episode: Annotated[
        int | None,
        Field(description="Highest numbered episode in the indexed corpus."),
    ]
    newest_episode_date: Annotated[
        str | None,
        Field(description="Publication date of the newest indexed episode in YYYY-MM-DD format."),
    ]
    oldest_episode: Annotated[
        int | None,
        Field(description="Lowest numbered episode in the indexed corpus."),
    ]


class TranscriptCitation(TypedDict):
    """One grounded podcast transcript match."""

    episode_number: Annotated[
        int | None,
        Field(description="Podcast episode number, or null for an unnumbered bonus episode."),
    ]
    episode_title: Annotated[str, Field(description="Podcast episode title.")]
    episode_date: Annotated[
        str,
        Field(description="Episode publication date in YYYY-MM-DD format."),
    ]
    timestamp: Annotated[
        str,
        Field(description="Human-readable excerpt start time as H:MM:SS."),
    ]
    start_seconds: Annotated[
        float | None,
        Field(description="Excerpt start offset in seconds, or null when unavailable."),
    ]
    end_seconds: Annotated[
        float | None,
        Field(description="Excerpt end offset in seconds, or null when unavailable."),
    ]
    speakers: Annotated[
        list[str],
        Field(description="Mapped host or guest names present in the excerpt."),
    ]
    excerpt: Annotated[str, Field(description="Grounded transcript text suitable for citation.")]


class SearchPepResponse(TypedDict):
    """Structured response from the US-politics commentary search."""

    corpus: CorpusCoverage
    results: list[TranscriptCitation]


# Transport params (stateless_http, path, security) are passed to
# streamable_http_app() by server.mount_mcp_if_configured — in mcp>=2 they
# live on the transport methods, not the constructor.
mcp = MCPServer("pep-oracle")


def format_citation(result: dict) -> TranscriptCitation:
    """Convert a store.query result dict to the MCP citation shape."""
    start = result.get("start_time")
    end = result.get("end_time")
    excerpt = result.get("speaker_text") or result.get("text", "")

    speakers: list[str] = []
    raw_speakers = result.get("speakers")
    if raw_speakers:
        try:
            turns = json.loads(raw_speakers) if isinstance(raw_speakers, str) else raw_speakers
            speakers = sorted({t["speaker"] for t in turns if "speaker" in t})
        except (json.JSONDecodeError, TypeError, KeyError):
            speakers = []

    ep_num = result.get("episode_number")
    # Store uses 0 as a sentinel for "no episode number"
    if ep_num == 0:
        ep_num = None

    return {
        "episode_number": ep_num,
        "episode_title": result.get("episode_title", ""),
        "episode_date": result.get("episode_date", ""),
        "timestamp": format_timestamp(start),
        "start_seconds": start,
        "end_seconds": end,
        "speakers": speakers,
        "excerpt": excerpt,
    }


def get_serving_corpus():
    """Retrieval source: the corpus artifact (InMemoryCorpus), TTL-refreshed and
    version-swapped atomically. Validates dims + embedder against the manifest at
    load. The only serving path (ChromaDB serving was removed in the AWS-only cut)."""
    return corpus_mod.current_corpus(
        config.CORPUS_URI, ttl_seconds=config.CORPUS_REFRESH_TTL_SECONDS
    )


@mcp.tool(
    name=SEARCH_TOOL_NAME,
    title=SEARCH_TOOL_TITLE,
    description=SEARCH_PEP_DESCRIPTION,
    annotations=ToolAnnotations(
        title=SEARCH_TOOL_TITLE,
        read_only_hint=True,
        open_world_hint=False,
    ),
    structured_output=True,
)
def search_pep(
    query: Annotated[
        str,
        Field(
            description=(
                "Natural-language US-politics question, topic, claim, or article context to "
                "match against the podcast transcripts. Include distinctive names or policy "
                "terms when available."
            ),
            min_length=1,
            max_length=MAX_QUERY_LENGTH,
            examples=["Why is the administration challenging Federal Reserve independence?"],
        ),
    ],
    top_k: Annotated[
        int,
        Field(
            description="Maximum number of transcript excerpts to return.",
            ge=1,
            le=MAX_TOP_K,
            examples=[5],
        ),
    ] = 5,
    episode_number: Annotated[
        int | None,
        Field(
            description=(
                "Exact numbered episode to search. Leave null to search the full corpus; use "
                "corpus.newest_episode from a prior result to scope a follow-up to the latest "
                "episode."
            ),
            ge=1,
            examples=[251],
        ),
    ] = None,
    intent: Annotated[
        SearchIntent | None,
        Field(
            description=(
                "Temporal ranking intent: current applies a recency preference; evolution "
                "spreads matches across episodes oldest-first; prediction returns relevant "
                "prediction-to-outcome evidence oldest-first; historical and timeless preserve "
                "pure relevance. Leave null for pure relevance."
            ),
            examples=["current"],
        ),
    ] = None,
    after_date: Annotated[
        str | None,
        Field(
            description="Inclusive earliest episode publication date in YYYY-MM-DD format.",
            pattern=DATE_PATTERN,
            examples=["2026-01-01"],
        ),
    ] = None,
    before_date: Annotated[
        str | None,
        Field(
            description="Inclusive latest episode publication date in YYYY-MM-DD format.",
            pattern=DATE_PATTERN,
            examples=["2026-06-01"],
        ),
    ] = None,
) -> SearchPepResponse:
    if not query or not query.strip():
        raise ValueError("query must not be empty")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be at most {MAX_QUERY_LENGTH} characters")
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
    with timed("search.total"):
        with timed("search.embed"):
            embedding = embed_texts([query])[0]
        # Fresh collection: the API server is long-lived but episodes are written
        # by a separate ingest process, so a cached client would serve stale data.
        with timed("search.corpus_fetch"):
            collection = get_serving_corpus()
        # Pull a candidate pool via hybrid (semantic+BM25) retrieval, then let the
        # shared temporal layer select + order the final top_k for the caller intent.
        with timed("search.hybrid"):
            candidates = hybrid_search(
                collection,
                query,
                embedding,
                top_k=top_k * temporal.CANDIDATE_MULTIPLIER,
                episode_numbers=[episode_number] if episode_number else None,
                after_date=after_date,
                before_date=before_date,
            )
        results, order = temporal.select_for_intent(candidates, intent, top_k, date.today())
        results = sorted(
            results,
            key=lambda r: r.get("episode_date", ""),
            reverse=(order != temporal.CHRONOLOGICAL),
        )
        with timed("search.stats"):
            stats = get_ingestion_stats(collection)
    # Corpus summary lets the caller answer "latest episode" questions: results
    # are ranked by relevance, not recency, so the newest episode may be absent.
    return {
        "corpus": {
            "newest_episode": stats["latest_episode"],
            "newest_episode_date": stats["latest_date"],
            "oldest_episode": stats["earliest_episode"],
        },
        "results": [format_citation(r) for r in results],
    }
