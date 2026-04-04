import time

from openai import OpenAI, RateLimitError

from pep_oracle.config import EMBEDDING_MODEL

BATCH_SIZE = 20
MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0


def embed_texts(
    texts: list[str],
    client: OpenAI | None = None,
    model: str = EMBEDDING_MODEL,
) -> list[list[float]]:
    if client is None:
        client = OpenAI()

    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        response = _embed_with_retry(client, batch, model)
        # Response embeddings are returned in order of input
        sorted_data = sorted(response.data, key=lambda d: d.index)
        all_embeddings.extend(d.embedding for d in sorted_data)

    return all_embeddings


def _embed_with_retry(client: OpenAI, texts: list[str], model: str):
    backoff = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES):
        try:
            return client.embeddings.create(model=model, input=texts)
        except RateLimitError:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(backoff)
            backoff *= 2
