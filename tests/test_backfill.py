
import pep_oracle.backfill as backfill
from pep_oracle import corpus


def _fixture_path():
    import pathlib

    return str(pathlib.Path(__file__).parent / "fixtures" / "export_sample.json")


def _fake_embed(texts):
    # Deterministic 2-d vectors that DIFFER from the fixture's old embeddings.
    return [[float(len(t)), 1.0] for t in texts]


def test_backfill_reembeds_and_publishes_v0001(tmp_path):
    manifest = backfill.backfill(
        export_path=_fixture_path(),
        dest=str(tmp_path),
        version="v0001",
        embed=_fake_embed,
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="deadbee",
        built_at="2026-06-02T00:00:00+00:00",
    )

    assert manifest.chunk_count == 2
    assert manifest.episode_range == [251, 253]
    assert manifest.embed_model == "amazon.titan-embed-text-v2:0"

    c = corpus.load_current(str(tmp_path))
    got = c.get(include=["documents", "embeddings", "metadatas"])
    # ids + text + metadata preserved from the export
    assert got["ids"] == ["ep251-chunk-0", "ep253-chunk-0"]
    assert got["metadatas"][0]["episode_number"] == 251
    assert got["metadatas"][0]["start_time"] == 12.5
    # embeddings REPLACED by the new embedder (old bge-large vectors discarded)
    assert got["embeddings"][0] == [float(len("the byrd rule constrains reconciliation in the senate")), 1.0]
    assert got["embeddings"][0] != [0.111, 0.222, 0.333]


def test_backfill_embeds_each_document_once(tmp_path):
    seen = []

    def counting_embed(texts):
        seen.extend(texts)
        return [[1.0, 2.0] for _ in texts]

    backfill.backfill(
        export_path=_fixture_path(), dest=str(tmp_path), version="v0001",
        embed=counting_embed, embed_model="m", dims=2, git_sha="s",
        built_at="t",
    )
    assert seen == [
        "the byrd rule constrains reconciliation in the senate",
        "section 122 tariffs and the trade deficit",
    ]
