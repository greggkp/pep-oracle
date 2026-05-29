import asyncio
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pep_oracle import oauth
from pep_oracle.cache import CacheEntry, get_freshness, trigger_refresh
from pep_oracle.config import CHROMA_DIR, SERVER_HOST, SERVER_PORT, TOPICS_PATH
from pep_oracle.feed import fetch_episodes
from pep_oracle.query import ask as do_ask
from pep_oracle.store import (
    get_client,
    get_collection,
    get_fresh_collection,
    get_ingested_guids,
    get_ingestion_stats,
)
from pep_oracle.topics import bootstrap_topics, load_topics

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

_caches = {
    "status": CacheEntry(name="status", ttl_seconds=300),
    "episodes": CacheEntry(name="episodes", ttl_seconds=300),
    "topics": CacheEntry(name="topics", ttl_seconds=900),
}

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
    episode_input: str = ""
    diarize: bool = True


def parse_episode_input(s: str) -> list[int]:
    """Parse a string like '150-200, 210, 215' into a sorted list of episode numbers.

    Raises ValueError on invalid tokens (non-numeric, backwards ranges).
    """
    s = s.strip()
    if not s:
        return []
    nums: set[int] = set()
    for token in s.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("-"):
            raise ValueError(f"Invalid episode number: {token}")
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start = int(parts[0].strip())
                end = int(parts[1].strip())
            except ValueError:
                raise ValueError(f"Invalid range: {token}")
            if start > end:
                raise ValueError(f"Invalid range: {token}")
            if end - start > 1000:
                raise ValueError(f"Range too large: {token} (max 1000)")
            nums.update(range(start, end + 1))
        else:
            try:
                n = int(token)
            except ValueError:
                raise ValueError(f"Invalid episode number: {token}")
            if n < 0:
                raise ValueError(f"Invalid episode number: {token}")
            nums.add(n)
    return sorted(nums)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("pep-oracle server starting on %s:%s", SERVER_HOST, SERVER_PORT)
    # Eager cache refresh so data is ready when the browser loads
    asyncio.create_task(trigger_refresh(_caches["status"], _fetch_status))
    asyncio.create_task(trigger_refresh(_caches["episodes"], _fetch_episodes))
    # Topics refresh is deferred until a user hits /topics
    yield


app = FastAPI(title="pep-oracle", lifespan=lifespan)


class _BearerAuthASGIWrapper:
    """ASGI middleware gating an inner app on a JWT bearer token.

    401 on missing/malformed Authorization or any
    :func:`oauth.verify_access_token` failure (sig/iss/aud/exp).
    """

    def __init__(self, inner_app, signing_key: str, issuer: str):
        self._inner = inner_app
        self._signing_key = signing_key
        self._issuer = issuer

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        scheme, _, rest = headers.get("authorization", "").partition(" ")
        token = rest if scheme.lower() == "bearer" and rest else None
        if token is None:
            await self._reject(send)
            return
        try:
            oauth.verify_access_token(self._signing_key, token, self._issuer)
        except oauth.InvalidToken:
            await self._reject(send)
            return
        await self._inner(scope, receive, send)

    @staticmethod
    async def _reject(send) -> None:
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="pep-oracle-mcp"'),
            ],
        })
        await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})


def _resolve_signing_key() -> str:
    """Env ``PEP_ORACLE_OAUTH_SIGNING_KEY`` → ``$DATA_DIR/oauth_signing_key``
    → newly generated key written to that path with 0600 perms."""
    env_key = os.environ.get("PEP_ORACLE_OAUTH_SIGNING_KEY", "").strip()
    if env_key:
        return env_key
    data_dir = Path(os.environ.get("PEP_ORACLE_DATA_DIR") or (Path.home() / ".pep-oracle")).expanduser()
    key_path = data_dir / "oauth_signing_key"
    if key_path.exists():
        existing = key_path.read_text().strip()
        if existing:
            return existing
    data_dir.mkdir(parents=True, exist_ok=True)
    new_key = secrets.token_urlsafe(32)
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, new_key.encode("ascii"))
    finally:
        os.close(fd)
    logger.info("Generated new OAuth signing key at %s (mode 0600)", key_path)
    return new_key


def mount_mcp_if_configured(app: FastAPI) -> bool:
    """Mount /mcp + register OAuth routes. Requires PEP_ORACLE_PUBLIC_URL and
    PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH=1 (deployment-safety guard: confirms
    /oauth/authorize sits behind an upstream authenticator). Signing key
    comes from :func:`_resolve_signing_key`. Returns True iff mounted."""
    public_url = os.environ.get("PEP_ORACLE_PUBLIC_URL", "").strip()
    if not public_url:
        logger.warning(
            "PEP_ORACLE_PUBLIC_URL not set — MCP endpoint disabled. Set to the "
            "public tunnel hostname claude.ai will fetch (e.g. https://pep-oracle.iicapn.com)."
        )
        return False

    if os.environ.get("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "") != "1":
        logger.error(
            "PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH != '1' — refusing to mount /mcp. "
            "/oauth/authorize has no app-layer auth and MUST sit behind an upstream "
            "authenticator (e.g. Cloudflare Access on /oauth/authorize). See the "
            "Cloudflare Access setup section in /home/gregg/.claude/plans/mcp-oauth-dcr.md. "
            "Set the var to '1' once that upstream guard is in place."
        )
        return False

    signing_key = _resolve_signing_key()
    data_dir = Path(os.environ.get("PEP_ORACLE_DATA_DIR") or (Path.home() / ".pep-oracle")).expanduser()
    oauth.register_oauth_routes(app, signing_key, public_url, str(data_dir / "oauth.db"))
    logger.info("OAuth provider routes registered")

    from pep_oracle.mcp_server import mcp

    # Remap SDK's /mcp → / so mount at /mcp gives final URL /mcp (not /mcp/mcp).
    mcp.settings.streamable_http_path = "/"
    # SDK's TransportSecurity defaults reject non-localhost Host headers (DNS
    # rebinding defense). Extend allowed_hosts/allowed_origins with the public
    # hostname; the JWT bearer check is the real auth, and CF Access fronts
    # the only browser-driven OAuth path. Keep localhost defaults for dev.
    parsed = urlparse(public_url)
    if parsed.hostname:
        ts = mcp.settings.transport_security
        if parsed.hostname not in ts.allowed_hosts:
            ts.allowed_hosts = [*ts.allowed_hosts, parsed.hostname]
        public_origin = f"{parsed.scheme}://{parsed.hostname}"
        if public_origin not in ts.allowed_origins:
            ts.allowed_origins = [*ts.allowed_origins, public_origin]
    mcp_asgi = mcp.streamable_http_app()
    session_manager = mcp.session_manager

    # StreamableHTTPSessionManager must be entered as an async context for the
    # app's lifetime or its task group never starts ("Task group is not
    # initialized."). Chain into the FastAPI router lifespan.
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _combined_lifespan(app_: FastAPI):
        async with session_manager.run():
            async with previous_lifespan(app_):
                yield

    app.router.lifespan_context = _combined_lifespan
    app.mount("/mcp", _BearerAuthASGIWrapper(mcp_asgi, signing_key, public_url.rstrip("/")))
    logger.info("MCP mounted at /mcp")
    return True


def _get_fresh_collection():
    """Thin alias for store.get_fresh_collection (kept for local call sites)."""
    return get_fresh_collection()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/freshness")
async def api_freshness():
    return get_freshness(_caches)


@app.post("/ask")
async def api_ask(req: AskRequest):
    answer = await asyncio.to_thread(do_ask, req.question, top_k=req.top_k, history=req.history)
    return {"answer": answer}


def _fetch_status():
    """Fetch fresh status data (called by cache refresh)."""
    collection = _get_fresh_collection()
    ingested = get_ingested_guids(collection)
    chunk_count = collection.count()
    db_size = sum(f.stat().st_size for f in CHROMA_DIR.rglob("*") if f.is_file())
    stats = get_ingestion_stats(collection)
    try:
        all_episodes = fetch_episodes()
        feed_count = len(all_episodes)
    except Exception:
        feed_count = None
    return {
        "feed_count": feed_count,
        "ingested_count": len(ingested),
        "chunk_count": chunk_count,
        "db_size_bytes": db_size,
        **stats,
    }


@app.get("/status")
async def api_status():
    cache = _caches["status"]
    if cache.is_stale():
        asyncio.create_task(trigger_refresh(cache, _fetch_status))
    data = cache.data or {}
    return {**data, "stale": cache.is_stale() or cache.refreshing}


def _fetch_episodes():
    """Fetch fresh episodes data (called by cache refresh)."""
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


@app.get("/episodes")
async def api_episodes():
    cache = _caches["episodes"]
    if cache.is_stale():
        asyncio.create_task(trigger_refresh(cache, _fetch_episodes))
    data = cache.data or []
    return {"episodes": data, "stale": cache.is_stale() or cache.refreshing}


def _fetch_topics():
    """Fetch topics data from disk (called by cache refresh)."""
    episodes = fetch_episodes()
    # Bootstrap topics.json from feed if it doesn't exist
    if not TOPICS_PATH.exists():
        bootstrap_topics(episodes, TOPICS_PATH)
    topic_episodes = load_topics(TOPICS_PATH)
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
    return {"episodes": topic_episodes, "not_ingested_episodes": not_ingested}


@app.get("/topics")
async def api_topics():
    cache = _caches["topics"]
    if cache.is_stale():
        asyncio.create_task(trigger_refresh(cache, _fetch_topics))
    data = cache.data or {"episodes": [], "not_ingested_episodes": []}
    return {**data, "stale": cache.is_stale() or cache.refreshing}


@app.post("/ingest")
async def api_ingest(req: IngestRequest):
    global _ingest_running, _ingest_last_result, _ingest_progress

    if _ingest_running:
        return {"status": "already_running"}

    # Parse episode_input and merge with episode_numbers
    try:
        parsed = parse_episode_input(req.episode_input)
    except ValueError as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(e)})
    merged = sorted(set(req.episode_numbers + parsed))

    def _apply_progress(step: str) -> None:
        # Episode-level messages look like "[1/3] Ep 255: TITLE..."
        if step.startswith("["):
            parts = step.split("] ", 1)
            counts = parts[0].lstrip("[")
            try:
                done, total = counts.split("/")
                _ingest_progress["episodes_done"] = int(done) - 1
                _ingest_progress["episodes_total"] = int(total)
            except ValueError:
                pass
            _ingest_progress["current_episode"] = parts[1] if len(parts) > 1 else ""
            _ingest_progress["step"] = "starting"
        else:
            _ingest_progress["step"] = step

    async def _run():
        global _ingest_running, _ingest_last_result, _ingest_progress
        try:
            # Isolate ingestion in a subprocess so a crash there can't
            # take the API down with it.
            cmd = [sys.executable, "-m", "pep_oracle.ingest_worker"]
            if req.force:
                cmd.append("--force")
            if req.diarize:
                cmd.append("--diarize")
            for n in merged:
                cmd.extend(["--episode", str(n)])

            # MALLOC_ARENA_MAX=2 caps glibc's per-thread malloc arenas —
            # chromadb + numpy allocate from many threads and the default
            # (8 × cores) wastes hundreds of MB to fragmentation.
            env = {**os.environ, "MALLOC_ARENA_MAX": "2"}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )

            result: dict | None = None
            error: str | None = None
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line.startswith("PROGRESS: "):
                    _apply_progress(line[len("PROGRESS: "):])
                elif line.startswith("RESULT: "):
                    try:
                        result = json.loads(line[len("RESULT: "):])
                    except json.JSONDecodeError:
                        logger.warning("could not decode ingest result: %s", line)
                elif line.startswith("ERROR: "):
                    error = line[len("ERROR: "):]
                    logger.error("ingest worker error: %s", error)
                elif line:
                    logger.info("ingest: %s", line)

            code = await proc.wait()
            if result is not None:
                _ingest_last_result = result
            elif error is not None:
                _ingest_last_result = {"error": error}
            else:
                _ingest_last_result = {"error": f"ingest worker exited with code {code}"}

            # Invalidate all caches so frontend picks up new data
            for cache in _caches.values():
                cache.invalidate()
            asyncio.create_task(trigger_refresh(_caches["status"], _fetch_status))
            asyncio.create_task(trigger_refresh(_caches["episodes"], _fetch_episodes))
            asyncio.create_task(trigger_refresh(_caches["topics"], _fetch_topics))
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


mount_mcp_if_configured(app)


def main():
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
