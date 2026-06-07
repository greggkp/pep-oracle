import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SERVER_HOST = os.getenv("PEP_ORACLE_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PEP_ORACLE_PORT", "8000"))

RSS_FEED_URL = "https://feeds.libsyn.com/249335/rss"
APPLE_PODCAST_ID = "1499646320"

DATA_DIR = Path(os.getenv("PEP_ORACLE_DATA_DIR", Path.home() / ".pep-oracle"))
CACHE_DIR = DATA_DIR / "cache"
TRANSCRIPT_CACHE_DIR = CACHE_DIR / "transcripts"
DIARIZATION_CACHE_DIR = CACHE_DIR / "diarization"
CHROMA_DIR = DATA_DIR / "chroma"
TOPICS_PATH = DATA_DIR / "topics.json"
SPEAKER_PROFILES_PATH = DATA_DIR / "speaker_profiles.json"

CHROMA_COLLECTION = "pep_oracle"
QUERY_MODEL = "claude-sonnet-4-20250514"

# --- Embedding backend (fastembed local | AWS Bedrock) ---
# Default stays "fastembed" so existing local ingestion/CLI/tests are unchanged;
# the AWS migration opts in with PEP_ORACLE_EMBED_BACKEND=bedrock.
EMBED_BACKEND = os.getenv("PEP_ORACLE_EMBED_BACKEND", "fastembed")
# Sydney — operator default; Bedrock Titan v2 isn't in ap-southeast-4 (Melbourne).
BEDROCK_REGION = os.getenv("PEP_ORACLE_BEDROCK_REGION", "ap-southeast-2")
# EMBED_MODEL / EMBED_DIMS apply when EMBED_BACKEND=bedrock (the fastembed model
# name lives in embeddings.MODEL_NAME).
EMBED_MODEL = os.getenv("PEP_ORACLE_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
EMBED_DIMS = int(os.getenv("PEP_ORACLE_EMBED_DIMS", "1024"))

# --- Corpus artifact base location (local dir or s3:// base URI) ---
# The artifact lives under <CORPUS_URI>/corpus/{vNNNN.parquet,vNNNN.manifest.json,current.json};
# the "/corpus" prefix is appended by corpus.py, so this is the BASE, not the corpus dir itself.
CORPUS_URI = os.getenv("PEP_ORACLE_CORPUS_URI", str(DATA_DIR))

# --- Serving source (Phase 2a) ---
# When "1", the MCP tool retrieves from the corpus artifact at CORPUS_URI via an
# in-memory InMemoryCorpus (the Lambda path); otherwise it uses the live ChromaDB
# collection (the OptiPlex default — nothing rebuilds the artifact on ingest until
# Phase 3). Serving from the artifact REQUIRES EMBED_BACKEND=bedrock with a model
# matching the artifact's manifest (validated at load).
SERVE_FROM_ARTIFACT = os.getenv("PEP_ORACLE_SERVE_FROM_ARTIFACT", "0") == "1"
# How often a warm process re-checks current.json for a new corpus version (a cheap
# small-object GET). New episodes reach a warm container within this window.
CORPUS_REFRESH_TTL_SECONDS = int(os.getenv("PEP_ORACLE_CORPUS_REFRESH_TTL_SECONDS", "300"))
# Baked into the image at build time (Phase 2c); reported by GET /version.
GIT_SHA = os.getenv("PEP_ORACLE_GIT_SHA", "")


# --- OAuth store backend (Phase 2b) ---
# "sqlite" (local default, file/:memory:) or "dynamodb" (cloud). The serving
# Lambda sets "dynamodb"; the OptiPlex keeps "sqlite".
OAUTH_STORE = os.getenv("PEP_ORACLE_OAUTH_STORE", "sqlite")
OAUTH_DDB_TABLE = os.getenv("PEP_ORACLE_OAUTH_DDB_TABLE", "pep-oracle-oauth")
OAUTH_DDB_REGION = os.getenv("PEP_ORACLE_OAUTH_DDB_REGION", BEDROCK_REGION)

# --- OAuth signing-key backend (Phase 2b2) ---
# "local" (default): env PEP_ORACLE_OAUTH_SIGNING_KEY -> $DATA_DIR/oauth_signing_key
# -> a freshly generated 0600 key (unchanged OptiPlex/dev behavior). "ssm": an
# HS256 SecureString from SSM Parameter Store (the Lambda path).
OAUTH_SIGNING_BACKEND = os.getenv("PEP_ORACLE_OAUTH_SIGNING_BACKEND", "local")
OAUTH_SIGNING_SSM_PARAM = os.getenv(
    "PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM", "/pep-oracle/oauth-signing-key"
)
OAUTH_SIGNING_SSM_REGION = os.getenv("PEP_ORACLE_OAUTH_SIGNING_SSM_REGION", BEDROCK_REGION)

# --- /oauth/authorize identity gate (Phase 2b2) ---
# "trusted_upstream" (default): auto-approve, relying on an upstream authenticator
# (Cloudflare Access) -- the OptiPlex model. "cognito": in-app identity check against
# a one-user Cognito user pool (the AWS model; no external-edge dependency).
AUTHORIZE_GATE = os.getenv("PEP_ORACLE_AUTHORIZE_GATE", "trusted_upstream")
# Hosted-UI base, e.g. https://pep-oracle.auth.ap-southeast-2.amazoncognito.com
COGNITO_DOMAIN = os.getenv("PEP_ORACLE_COGNITO_DOMAIN", "")
COGNITO_CLIENT_ID = os.getenv("PEP_ORACLE_COGNITO_CLIENT_ID", "")
COGNITO_CLIENT_SECRET = os.getenv("PEP_ORACLE_COGNITO_CLIENT_SECRET", "")
COGNITO_USER_POOL_ID = os.getenv("PEP_ORACLE_COGNITO_USER_POOL_ID", "")  # e.g. ap-southeast-2_abc123
COGNITO_REGION = os.getenv("PEP_ORACLE_COGNITO_REGION", BEDROCK_REGION)
COGNITO_ALLOWED_EMAILS = os.getenv("PEP_ORACLE_COGNITO_ALLOWED_EMAILS", "")  # comma-separated


def ensure_dirs() -> None:
    for d in (TRANSCRIPT_CACHE_DIR, DIARIZATION_CACHE_DIR, CHROMA_DIR):
        d.mkdir(parents=True, exist_ok=True)
