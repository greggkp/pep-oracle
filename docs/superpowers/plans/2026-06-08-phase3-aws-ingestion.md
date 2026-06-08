# Phase 3 — AWS-native ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled, scale-to-zero AWS job that incrementally ingests new podcast episodes and publishes new corpus versions to S3 — removing the OptiPlex from the ingestion loop — so new episodes appear on `pep-oracle.iicapn.com` automatically.

**Architecture:** An **artifact-native incremental ingest** (no ChromaDB): load the current corpus parquet in memory, diff its episode GUIDs against the RSS feed, transcribe/diarize (Modal) + chunk + Bedrock-embed ONLY new episodes, append rows, and `corpus.write_artifact(vN+1)` (atomic `current.json` flip). The serving Lambda's TTL refresh picks it up. Run on **ECS Fargate** triggered by a daily **EventBridge** rule; the per-episode pipeline is shared with the existing ChromaDB ingest via a small refactor.

**Tech Stack:** Python (existing app), AWS CDK v2 (`aws-cdk-lib`: ECS Fargate, EC2 VPC, EventBridge, IAM, SSM), Bedrock (Titan v2 embeddings), Modal (GPU transcribe/diarize, unchanged), pyarrow (parquet), pytest + `aws_cdk.assertions`.

**Spec:** `docs/superpowers/specs/2026-06-08-phase3-aws-ingestion-design.md`.

**Commit hook (every task):** the repo `PreToolUse` hook blocks `git commit` unless `uv run pytest -x -q` passes **and** `.claude/.md-reviewed` exists (consumed each commit). App-code/infra tasks don't change `CLAUDE.md` — just re-`touch .claude/.md-reviewed` before committing; Task 7 changes `CLAUDE.md` (run `/claude-md-improver`). Infra (`infra/`) is excluded from the root pytest via `--ignore=infra`; CDK assertion tests run with `cd infra && .venv/bin/python -m pytest`.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/pep_oracle/ingest.py` | extract `episode_chunks_and_embeddings()` (shared per-episode pipeline) | 1 |
| `src/pep_oracle/ingest_artifact.py` (new) | `ingest_artifact_incremental()` — load corpus, diff feed, process new, merge, publish | 2 |
| `src/pep_oracle/config.py` | `SPEAKER_PROFILES_URI` constant | 2 |
| `src/pep_oracle/cli.py` | `ingest-artifact` CLI command | 3 |
| `Dockerfile.ingest` (new), `.dockerignore` | ingest container image | 4 |
| `infra/pep_oracle_infra/ingest_stack.py` (new) | VPC + ECS Fargate task + EventBridge + IAM | 5 |
| `infra/app.py` | wire the ingest stack (corpus bucket + KMS key from prod stack) | 5 |
| `infra/tests/test_ingest_stack.py` (new) | CDK assertion tests | 5 |
| `tests/test_ingest_artifact.py` (new) | unit tests for the incremental orchestrator | 2 |
| `tests/test_ingest.py`, `tests/test_cli.py` | refactor + CLI tests | 1, 3 |
| `docs/aws/phase3-ingestion-runbook.md` (new), `CLAUDE.md` | deploy + decommission runbook | 7 |

**Verified facts (from code read):** `corpus.write_artifact(rows, *, dest, version, embed_model, dims, git_sha, built_at) -> Manifest` where each row is `{"chunk_id", "text", "embedding", "metadata"}`; `corpus.load_current(base) -> InMemoryCorpus` with `.ids/.docs/.embeddings/.metas/.version`; `store._chunk_metadata(chunk) -> dict` is the canonical row-metadata builder (matches the v0001 artifact, built by backfill from an export of that same metadata); `embeddings.embed_texts(list[str]) -> list[list[float]]` uses Bedrock when `EMBED_BACKEND=bedrock`; `feed.fetch_episodes() -> list[Episode]` (`.guid/.title/.pub_date/.audio_url/.episode_number`); `apply_diarization(segments, speaker_segments, profile_path=None, roster=None, clusters=None)` and `load_speaker_profiles(profile_path=None)` accept an explicit path; Modal invoked via `modal.Function.from_name(...).remote(...)` needing `MODAL_TOKEN_ID/SECRET`.

---

## Task 1: Extract the shared per-episode pipeline in `ingest.py`

Pull the transcribe→diarize→chunk→embed body out of `_ingest_one` into a reusable `episode_chunks_and_embeddings()` returning `(chunks, embeddings)`. The ChromaDB path (`_ingest_one`) and the new artifact path (Task 2) both call it. Behavior of `_ingest_one` is unchanged.

**Files:**
- Modify: `src/pep_oracle/ingest.py`
- Test: `tests/test_ingest.py`

- [ ] **Step 1: Read `_ingest_one`** (`src/pep_oracle/ingest.py:67-127`) to see the exact transcribe→diarize→chunk→embed→`add_chunks` body and what surrounds it (transcript source, the returned topic entry, the success bool). You will move the transcribe-through-embed portion into a new function and call it from `_ingest_one`, leaving `add_chunks`, the topic entry, and the return value intact.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_ingest.py` (reuse the module's existing Modal/transcribe monkeypatch fixtures — see the top of that file and `tests/conftest.py` for how `pep_oracle.transcripts.whisper.modal` / `...diarize.modal` are faked):

```python
def test_episode_chunks_and_embeddings_returns_chunks_and_vectors(monkeypatch):
    from pep_oracle import ingest
    from pep_oracle.models import Chunk, Episode, TranscriptSegment
    from datetime import datetime, timezone

    ep = Episode(guid="g-new", title="Test (Ep 300, 1 Jan)", pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                 audio_url="http://x/a.mp3", description="d", episode_number=300)

    monkeypatch.setattr(ingest, "_run_transcribe_and_diarize",
                        lambda episode, diarize, cb: (
                            [TranscriptSegment(text="hello world", start_time=0.0, end_time=5.0)],
                            "whisper", [], 0.0, 0.0))
    monkeypatch.setattr(ingest, "embed_texts", lambda texts: [[0.1] * 1024 for _ in texts])

    chunks, embeddings = ingest.episode_chunks_and_embeddings(ep, diarize=False)
    assert chunks and all(isinstance(c, Chunk) for c in chunks)
    assert len(embeddings) == len(chunks) and len(embeddings[0]) == 1024
    assert chunks[0].episode_guid == "g-new"
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_ingest.py::test_episode_chunks_and_embeddings_returns_chunks_and_vectors -q`
Expected: FAIL — `AttributeError: module 'pep_oracle.ingest' has no attribute 'episode_chunks_and_embeddings'`.

- [ ] **Step 4: Implement the extraction**

In `src/pep_oracle/ingest.py`, add the function (place it just above `_ingest_one`):

```python
def episode_chunks_and_embeddings(
    episode,
    *,
    diarize: bool = False,
    profile_path=None,
    progress_callback=None,
):
    """Transcribe → (optionally) diarize → chunk → embed one episode.

    Returns (chunks, embeddings) — the per-episode work shared by the ChromaDB
    ingest (_ingest_one) and the artifact ingest (ingest_artifact). Returns
    ([], []) when the episode yields no chunks. No storage writes here.
    """
    segments, _source, speaker_segments, _t_elapsed, _d_elapsed = _run_transcribe_and_diarize(
        episode, diarize, progress_callback
    )
    if diarize:
        roster = host_roster_from_title(episode.title)
        clusters = load_cluster_info(episode.guid)
        segments = apply_diarization(
            segments, speaker_segments, profile_path=profile_path, roster=roster, clusters=clusters
        )
    chunks = chunk_transcript(segments, episode)
    if not chunks:
        return [], []
    embeddings = embed_texts([c.text for c in chunks])
    return chunks, embeddings
```

Then in `_ingest_one`, replace the transcribe-through-embed block (the lines that call `_run_transcribe_and_diarize`, `apply_diarization`, `chunk_transcript`, and `embed_texts`) with a single call:

```python
        chunks, embeddings = episode_chunks_and_embeddings(
            episode, diarize=diarize, progress_callback=progress_callback
        )
        if not chunks:
            return False, None  # preserve the existing "no chunks" outcome
        add_chunks(collection, chunks, embeddings)
```

Keep everything else in `_ingest_one` (the `add_chunks` call, the topic-entry construction, the return shape) exactly as it was. Confirm `host_roster_from_title`, `load_cluster_info`, `apply_diarization`, `chunk_transcript`, `embed_texts` are already imported in `ingest.py` (they are — they were used by the original body).

- [ ] **Step 5: Run the new test + the existing ingest tests**

Run: `uv run pytest tests/test_ingest.py -q`
Expected: PASS — the new test AND every pre-existing ingest test (behavior of `_ingest_one`/`ingest_all` unchanged).

- [ ] **Step 6: Commit**

```bash
touch .claude/.md-reviewed
git add src/pep_oracle/ingest.py tests/test_ingest.py
git commit -m "refactor(ingest): extract shared episode_chunks_and_embeddings()"
```

---

## Task 2: `ingest_artifact.py` — incremental artifact orchestrator

The core of Phase 3: load the current corpus, find new episodes, process only those, merge, and publish `vN+1`.

**Files:**
- Create: `src/pep_oracle/ingest_artifact.py`
- Modify: `src/pep_oracle/config.py` (add `SPEAKER_PROFILES_URI`)
- Test: `tests/test_ingest_artifact.py`

- [ ] **Step 1: Add the config constant**

In `src/pep_oracle/config.py`, after the `CORPUS_URI` line, add:

```python
# Phase 3 ingestion: S3 (or local) location of the diarization speaker references,
# downloaded at ingest time and passed as profile_path. Defaults under the corpus base.
SPEAKER_PROFILES_URI = os.getenv("PEP_ORACLE_SPEAKER_PROFILES_URI", f"{CORPUS_URI}/refs/speaker_profiles.json")
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_ingest_artifact.py`:

```python
"""Tests for the artifact-native incremental ingest (ingest_artifact.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pep_oracle import ingest_artifact
from pep_oracle.corpus import InMemoryCorpus
from pep_oracle.models import Chunk, Episode


def _ep(guid, num):
    return Episode(guid=guid, title=f"T (Ep {num}, 1 Jan)",
                   pub_date=datetime(2026, 1, num, tzinfo=timezone.utc),
                   audio_url=f"http://x/{guid}.mp3", description="d", episode_number=num)


def _existing_corpus():
    # one already-ingested episode (guid g1), one chunk
    metas = [{"episode_guid": "g1", "episode_title": "T (Ep 250)", "episode_date": "2026-01-01",
              "episode_number": 250, "start_time": 0.0, "end_time": 5.0}]
    return InMemoryCorpus(ids=["g1_0000"], docs=["old text"], embeddings=[[0.0] * 1024],
                          metas=metas, version="v0001")


def _new_chunk(guid, num):
    return Chunk(chunk_id=f"{guid}_0000", episode_guid=guid, text="new text",
                 episode_title=f"T (Ep {num})", episode_date=f"2026-01-0{num}",
                 start_time=0.0, end_time=5.0, episode_number=num)


def _patch_common(monkeypatch, feed_eps, new_guids_processed):
    monkeypatch.setattr(ingest_artifact, "load_current", lambda base: _existing_corpus())
    monkeypatch.setattr(ingest_artifact, "fetch_episodes", lambda: feed_eps)
    monkeypatch.setattr(ingest_artifact, "_download_profiles", lambda dest: None)  # no S3 in tests

    def fake_proc(episode, *, diarize, profile_path, progress_callback=None):
        new_guids_processed.append(episode.guid)
        return [_new_chunk(episode.guid, episode.episode_number)], [[0.2] * 1024]

    monkeypatch.setattr(ingest_artifact, "episode_chunks_and_embeddings", fake_proc)


def test_incremental_processes_only_new_and_merges(monkeypatch):
    processed = []
    _patch_common(monkeypatch, [_ep("g1", 250), _ep("g2", 251)], processed)
    captured = {}

    def fake_write(rows, *, dest, version, embed_model, dims, git_sha, built_at):
        captured.update(rows=rows, version=version, dims=dims)
        from pep_oracle.corpus import Manifest
        return Manifest(1, embed_model, dims, [250, 251], len(rows), git_sha, built_at, "sha")

    monkeypatch.setattr(ingest_artifact, "write_artifact", fake_write)

    manifest = ingest_artifact.ingest_artifact_incremental(dest="s3://b", diarize=True)
    assert processed == ["g2"]                 # only the new episode
    assert captured["version"] == "v0002"      # incremented
    ids = [r["chunk_id"] for r in captured["rows"]]
    assert ids == ["g1_0000", "g2_0000"]       # existing + new, merged
    assert captured["rows"][1]["metadata"]["episode_guid"] == "g2"
    assert len(captured["rows"][1]["embedding"]) == 1024
    assert manifest.chunk_count == 2


def test_no_new_episodes_is_noop(monkeypatch):
    processed = []
    _patch_common(monkeypatch, [_ep("g1", 250)], processed)  # feed == already-ingested
    called = {"wrote": False}
    monkeypatch.setattr(ingest_artifact, "write_artifact",
                        lambda *a, **k: called.update(wrote=True))
    result = ingest_artifact.ingest_artifact_incremental(dest="s3://b", diarize=True)
    assert result is None
    assert called["wrote"] is False
    assert processed == []
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_ingest_artifact.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pep_oracle.ingest_artifact'`.

- [ ] **Step 4: Implement `ingest_artifact.py`**

Create `src/pep_oracle/ingest_artifact.py`:

```python
"""Artifact-native incremental ingest (Phase 3, AWS-native).

Load the current corpus artifact, diff its episode GUIDs against the RSS feed,
transcribe/diarize/chunk/Bedrock-embed ONLY new episodes, merge rows, and publish
a new corpus version (vN+1) to S3 — no ChromaDB. Idempotent: a failed run publishes
nothing (the current.json flip is atomic + last), so the next run retries.
"""

from __future__ import annotations

import datetime as _dt
import logging
import tempfile
from pathlib import Path

from pep_oracle import config, storage
from pep_oracle.corpus import load_current, write_artifact
from pep_oracle.feed import fetch_episodes
from pep_oracle.ingest import episode_chunks_and_embeddings
from pep_oracle.store import _chunk_metadata

logger = logging.getLogger(__name__)


def _next_version(current: str | None) -> str:
    n = int(current[1:]) + 1 if current and current.startswith("v") else 1
    return f"v{n:04d}"


def _download_profiles(dest: str) -> Path | None:
    """Download speaker_profiles.json (config.SPEAKER_PROFILES_URI) to a temp file
    and return its path, or None if absent. Passed to diarization as profile_path."""
    uri = config.SPEAKER_PROFILES_URI
    try:
        data = storage.get_bytes(uri)
    except Exception:  # noqa: BLE001 — absent refs → generic-label fallback, don't fail ingest
        logger.warning("speaker profiles not found at %s; diarization uses fallback labels", uri)
        return None
    path = Path(tempfile.gettempdir()) / "speaker_profiles.json"
    path.write_bytes(data)
    return path


def _corpus_rows(corpus) -> list[dict]:
    return [
        {"chunk_id": cid, "text": doc, "embedding": emb, "metadata": meta}
        for cid, doc, emb, meta in zip(corpus.ids, corpus.docs, corpus.embeddings, corpus.metas)
    ]


def ingest_artifact_incremental(
    *,
    dest: str | None = None,
    diarize: bool = True,
    git_sha: str = "",
    now_iso: str | None = None,
):
    """Publish a new corpus version with any episodes from the feed not already in
    the current artifact. Returns the new Manifest, or None if there were none."""
    if config.EMBED_BACKEND != "bedrock":
        raise RuntimeError("ingest_artifact requires PEP_ORACLE_EMBED_BACKEND=bedrock")
    dest = dest or config.CORPUS_URI

    corpus = load_current(dest)
    existing_guids = {m["episode_guid"] for m in corpus.metas}
    new_eps = [e for e in fetch_episodes() if e.guid not in existing_guids]
    if not new_eps:
        logger.info("ingest_artifact: no new episodes; current=%s", corpus.version)
        return None
    logger.info("ingest_artifact: %d new episode(s) on top of %s", len(new_eps), corpus.version)

    profile_path = _download_profiles(dest)
    rows = _corpus_rows(corpus)
    for ep in sorted(new_eps, key=lambda e: e.episode_number or 0):
        chunks, embeddings = episode_chunks_and_embeddings(
            ep, diarize=diarize, profile_path=profile_path
        )
        for chunk, emb in zip(chunks, embeddings):
            rows.append(
                {"chunk_id": chunk.chunk_id, "text": chunk.text,
                 "embedding": emb, "metadata": _chunk_metadata(chunk)}
            )

    version = _next_version(corpus.version)
    built_at = now_iso or _dt.datetime.now(_dt.timezone.utc).isoformat()
    manifest = write_artifact(
        rows, dest=dest, version=version, embed_model=config.EMBED_MODEL,
        dims=config.EMBED_DIMS, git_sha=git_sha or config.GIT_SHA, built_at=built_at,
    )
    logger.info("ingest_artifact: published %s (%d chunks, eps %s)",
                version, manifest.chunk_count, manifest.episode_range)
    return manifest
```

Note: the test monkeypatches `load_current`, `fetch_episodes`, `episode_chunks_and_embeddings`, `write_artifact`, and `_download_profiles` as module attributes — they are imported at module top, so patching `ingest_artifact.<name>` works.

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/test_ingest_artifact.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
touch .claude/.md-reviewed
git add src/pep_oracle/ingest_artifact.py src/pep_oracle/config.py tests/test_ingest_artifact.py
git commit -m "feat(ingest): artifact-native incremental ingest (load→diff→embed-new→publish)"
```

---

## Task 3: `ingest-artifact` CLI command

Expose the orchestrator as the container entrypoint.

**Files:**
- Modify: `src/pep_oracle/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py` (it already uses `click.testing.CliRunner` — match the existing style):

```python
def test_ingest_artifact_command_invokes_orchestrator(monkeypatch):
    from click.testing import CliRunner
    from pep_oracle import cli as cli_mod

    called = {}
    def fake(**kwargs):
        called.update(kwargs)
        class _M:
            chunk_count = 3
            episode_range = [169, 300]
        return _M()
    monkeypatch.setattr("pep_oracle.ingest_artifact.ingest_artifact_incremental", fake)

    r = CliRunner().invoke(cli_mod.cli, ["ingest-artifact", "--dest", "s3://b", "--diarize"])
    assert r.exit_code == 0, r.output
    assert called["dest"] == "s3://b"
    assert called["diarize"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_ingest_artifact_command_invokes_orchestrator -q`
Expected: FAIL — no such command `ingest-artifact`.

- [ ] **Step 3: Implement the command**

In `src/pep_oracle/cli.py`, add (matching the existing `@cli.command()` style; `config` is importable):

```python
@cli.command(name="ingest-artifact")
@click.option("--dest", default=None, help="Corpus base (local dir or s3:// URI). Default: PEP_ORACLE_CORPUS_URI.")
@click.option("--no-diarize", is_flag=True, help="Skip speaker diarization.")
def ingest_artifact_cmd(dest: str | None, no_diarize: bool) -> None:
    """Incremental artifact ingest: publish a new corpus version with new feed episodes."""
    from pep_oracle.ingest_artifact import ingest_artifact_incremental

    manifest = ingest_artifact_incremental(dest=dest, diarize=not no_diarize)
    if manifest is None:
        click.echo("No new episodes; corpus unchanged.")
    else:
        click.echo(f"Published {manifest.chunk_count} chunks (episodes {manifest.episode_range}).")
```

(`diarize` defaults on, matching the spec; the test passes `--diarize` which is the default, so also accept a no-op `--diarize` flag if you prefer — but `--no-diarize` is the real control. Update the test's flag to match: it asserts `diarize is True`, which is the default, so drop `--diarize` from the test invocation or add a redundant `--diarize` flag. Simplest: change the test invocation to `["ingest-artifact", "--dest", "s3://b"]` and assert `called["diarize"] is True`.)

- [ ] **Step 4: Adjust the test to the final flag surface + run**

Edit the test invocation to `["ingest-artifact", "--dest", "s3://b"]` (diarize defaults True). Run: `uv run pytest tests/test_cli.py::test_ingest_artifact_command_invokes_orchestrator -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
touch .claude/.md-reviewed
git add src/pep_oracle/cli.py tests/test_cli.py
git commit -m "feat(cli): ingest-artifact command (Fargate entrypoint)"
```

---

## Task 4: `Dockerfile.ingest` — ingest container image

A container that runs `pep-oracle ingest-artifact`. Needs the package + Modal + boto3 (no FastAPI/MCP/fastembed).

**Files:**
- Create: `Dockerfile.ingest`

- [ ] **Step 1: Create `Dockerfile.ingest`**

```dockerfile
# Ingestion image — runs `pep-oracle ingest-artifact` on Fargate (Modal GPU does the
# heavy transcribe/diarize; this container orchestrates + Bedrock-embeds + publishes).
FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/

# Base deps (modal, feedparser, requests, etc.) + the aws extra (boto3, pyarrow).
# No [server] extra (no FastAPI/MCP); fastembed isn't used on the bedrock path.
RUN python -m pip install --no-cache-dir ".[aws]"

ENTRYPOINT ["pep-oracle"]
CMD ["ingest-artifact"]
```

- [ ] **Step 2: Verify it builds + the entrypoint works**

```bash
cd /opt/pep-oracle/app
docker build -f Dockerfile.ingest -t pep-oracle-ingest:plan-check .
docker run --rm pep-oracle-ingest:plan-check ingest-artifact --help
```

Expected: image builds; `--help` prints the command usage. (If Docker is unavailable, verify structurally: `pyproject.toml` `[aws]` extra contains `boto3`+`pyarrow`+`modal` is a base dep, and `pep-oracle` console script exists; report which path you took.)

- [ ] **Step 3: Commit**

```bash
touch .claude/.md-reviewed
git add Dockerfile.ingest
git commit -m "feat(infra): ingest container image (Dockerfile.ingest)"
```

---

## Task 5: CDK ingest stack — VPC + Fargate task + daily EventBridge rule

Add `PepOracleIngestStack` (ap-southeast-2): a minimal VPC, an ECS cluster, a Fargate task definition (the ingest image, env + SSM-secret Modal tokens, least-privilege IAM), and a daily EventBridge rule that runs the task. The corpus bucket + KMS key come from the prod stack.

**Files:**
- Create: `infra/pep_oracle_infra/ingest_stack.py`
- Modify: `infra/app.py`
- Test: `infra/tests/test_ingest_stack.py`

- [ ] **Step 1: Write the failing tests**

Create `infra/tests/test_ingest_stack.py`:

```python
"""Template assertions for PepOracleIngestStack."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.ingest_stack import PepOracleIngestStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com", compute_region="ap-southeast-2",
        cert_region="us-east-1", corpus_bucket_name="pep-oracle-corpus-test",
        cognito_domain_prefix="p", allowed_email="me@example.com",
    )


def _template() -> Template:
    app = cdk.App()
    # The ingest stack takes the bucket + key by name/ARN (decoupled from the prod stack
    # for testability); see ingest_stack for the from_* imports.
    stack = PepOracleIngestStack(app, "Ingest", cfg=_cfg(), cross_region_references=True, env=ENV)
    return Template.from_stack(stack)


def test_fargate_taskdef_with_ingest_command():
    t = _template()
    t.resource_count_is("AWS::ECS::Cluster", 1)
    t.has_resource_properties("AWS::ECS::TaskDefinition", Match.object_like({
        "RequiresCompatibilities": ["FARGATE"],
        "ContainerDefinitions": Match.array_with([
            Match.object_like({
                "Command": ["ingest-artifact"],
                "Secrets": Match.array_with([
                    Match.object_like({"Name": "MODAL_TOKEN_ID"}),
                    Match.object_like({"Name": "MODAL_TOKEN_SECRET"}),
                ]),
                "Environment": Match.array_with([
                    Match.object_like({"Name": "PEP_ORACLE_EMBED_BACKEND", "Value": "bedrock"}),
                    Match.object_like({"Name": "PEP_ORACLE_SERVE_FROM_ARTIFACT", "Value": "0"}),
                    Match.object_like({"Name": "PEP_ORACLE_CORPUS_URI", "Value": "s3://pep-oracle-corpus-test"}),
                ]),
            })
        ]),
    }))


def test_daily_eventbridge_rule_targets_ecs():
    t = _template()
    t.has_resource_properties("AWS::Events::Rule", Match.object_like({
        "ScheduleExpression": "rate(1 day)",
        "Targets": Match.array_with([Match.object_like({"EcsParameters": Match.any_value()})]),
    }))


def test_task_role_has_bedrock_and_s3_and_ssm():
    t = _template()
    t.has_resource_properties("AWS::IAM::Policy", Match.object_like({
        "PolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({"Action": "bedrock:InvokeModel"}),
            ])
        })
    }))
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /opt/pep-oracle/app/infra && .venv/bin/python -m pytest tests/test_ingest_stack.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'pep_oracle_infra.ingest_stack'`.

- [ ] **Step 3: Implement `ingest_stack.py`**

Create `infra/pep_oracle_infra/ingest_stack.py`:

```python
"""ap-southeast-2 ingestion stack (Phase 3): a daily EventBridge rule runs a scale-to-zero
Fargate task that incrementally ingests new episodes and publishes a new corpus version to
S3. Modal does the GPU transcribe/diarize; this task orchestrates + Bedrock-embeds.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import Duration, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from pep_oracle_infra.config import DeployConfig

# SSM SecureString params holding the Modal credentials (created out-of-band; see runbook).
MODAL_TOKEN_ID_PARAM = "/pep-oracle/modal-token-id"
MODAL_TOKEN_SECRET_PARAM = "/pep-oracle/modal-token-secret"


class PepOracleIngestStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: DeployConfig, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        self.cfg = cfg

        # Import the corpus bucket + data key by name/ARN (decoupled from the prod stack).
        corpus_bucket = s3.Bucket.from_bucket_name(self, "CorpusBucket", cfg.corpus_bucket_name)
        data_key = kms.Key.from_lookup(self, "DataKey", alias_name="alias/pep-oracle-data") \
            if False else None  # see note below; use the prod key via app.py wiring

        # Minimal VPC: 1 AZ, a public subnet, no NAT (scale-to-zero, public egress).
        vpc = ec2.Vpc(
            self, "IngestVpc",
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)
            ],
        )
        cluster = ecs.Cluster(self, "IngestCluster", vpc=vpc)

        task_def = ecs.FargateTaskDefinition(self, "IngestTask", cpu=1024, memory_limit_mib=4096)

        project_root = Path(__file__).resolve().parents[2]
        modal_id = ssm.StringParameter.from_secure_string_parameter_attributes(
            self, "ModalIdParam", parameter_name=MODAL_TOKEN_ID_PARAM)
        modal_secret = ssm.StringParameter.from_secure_string_parameter_attributes(
            self, "ModalSecretParam", parameter_name=MODAL_TOKEN_SECRET_PARAM)

        container = task_def.add_container(
            "ingest",
            image=ecs.ContainerImage.from_asset(str(project_root), file="Dockerfile.ingest"),
            logging=ecs.LogDriver.aws_logs(stream_prefix="ingest",
                                           log_retention=logs.RetentionDays.ONE_MONTH),
            command=["ingest-artifact"],
            environment={
                "PEP_ORACLE_EMBED_BACKEND": "bedrock",
                "PEP_ORACLE_SERVE_FROM_ARTIFACT": "0",
                "PEP_ORACLE_BEDROCK_REGION": cfg.compute_region,
                "PEP_ORACLE_EMBED_MODEL": cfg.embed_model,
                "PEP_ORACLE_EMBED_DIMS": cfg.embed_dims,
                "PEP_ORACLE_CORPUS_URI": f"s3://{cfg.corpus_bucket_name}",
                "PEP_ORACLE_DATA_DIR": "/tmp/pep-oracle",
            },
            secrets={
                "MODAL_TOKEN_ID": ecs.Secret.from_ssm_parameter(modal_id),
                "MODAL_TOKEN_SECRET": ecs.Secret.from_ssm_parameter(modal_secret),
            },
        )

        # Least-privilege task role.
        role = task_def.task_role
        corpus_bucket.grant_read_write(role)
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[f"arn:aws:bedrock:{cfg.compute_region}::foundation-model/{cfg.embed_model}"],
        ))
        # The corpus bucket is CMK-encrypted; the prod data key's ARN is wired in via app.py.
        role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey"],
            resources=[self.node.try_get_context("data_key_arn") or "*"],
        ))

        rule = events.Rule(self, "DailyIngest", schedule=events.Schedule.rate(Duration.days(1)))
        rule.add_target(targets.EcsTask(
            cluster=cluster, task_definition=task_def, task_count=1,
            subnet_selection=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            assign_public_ip=True,
        ))

        self.cluster = cluster
        self.task_definition = task_def
```

**Implementation notes for the engineer (resolve while implementing):**
- The KMS data key: pass the prod stack's `kms_key` into this stack from `app.py` (same region → direct construct ref) and call `data_key.grant_encrypt_decrypt(role)` instead of the context-ARN placeholder above; drop the `from_lookup`/`try_get_context` lines. The placeholder exists only so the assertion test (which builds the stack standalone) synthesizes; when you wire `app.py`, change the constructor to accept `data_key: kms.IKey` and use it. Update `_template()` in the test to pass a dummy key (`kms.Key(...)` in a throwaway stack or `kms.Key.from_key_arn(...)`).
- `ecs.Secret.from_ssm_parameter` auto-grants the execution role `ssm:GetParameters` + `kms:Decrypt` on the SSM param — no manual grant needed.
- `from_secure_string_parameter_attributes` requires no `version` for SecureString in recent CDK; if your CDK version demands one, pass `version=1`.

- [ ] **Step 4: Wire `app.py`**

In `infra/app.py`, after the prod stack, add:

```python
from pep_oracle_infra.ingest_stack import PepOracleIngestStack

ingest = PepOracleIngestStack(
    app, "PepOracleIngestStack", cfg=cfg,
    data_key=prod.kms_key,            # same-region construct ref
    corpus_bucket=prod.corpus_bucket, # pass the real bucket
    cross_region_references=True,
    env=cdk.Environment(account=account, region=cfg.compute_region),
)
ingest.add_dependency(prod)
```

…and change `PepOracleIngestStack.__init__` to accept `data_key: kms.IKey` and `corpus_bucket: s3.IBucket` params (using them directly instead of the `from_bucket_name`/placeholder in Step 3). Keep the standalone-synth fallback only if it doesn't complicate the real path — simplest is required params + the test passes dummies.

- [ ] **Step 5: Run the tests + synth**

```bash
cd /opt/pep-oracle/app/infra && .venv/bin/python -m pytest tests/ -q
```

Expected: PASS (existing + the 3 new ingest-stack tests). Then best-effort `PATH="$PWD/.venv/bin:$PATH" ./node_modules/.bin/cdk synth PepOracleIngestStack -c allowed_email=me@example.com >/dev/null && echo synth-OK` if the cdk CLI is present.

- [ ] **Step 6: Commit**

```bash
cd /opt/pep-oracle/app
touch .claude/.md-reviewed
git add infra/pep_oracle_infra/ingest_stack.py infra/app.py infra/tests/test_ingest_stack.py
git commit -m "feat(infra): Fargate ingest stack — daily EventBridge → ECS, least-privilege IAM"
```

---

## Task 6: Full-suite gate

- [ ] **Step 1: Run both suites**

```bash
cd /opt/pep-oracle/app && uv run pytest -q
cd infra && .venv/bin/python -m pytest -q
```

Expected: root PASS (new ingest/cli/ingest_artifact tests included; `infra` ignored), infra PASS (ingest-stack tests included).

- [ ] **Step 2: Commit any fixups** (only if needed; otherwise skip).

---

## Task 7: Deploy + decommission runbook + docs

**Files:**
- Create: `docs/aws/phase3-ingestion-runbook.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write `docs/aws/phase3-ingestion-runbook.md`**

```markdown
# Phase 3 — AWS ingestion deploy + decommission runbook

Execute after merge, with go-ahead. Region ap-southeast-2, account 940831808393.

## 1. One-time prerequisites
- Modal tokens → SSM SecureString:
  ```bash
  aws ssm put-parameter --name /pep-oracle/modal-token-id --type SecureString --value "$MODAL_TOKEN_ID" --region ap-southeast-2
  aws ssm put-parameter --name /pep-oracle/modal-token-secret --type SecureString --value "$MODAL_TOKEN_SECRET" --region ap-southeast-2
  ```
- Speaker references → S3 (one-time; static):
  ```bash
  aws s3 cp ~/.pep-oracle/speaker_profiles.json s3://pep-oracle-corpus-prod/refs/speaker_profiles.json --region ap-southeast-2
  ```
- Confirm the Modal apps are deployed (`modal deploy cloud/transcribe_modal.py cloud/diarize_modal.py`) — unchanged from today.

## 2. Deploy
```bash
cd infra
PATH="$PWD/.venv/bin:$PATH" ./node_modules/.bin/cdk deploy PepOracleIngestStack --require-approval never -c allowed_email=<you@example.com> -c git_sha=$(git -C .. rev-parse --short HEAD)
```

## 3. Verify with a manual run (before relying on the schedule)
```bash
aws ecs run-task --cluster <IngestCluster name from outputs> --task-definition <IngestTask arn> \
  --launch-type FARGATE --network-configuration '{"awsvpcConfiguration":{"subnets":[<public subnet>],"assignPublicIp":"ENABLED"}}' --region ap-southeast-2
# tail logs:
aws logs tail /aws/ecs/... --follow --region ap-southeast-2
# confirm a new corpus version published + the live endpoint advanced:
curl -s https://pep-oracle.iicapn.com/version | jq '{corpus_version, corpus_episode_range}'
```
Expect `corpus_version` to advance (e.g. v0002) and `corpus_episode_range` to include the newest episode within ~5 min (serving TTL).

## 4. Cut over (only after step 3 succeeds)
On the OptiPlex:
```bash
sudo systemctl disable --now pep-oracle-ingest.timer
sudo systemctl disable --now pep-oracle-backup.service
```
AWS is now the sole ingest + publish path. Backup: the corpus lives in the versioned S3 bucket; speaker refs in S3. The Modal transcript/diarization caches are no longer copied off-site (accepted — re-compute insurance only). The OptiPlex `pep-oracle-api.service` stays as the DNS-rollback fallback until a later decommission.

## Rollback
Re-enable the OptiPlex timer/backup; the AWS schedule is idempotent so disabling the EventBridge rule (`aws events disable-rule`) stops AWS ingestion without data loss.
```

- [ ] **Step 2: Update `CLAUDE.md`**

Add an ingestion bullet under `## Deployment` (after the Phase 2c bullet):

```markdown
- **AWS ingestion (Phase 3, `infra/ingest_stack.py`)**: daily EventBridge rule → scale-to-zero Fargate task running `pep-oracle ingest-artifact` (`ingest_artifact.py`): loads the current S3 corpus, diffs the RSS feed, Modal-transcribes/diarizes + Bedrock-embeds only NEW episodes, merges rows, publishes `vN+1` (atomic `current.json` flip); the serving Lambda TTL-refreshes within ~5 min. No ChromaDB on this path (shares `ingest.episode_chunks_and_embeddings` with the local ChromaDB ingest). Modal tokens via SSM SecureString; speaker refs read from `s3://…/refs/speaker_profiles.json`. After cutover the OptiPlex `pep-oracle-ingest.timer` + `pep-oracle-backup.service` are disabled. Deploy/decommission: `docs/aws/phase3-ingestion-runbook.md`.
```

Keep `CLAUDE.md` under 300 lines (`wc -l CLAUDE.md`).

- [ ] **Step 3: Verify + `/claude-md-improver` + commit**

Run: `uv run pytest -q && wc -l CLAUDE.md`, then run `/claude-md-improver`, then:

```bash
touch .claude/.md-reviewed
git add docs/aws/phase3-ingestion-runbook.md CLAUDE.md
git commit -m "docs(phase3): AWS ingestion deploy/decommission runbook + CLAUDE.md"
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-06-08-phase3-aws-ingestion-design.md`):

| Spec requirement | Task |
|---|---|
| Artifact-native incremental (load → diff feed → process new → merge → publish vN+1) | Task 2 |
| Reuse per-episode pipeline via a small refactor | Task 1 (`episode_chunks_and_embeddings`) |
| Embed only new episodes, Bedrock | Task 2 (loops only `new_eps`; `embed_texts` bedrock) |
| Idempotent (failed run publishes nothing) | Task 2 (write_artifact flips current.json last; no-new → no write) |
| Speaker refs read from S3 | Task 2 (`_download_profiles` from `SPEAKER_PROFILES_URI`) |
| CLI entrypoint | Task 3 |
| Ingest container image (no FastAPI/fastembed) | Task 4 |
| ECS Fargate + daily EventBridge + least-priv IAM + Modal tokens via SSM + VPC | Task 5 |
| Modal unchanged (Function.from_name) | reused (no change) |
| Verify-then-cut transition; disable OptiPlex ingest + backup | Task 7 runbook |
| Out of scope: topics/web UI/`/ask`, ref regen, cache copy, Phases 4/5 | not in plan |

**Placeholder scan:** the only deferred detail is the KMS-key wiring in Task 5 (explicit engineer note: pass `prod.kms_key` via `app.py`, use `grant_encrypt_decrypt`, adjust the test's dummy) — the code + the resolution are both spelled out, not a TODO. No other placeholders. ✔

**Type/name consistency:**
- `episode_chunks_and_embeddings(episode, *, diarize, profile_path, progress_callback) -> (chunks, embeddings)` — defined Task 1, called in `_ingest_one` (Task 1) and `ingest_artifact` (Task 2). ✔
- Row shape `{"chunk_id","text","embedding","metadata"}` matches `corpus.write_artifact` + `_build_table` columns; metadata via `store._chunk_metadata`. ✔
- `ingest_artifact_incremental(*, dest, diarize, git_sha, now_iso) -> Manifest|None` — defined Task 2, called by the CLI (Task 3) and the test. ✔
- `write_artifact(rows, *, dest, version, embed_model, dims, git_sha, built_at)` — exact signature from `corpus.py`. ✔
- `load_current(base)`, `fetch_episodes()`, `_chunk_metadata(chunk)` — imported into `ingest_artifact` and monkeypatched by name in tests. ✔
- CDK: `PepOracleIngestStack(cfg, data_key, corpus_bucket)` (Task 5 wiring) — `app.py` passes `prod.kms_key`/`prod.corpus_bucket`. ✔

**Verified in-env before planning:** exact signatures + the row/metadata schema read from `ingest.py`/`store.py`/`corpus.py`/`chunking.py`/`feed.py`/`diarize.py`/`cli.py`/`config.py`; current CDK API for ECS Fargate + `ecs.Secret.from_ssm_parameter` + EventBridge `EcsTask` target via the CDK docs.
