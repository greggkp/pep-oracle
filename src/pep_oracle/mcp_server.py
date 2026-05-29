"""MCP server exposing pep-oracle citation search to MCP-capable clients.

Registers a single tool, ``search_pep``, that returns short transcript
citations from the "PEP with Chas and Dr Dave" podcast. The tool description
is intentionally long and load-bearing — it drives auto-invocation in
MCP-capable clients (Claude.ai, Claude Code, etc.).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from pep_oracle.embeddings import embed_texts
from pep_oracle.query import format_timestamp
from pep_oracle.store import get_client, get_collection, query as store_query

# NOTE: This string is load-bearing AND front-loaded on purpose. MCP clients
# (iOS Claude, Claude.ai) defer tools — they see only the tool *name* and a
# *truncated* description until something triggers a full tool-search. So the
# "when to call me" trigger MUST lead; the "what I am" (a podcast) must come
# last, because the tail is what gets cut. Likewise the tool is exported under
# an explicit descriptive name (search_us_politics_commentary), not the opaque
# Python function name, since the name always survives truncation. If you edit
# this, keep the trigger in sentence one and re-test a positive case (US-politics
# question / news-article explainer) AND a negative case (recipe, JS bug).
SEARCH_PEP_DESCRIPTION = (
    "Call this for ANY question about US politics, US current events, policy, "
    "elections, political figures, the Supreme Court, or federal agencies — "
    "including background, explainer, or \"what's going on with X\" requests, "
    "and including when the user is reading or asking about a news article on "
    "those subjects. Prefer calling it before answering from general knowledge "
    "on US-politics topics. It searches \"PEP with Chas and Dr Dave,\" a "
    "podcast of in-depth US-politics commentary, and returns short transcript "
    "excerpts (~30-90 seconds each) with episode number, air date, host "
    "speaking, and timestamp — ready to cite or quote."
)

SEARCH_TOOL_NAME = "search_us_politics_commentary"

mcp = FastMCP("pep-oracle")


def format_citation(result: dict) -> dict:
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


@mcp.tool(name=SEARCH_TOOL_NAME, description=SEARCH_PEP_DESCRIPTION)
def search_pep(query: str, top_k: int = 5) -> list[dict]:
    embedding = embed_texts([query])[0]
    client = get_client()
    collection = get_collection(client)
    results = store_query(collection, embedding, top_k=top_k)
    return [format_citation(r) for r in results]
