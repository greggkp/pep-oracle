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
AUDIO_CACHE_DIR = CACHE_DIR / "audio"
TRANSCRIPT_CACHE_DIR = CACHE_DIR / "transcripts"
DIARIZATION_CACHE_DIR = CACHE_DIR / "diarization"
CHROMA_DIR = DATA_DIR / "chroma"
SPEAKER_PROFILES_PATH = DATA_DIR / "speaker_profiles.json"

EMBEDDING_MODEL = "text-embedding-3-small"
CHROMA_COLLECTION = "pep_oracle"
QUERY_MODEL = "claude-sonnet-4-20250514"


def ensure_dirs() -> None:
    for d in (AUDIO_CACHE_DIR, TRANSCRIPT_CACHE_DIR, DIARIZATION_CACHE_DIR, CHROMA_DIR):
        d.mkdir(parents=True, exist_ok=True)
