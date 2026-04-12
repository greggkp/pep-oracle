from dataclasses import dataclass
from datetime import datetime


@dataclass
class Episode:
    guid: str
    title: str
    pub_date: datetime
    audio_url: str
    description: str
    duration_seconds: int | None = None
    episode_number: int | None = None
    transcript_source: str | None = None


@dataclass
class TranscriptSegment:
    text: str
    start_time: float | None = None
    end_time: float | None = None
    speaker: str | None = None


@dataclass
class Chunk:
    chunk_id: str
    episode_guid: str
    text: str
    episode_title: str
    episode_date: str
    start_time: float | None = None
    end_time: float | None = None
    episode_number: int | None = None
    speaker_text: str | None = None
    speaker_turns: list[dict] | None = None
