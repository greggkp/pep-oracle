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


def ensure_dirs() -> None:
    for d in (TRANSCRIPT_CACHE_DIR, DIARIZATION_CACHE_DIR, CHROMA_DIR):
        d.mkdir(parents=True, exist_ok=True)
