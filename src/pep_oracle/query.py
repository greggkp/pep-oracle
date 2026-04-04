import anthropic

from pep_oracle.config import QUERY_MODEL
from pep_oracle.embeddings import embed_texts
from pep_oracle.store import get_client, get_collection, query as store_query

SYSTEM_PROMPT = """\
You are a helpful assistant that answers questions about the podcast \
"PEP with Chas and Dr Dave" (a podcast about American politics by \
Australian journalists Chas Licciardello and Dr David Smith).

Answer the question based ONLY on the provided transcript excerpts. \
If the information is not in the excerpts, say so. Always cite which \
episode(s) your answer comes from, including the episode title and date."""


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h}:{m:02d}:{s:02d}"


def build_context(results: list[dict]) -> str:
    sections = []
    for r in results:
        ep_num = f"Ep {r['episode_number']}, " if r.get("episode_number") else ""
        start = format_timestamp(r["start_time"])
        end = format_timestamp(r["end_time"])
        header = f"[{r['episode_title']} ({ep_num}{r['episode_date']}), {start}–{end}]"
        sections.append(f"---\n{header}\n{r['text']}\n---")
    return "\n\n".join(sections)


def ask(
    question: str,
    top_k: int = 10,
    model: str = QUERY_MODEL,
    episode_number: int | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
    openai_client=None,
) -> str:
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Embed the question
    query_embedding = embed_texts([question], client=openai_client)[0]

    # Retrieve relevant chunks
    client = get_client()
    collection = get_collection(client)
    results = store_query(collection, query_embedding, top_k=top_k, episode_number=episode_number)

    if not results:
        return "No relevant content found. Have you ingested any episodes yet?"

    # Build prompt and call Claude
    context = build_context(results)
    user_message = f"TRANSCRIPT EXCERPTS:\n\n{context}\n\nQUESTION: {question}"

    response = anthropic_client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
