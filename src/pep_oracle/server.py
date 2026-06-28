import logging
import os
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from urllib.parse import urlparse

from fastapi import FastAPI

from pep_oracle import authorize_gate, oauth
from pep_oracle import config as _config
from pep_oracle import corpus as _corpus
from pep_oracle.config import SERVER_HOST, SERVER_PORT

# force=True: the Lambda Python runtime pre-installs a root handler, which makes a
# plain basicConfig a silent no-op — the root level stays WARNING and every INFO
# line (incl. the pep_oracle.timing instrumentation) is dropped from CloudWatch.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s", force=True)
logger = logging.getLogger(__name__)


app = FastAPI(title="pep-oracle")


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
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
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
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="pep-oracle-mcp"'),
                ],
            }
        )
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
            logger.error(
                "AUTHORIZE_GATE=cognito but misconfigured (%s) — refusing to mount /mcp.", e
            )
            return False
    elif gate_name == "trusted_upstream":
        if os.environ.get("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "") != "1":
            logger.error(
                "PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH != '1' — refusing to mount /mcp. "
                "/oauth/authorize has no app-layer auth and MUST sit behind an upstream "
                "authenticator (e.g. Cloudflare Access on /oauth/authorize), or set "
                "PEP_ORACLE_AUTHORIZE_GATE=cognito for the in-app identity check. "
                "Set the var to '1' once that upstream guard is in place."
            )
            return False
        gate = authorize_gate.get_gate()  # TrustedUpstreamGate
    else:
        logger.error("unknown PEP_ORACLE_AUTHORIZE_GATE=%r — refusing to mount /mcp.", gate_name)
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
        assert ts is not None
        if parsed.hostname not in ts.allowed_hosts:
            ts.allowed_hosts = [*ts.allowed_hosts, parsed.hostname]
        public_origin = f"{parsed.scheme}://{parsed.hostname}"
        if public_origin not in ts.allowed_origins:
            ts.allowed_origins = [*ts.allowed_origins, public_origin]
    assert mcp.settings.transport_security is not None
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


@app.get("/health")
async def health():
    return {"status": "ok"}


def _code_version() -> tuple[str, str]:
    sha = _config.GIT_SHA.strip()
    if not sha:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except Exception:  # noqa: BLE001 — version info only; never fail the endpoint
            sha = "unknown"
    semver = _config.SEMVER.strip()
    if not semver:
        try:
            semver = _pkg_version("pep-oracle")
        except PackageNotFoundError:
            semver = "unknown"
    return semver, sha


@app.get("/version")
async def api_version():
    semver, sha = _code_version()
    import typing
    out: dict[str, typing.Any] = {"code_semver": semver, "code_git_sha": sha}
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
        logger.warning("corpus manifest unavailable for /version: %s", exc)
        out["corpus_error"] = "corpus manifest unavailable"
    return out


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
