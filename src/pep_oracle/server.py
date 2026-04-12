import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pep_oracle.config import CHROMA_DIR, SERVER_HOST, SERVER_PORT
from pep_oracle.feed import fetch_episodes
from pep_oracle.ingest import ingest_all
from pep_oracle.query import ask as do_ask
from pep_oracle.store import get_client, get_collection, get_ingested_guids, get_ingestion_stats
from pep_oracle.topics import extract_topics

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

_ingest_lock = asyncio.Lock()
_ingest_running = False
_ingest_last_result: dict | None = None
_ingest_progress: dict = {"current_episode": "", "episodes_done": 0, "episodes_total": 0, "step": ""}


class AskRequest(BaseModel):
    question: str
    top_k: int = 10
    history: list[dict] = []


class IngestRequest(BaseModel):
    force: bool = False
    episode_numbers: list[int] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("pep-oracle server starting on %s:%s", SERVER_HOST, SERVER_PORT)
    yield


app = FastAPI(title="pep-oracle", lifespan=lifespan)


def _get_fresh_collection():
    """Return a ChromaDB collection that reflects current on-disk state.

    ChromaDB's PersistentClient caches the system by path.  When the CLI
    ingests episodes in a separate process, the server's cached client
    doesn't see the new data.  Clearing the system cache and creating a
    new client forces a fresh read from disk.
    """
    from chromadb.api.shared_system_client import SharedSystemClient

    SharedSystemClient.clear_system_cache()
    client = get_client()
    return get_collection(client)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/ask")
async def api_ask(req: AskRequest):
    answer = await asyncio.to_thread(do_ask, req.question, top_k=req.top_k, history=req.history)
    return {"answer": answer}


@app.get("/status")
async def api_status():
    def _status():
        collection = _get_fresh_collection()
        ingested = get_ingested_guids(collection)
        chunk_count = collection.count()
        all_episodes = fetch_episodes()
        db_size = sum(f.stat().st_size for f in CHROMA_DIR.rglob("*") if f.is_file())
        stats = get_ingestion_stats(collection)
        return {
            "feed_count": len(all_episodes),
            "ingested_count": len(ingested),
            "chunk_count": chunk_count,
            "db_size_bytes": db_size,
            **stats,
        }

    return await asyncio.to_thread(_status)


@app.get("/episodes")
async def api_episodes():
    def _episodes():
        all_episodes = fetch_episodes()
        try:
            collection = _get_fresh_collection()
            ingested = get_ingested_guids(collection)
        except Exception:
            ingested = set()

        return [
            {
                "episode_number": ep.episode_number,
                "title": ep.title,
                "date": ep.pub_date.strftime("%Y-%m-%d"),
                "ingested": ep.guid in ingested,
            }
            for ep in all_episodes
        ]

    return await asyncio.to_thread(_episodes)


@app.get("/topics")
async def api_topics():
    def _topics():
        episodes = fetch_episodes()
        topics = extract_topics(episodes)
        # Feed-based detection: compare ALL feed episodes against ChromaDB
        feed_eps = {ep.episode_number for ep in episodes if ep.episode_number is not None}
        try:
            collection = _get_fresh_collection()
            ingested_eps = set()
            all_meta = collection.get(include=["metadatas"])
            for meta in all_meta["metadatas"]:
                ep_num = meta.get("episode_number", 0)
                if ep_num:
                    ingested_eps.add(ep_num)
        except Exception:
            ingested_eps = set()
        not_ingested = sorted(feed_eps - ingested_eps)
        # Only flag episodes newer than the most recently ingested
        if ingested_eps:
            latest_ingested = max(ingested_eps)
            not_ingested = [ep for ep in not_ingested if ep > latest_ingested]
        return topics, not_ingested

    topics, not_ingested = await asyncio.to_thread(_topics)
    return {"topics": topics, "not_ingested_episodes": not_ingested}


@app.post("/ingest")
async def api_ingest(req: IngestRequest):
    global _ingest_running, _ingest_last_result, _ingest_progress

    if _ingest_running:
        return {"status": "already_running"}

    async def _run():
        global _ingest_running, _ingest_last_result, _ingest_progress
        try:
            def _progress(step: str):
                global _ingest_progress
                # Episode-level messages look like "[1/3] Ep 255: TITLE..."
                if step.startswith("["):
                    parts = step.split("] ", 1)
                    counts = parts[0].lstrip("[")
                    done, total = counts.split("/")
                    _ingest_progress["episodes_done"] = int(done) - 1
                    _ingest_progress["episodes_total"] = int(total)
                    _ingest_progress["current_episode"] = parts[1] if len(parts) > 1 else ""
                    _ingest_progress["step"] = "starting"
                else:
                    _ingest_progress["step"] = step

            result = await asyncio.to_thread(
                ingest_all,
                force=req.force,
                confirm_cost=False,
                episode_numbers=req.episode_numbers or None,
                progress_callback=_progress,
            )
            _ingest_last_result = result
        except Exception as e:
            _ingest_last_result = {"error": str(e)}
            logger.exception("Ingestion failed")
        finally:
            _ingest_running = False
            _ingest_progress = {"current_episode": "", "episodes_done": 0, "episodes_total": 0, "step": ""}

    _ingest_running = True
    _ingest_progress = {"current_episode": "", "episodes_done": 0, "episodes_total": 0, "step": ""}
    asyncio.create_task(_run())
    return {"status": "started"}


@app.get("/ingest/status")
async def api_ingest_status():
    return {"running": _ingest_running, "last_result": _ingest_last_result, **_ingest_progress}


@app.api_route("/reload", methods=["GET", "POST"])
async def api_reload():
    """Clear ChromaDB's cached state so the server picks up external writes."""
    await asyncio.to_thread(_get_fresh_collection)
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse(WEB_DIR / "index.html")


def main():
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
