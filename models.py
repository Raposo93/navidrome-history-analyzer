from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class HistorySource:
    table: str
    media_col: str
    user_col: str | None
    time_col: str
    item_type_col: str | None = None


@dataclass
class Track:
    id: str
    title: str
    artist: str
    album: str
    album_id: str
    album_artist: str
    genre: str
    year: int | None
    duration: float
    track_number: int
    disc_number: int
    path: str
    mbid: str


@dataclass
class Play:
    at: datetime
    track_id: str
    user_id: str
    user_name: str
    track: Track


@dataclass(frozen=True)
class AnnotationPlay:
    play_count: int
    last_played: datetime | None
