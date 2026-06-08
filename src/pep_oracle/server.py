import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pep_oracle import authorize_gate, config as _config, corpus as _corpus, oauth
from pep_oracle.cache import CacheEntry, get_freshness, trigger_refresh
from pep_oracle.config import CHROMA_DIR, SERVER_HOST, SERVER_PORT, TOPICS_PATH
from pep_oracle.feed import fetch_episodes
from pep_oracle.query import ask as do_ask
from pep_oracle.store import (
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
    """Resolve the OAuth HS256 signing key via the pluggable backend.

    Kept as a module-level seam so ``mount_mcp_if_configured`` and tests can patch it.
    """
    from pep_oracle import signing

    return signing.resolve_signing_key()


def mount_mcp_if_configured(app: FastAPI) -> bool:
    """Mount /mcp + register OAuth routes. Requires PEP_ORACLE_PUBLIC_URL.
    Gate is selected from config.AUTHORIZE_GATE: ``cognito`` uses the in-app
    Cognito identity check (no upstream flag needed); ``trusted_upstream`` requires
    PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH=1. Unknown gate values fail closed.
    Signing key comes from :func:`_resolve_signing_key`. Returns True iff mounted."""
    public_url = os.environ.get("PEP_ORACLE_PUBLIC_URL", "").strip()
    if not public_url:
        logger.warning(
            "PEP_ORACLE_PUBLIC_URL not set — MCP endpoint disabled. Set to the "
            "public tunnel hostname claude.ai will fetch (e.g. https://pep-oracle.iicapn.com)."
        )
        return False

    gate_name = _config.AUTHORIZE_GATE
    if gate_name == "cognito":
        # The in-app Cognito identity check IS the authorize-endpoint auth, so the
        # upstream-trust flag isn't required here. Refuse if misconfigured (fail-closed).
        try:
            gate = authorize_gate.get_gate()
        except ValueError as e:
            logger.error("AUTHORIZE_GATE=cognito but misconfigured (%s) — refusing to mount /mcp.", e)
            return False
    elif gate_name == "trusted_upstream":
        if os.environ.get("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "") != "1":
            logger.error(
                "PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH != '1' — refusing to mount /mcp. "
                "/oauth/authorize has no app-layer auth and MUST sit behind an upstream "
                "authenticator (e.g. Cloudflare Access on /oauth/authorize), or set "
                "PEP_ORACLE_AUTHORIZE_GATE=cognito for the in-app identity check. See the "
                "Cloudflare Access setup section in /home/gregg/.claude/plans/mcp-oauth-dcr.md. "
                "Set the var to '1' once that upstream guard is in place."
            )
            return False
        gate = authorize_gate.get_gate()  # TrustedUpstreamGate
    else:
        logger.error(
            "unknown PEP_ORACLE_AUTHORIZE_GATE=%r — refusing to mount /mcp.", gate_name
        )
        return False

    signing_key = _resolve_signing_key()
    from pep_oracle import oauth_store

    store = oauth_store.get_store()
    oauth.register_oauth_routes(app, signing_key, public_url, store, gate)
    logger.info("OAuth provider routes registered")

    from pep_oracle.mcp_server import mcp

    # Remap SDK's /mcp → / so mount at /mcp gives final URL /mcp (not /mcp/mcp).
    mcp.settings.streamable_http_path = "/"
    # SDK's TransportSecurity defaults reject non-localhost Host headers (a DNS-rebinding
    # defense for browser-facing localhost servers). Behind CloudFront→API Gateway the
    # Lambda sees the APIGW execute-api Host, not the public hostname, so that check 421s
    # every /mcp call. DNS rebinding is a browser threat and irrelevant here — /mcp is a
    # server-to-server JSON API gated by the JWT bearer (the real auth) — so disable the
    # host/origin check. Still extend allowed_hosts/origins with the public hostname for
    # the uvicorn/OptiPlex path where the check stays meaningful.
    parsed = urlparse(public_url)
    if parsed.hostname:
        ts = mcp.settings.transport_security
        if parsed.hostname not in ts.allowed_hosts:
            ts.allowed_hosts = [*ts.allowed_hosts, parsed.hostname]
        public_origin = f"{parsed.scheme}://{parsed.hostname}"
        if public_origin not in ts.allowed_origins:
            ts.allowed_origins = [*ts.allowed_origins, public_origin]
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    # Build the streamable app once to create the session-manager template (it captures
    # the MCP server app, the stateless flag, and the transport-security settings).
    mcp.streamable_http_app()
    _sm_template = mcp.session_manager

    # Per-request fresh StreamableHTTPSessionManager. The SDK's run() is once-per-instance
    # and tears its task group down on exit; driving a long-lived run() from the FastAPI
    # lifespan works under uvicorn but BREAKS under Mangum, which runs the ASGI lifespan
    # per invocation — warm invocation #2 re-calls run() on the singleton → RuntimeError →
    # LifespanFailure → every route 500s. Stateless requests are self-contained (fresh
    # transport, no cross-request state), so a fresh manager per request is correct under
    # both runtimes and needs no lifespan wiring. (Requires stateless_http=True, which
    # mcp_server sets.)
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    async def _mcp_stateless_asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        sm = StreamableHTTPSessionManager(
            app=_sm_template.app,
            event_store=_sm_template.event_store,
            json_response=_sm_template.json_response,
            stateless=_sm_template.stateless,
            security_settings=_sm_template.security_settings,
            retry_interval=_sm_template.retry_interval,
        )
        async with sm.run():
            await sm.handle_request(scope, receive, send)

    app.mount(
        "/mcp",
        _BearerAuthASGIWrapper(_mcp_stateless_asgi, signing_key, public_url.rstrip("/")),
    )
    logger.info("MCP mounted at /mcp (per-request stateless session manager)")
    return True


def _get_fresh_collection():
    """Thin alias for store.get_fresh_collection (kept for local call sites)."""
    return get_fresh_collection()


def _serving_collection():
    """Status/episodes retrieval source, mirroring the MCP serving seam: the artifact
    InMemoryCorpus when SERVE_FROM_ARTIFACT=1 (the Lambda path — no ChromaDB, no disk
    writes / ``ensure_dirs`` under a read-only HOME), else the live ChromaDB collection
    (the OptiPlex default). Both satisfy ``.get(include=[...])`` + ``.count()``."""
    if _config.SERVE_FROM_ARTIFACT:
        return _corpus.current_corpus(
            _config.CORPUS_URI, ttl_seconds=_config.CORPUS_REFRESH_TTL_SECONDS
        )
    return _get_fresh_collection()


@app.get("/health")
async def health():
    return {"status": "ok"}


def _code_version() -> tuple[str, str]:
    sha = _config.GIT_SHA.strip()
    if not sha:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except Exception:  # noqa: BLE001 — version info only; never fail the endpoint
            sha = "unknown"
    try:
        semver = _pkg_version("pep-oracle")
    except PackageNotFoundError:
        semver = "0.0.0"
    return semver, sha


@app.get("/version")
async def api_version():
    semver, sha = _code_version()
    out = {"code_semver": semver, "code_git_sha": sha}
    if _config.SERVE_FROM_ARTIFACT:
        try:
            version, manifest = _corpus.load_manifest(_config.CORPUS_URI)
            out.update(
                corpus_version=version,
                corpus_episode_range=manifest.episode_range,
                corpus_built_at=manifest.built_at,
                embed_model=manifest.embed_model,
                corpus_dims=manifest.dims,
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't 500 the version probe
            # /version is a PUBLIC route (not behind the /mcp bearer gate), so log
            # the detail (corpus path, S3 bucket, traceback) server-side and return
            # a generic marker — never leak internals to an unauthenticated caller.
            logger.warning("corpus manifest unavailable for /version: %s", exc)
            out["corpus_error"] = "corpus manifest unavailable"
    return out


@app.get("/freshness")
async def api_freshness():
    return get_freshness(_caches)


@app.post("/ask")
async def api_ask(req: AskRequest):
    answer = await asyncio.to_thread(do_ask, req.question, top_k=req.top_k, history=req.history)
    return {"answer": answer}


def _fetch_status():
    """Fetch fresh status data (called by cache refresh)."""
    collection = _serving_collection()
    ingested = get_ingested_guids(collection)
    chunk_count = collection.count()
    db_size = (
        0
        if _config.SERVE_FROM_ARTIFACT
        else sum(f.stat().st_size for f in CHROMA_DIR.rglob("*") if f.is_file())
    )
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
        collection = _serving_collection()
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


class _McpSlashNormalizer:
    """Rewrite a request to exactly ``/mcp`` into ``/mcp/`` in the ASGI scope so the
    mounted MCP app serves it directly instead of issuing a 307 redirect. Behind
    CloudFront→API Gateway the Lambda sees the APIGW execute-api Host, so Starlette would
    build that 307's Location against the internal host — a cross-host redirect that leaks
    the origin and makes clients drop the Authorization header. Rewriting in-process
    avoids the redirect. (uvicorn/OptiPlex doesn't use this wrapper; its same-host 307 is
    harmless.)"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


def _make_lambda_handler():
    """Wrap the ASGI app with Mangum for AWS Lambda. Returns None if mangum isn't
    installed (e.g. a base local install), so importing server stays cheap."""
    try:
        from mangum import Mangum
    except ImportError:
        return None
    return Mangum(_McpSlashNormalizer(app))


handler = _make_lambda_handler()


def main():
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
