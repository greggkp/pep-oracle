from pep_oracle.models import Chunk
from pep_oracle.store import (
    add_chunks,
    delete_episode,
    get_client,
    get_collection,
    get_ingested_guids,
    get_ingestion_stats,
    query,
)


def _make_chunks(guid: str, count: int) -> tuple[list[Chunk], list[list[float]]]:
    chunks = []
    embeddings = []
    for i in range(count):
        chunks.append(Chunk(
            chunk_id=f"{guid}_{i:04d}",
            episode_guid=guid,
            text=f"Chunk {i} text about topic {i}",
            episode_title=f"Episode {guid}",
            episode_date="2026-01-01",
            start_time=float(i * 240),
            end_time=float((i + 1) * 240),
            episode_number=1,
        ))
        # Simple embedding: one-hot-ish vector
        emb = [0.0] * 10
        emb[i % 10] = 1.0
        embeddings.append(emb)
    return chunks, embeddings


_counter = 0


def _fresh_collection():
    global _counter
    _counter += 1
    client = get_client(persistent=False)
    return client.get_or_create_collection(
        name=f"test_{_counter}",
        metadata={"hnsw:space": "cosine"},
    )


def test_add_and_query():
    col = _fresh_collection()
    chunks, embeddings = _make_chunks("ep-1", 3)
    add_chunks(col, chunks, embeddings)

    # Query with the first chunk's embedding should return it first
    results = query(col, embeddings[0], top_k=2)
    assert len(results) == 2
    assert results[0]["chunk_id"] == "ep-1_0000"
    assert results[0]["text"] == "Chunk 0 text about topic 0"
    assert results[0]["episode_guid"] == "ep-1"
    assert results[0]["start_time"] == 0.0


def test_get_ingested_guids():
    col = _fresh_collection()
    chunks1, emb1 = _make_chunks("ep-1", 2)
    chunks2, emb2 = _make_chunks("ep-2", 2)
    add_chunks(col, chunks1, emb1)
    add_chunks(col, chunks2, emb2)

    guids = get_ingested_guids(col)
    assert guids == {"ep-1", "ep-2"}


def test_delete_episode():
    col = _fresh_collection()
    chunks1, emb1 = _make_chunks("ep-1", 3)
    chunks2, emb2 = _make_chunks("ep-2", 2)
    add_chunks(col, chunks1, emb1)
    add_chunks(col, chunks2, emb2)

    assert col.count() == 5
    delete_episode(col, "ep-1")
    assert col.count() == 2
    assert get_ingested_guids(col) == {"ep-2"}


def test_upsert_is_idempotent():
    col = _fresh_collection()
    chunks, embeddings = _make_chunks("ep-1", 3)
    add_chunks(col, chunks, embeddings)
    add_chunks(col, chunks, embeddings)  # same data again
    assert col.count() == 3


def test_export_all_episodes():
    col = _fresh_collection()
    chunks1, emb1 = _make_chunks("ep-1", 2)
    chunks2, emb2 = _make_chunks("ep-2", 3)
    add_chunks(col, chunks1, emb1)
    add_chunks(col, chunks2, emb2)

    from pep_oracle.store import export_episodes

    items = export_episodes(col)
    assert len(items) == 5
    # Each item should have id, document, embedding, metadata
    for item in items:
        assert "id" in item
        assert "document" in item
        assert "embedding" in item
        assert "metadata" in item


def test_export_filtered_by_episode_number():
    col = _fresh_collection()
    chunks1, emb1 = _make_chunks("ep-1", 2)
    chunks2, emb2 = _make_chunks("ep-2", 3)
    add_chunks(col, chunks1, emb1)
    add_chunks(col, chunks2, emb2)

    from pep_oracle.store import export_episodes

    # All chunks use episode_number=1 from _make_chunks, so filter for that
    items = export_episodes(col, episode_numbers=[1])
    assert len(items) == 5  # all have episode_number=1


def test_import_round_trip():
    """Export from one collection and import into another — data should match."""
    col1 = _fresh_collection()
    chunks, emb = _make_chunks("ep-1", 3)
    add_chunks(col1, chunks, emb)

    from pep_oracle.store import export_episodes, import_chunks

    exported = export_episodes(col1)

    col2 = _fresh_collection()
    count = import_chunks(col2, exported)
    assert count == 3
    assert col2.count() == 3
    assert get_ingested_guids(col2) == {"ep-1"}


def test_import_upsert_is_idempotent():
    col = _fresh_collection()
    chunks, emb = _make_chunks("ep-1", 2)
    add_chunks(col, chunks, emb)

    from pep_oracle.store import export_episodes, import_chunks

    exported = export_episodes(col)
    import_chunks(col, exported)  # import same data again
    assert col.count() == 2


def test_query_returns_none_for_missing_times():
    col = _fresh_collection()
    chunk = Chunk(
        chunk_id="ep-1_0000",
        episode_guid="ep-1",
        text="No timing info",
        episode_title="Episode",
        episode_date="2026-01-01",
    )
    add_chunks(col, [chunk], [[1.0] * 10])

    results = query(col, [1.0] * 10, top_k=1)
    assert results[0]["start_time"] is None
    assert results[0]["end_time"] is None


def _make_dated_chunk(guid, ep_num, date, emb_index=0):
    """Create a single chunk with specific episode number and date."""
    chunk = Chunk(
        chunk_id=f"{guid}_0000",
        episode_guid=guid,
        text=f"Content from episode {ep_num}",
        episode_title=f"Episode {ep_num}",
        episode_date=date,
        episode_number=ep_num,
        start_time=0.0,
        end_time=60.0,
    )
    emb = [0.0] * 10
    emb[emb_index % 10] = 1.0
    return chunk, emb


def _collection_with_dated_episodes():
    """Collection with episodes at different dates for filter testing."""
    col = _fresh_collection()
    data = [
        ("ep-220", 220, "2024-06-15", 0),
        ("ep-240", 240, "2025-11-15", 1),
        ("ep-248", 248, "2026-03-06", 2),
        ("ep-251", 251, "2026-03-21", 3),
    ]
    for guid, ep_num, date, idx in data:
        chunk, emb = _make_dated_chunk(guid, ep_num, date, idx)
        add_chunks(col, [chunk], [emb])
    return col


def test_query_filter_after_date():
    col = _collection_with_dated_episodes()
    results = query(col, [1.0] * 10, top_k=10, after_date="2026-03-01")
    ep_nums = {r["episode_number"] for r in results}
    assert ep_nums == {248, 251}


def test_query_filter_before_date():
    col = _collection_with_dated_episodes()
    results = query(col, [1.0] * 10, top_k=10, before_date="2025-12-31")
    ep_nums = {r["episode_number"] for r in results}
    assert ep_nums == {220, 240}


def test_query_filter_date_range():
    col = _collection_with_dated_episodes()
    results = query(col, [1.0] * 10, top_k=10, after_date="2025-01-01", before_date="2026-01-01")
    ep_nums = {r["episode_number"] for r in results}
    assert ep_nums == {240}


def test_query_filter_episode_numbers():
    col = _collection_with_dated_episodes()
    results = query(col, [1.0] * 10, top_k=10, episode_numbers=[248, 251])
    ep_nums = {r["episode_number"] for r in results}
    assert ep_nums == {248, 251}


def test_query_filter_single_episode_number():
    col = _collection_with_dated_episodes()
    results = query(col, [1.0] * 10, top_k=10, episode_numbers=[220])
    assert len(results) == 1
    assert results[0]["episode_number"] == 220


def test_query_filter_combined_episode_and_date():
    col = _collection_with_dated_episodes()
    # Ask for episodes 220 and 248 but only after 2026-01-01 → only 248
    results = query(col, [1.0] * 10, top_k=10, episode_numbers=[220, 248], after_date="2026-01-01")
    ep_nums = {r["episode_number"] for r in results}
    assert ep_nums == {248}


def test_get_ingestion_stats():
    col = _collection_with_dated_episodes()
    stats = get_ingestion_stats(col)
    assert stats["earliest_date"] == "2024-06-15"
    assert stats["latest_date"] == "2026-03-21"
    assert stats["earliest_episode"] == 220
    assert stats["latest_episode"] == 251


def test_get_ingestion_stats_empty():
    col = _fresh_collection()
    stats = get_ingestion_stats(col)
    assert stats["earliest_date"] is None
    assert stats["latest_date"] is None
    assert stats["earliest_episode"] is None
    assert stats["latest_episode"] is None
