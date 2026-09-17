#!/usr/bin/env python3
"""
Radiografía de escucha de Navidrome — V4: sesiones temporales + hilos lógicos de álbum.

Lee navidrome.db en modo de solo lectura. Detecta automáticamente:
  1) historial temporal de scrobbles/reproducciones (incluye scrobbles.submission_time), o
  2) annotations con play_count/play_date (Navidrome antiguo).

No modifica la base de datos y no requiere paquetes externos.

Uso:
    python3 navidrome_history_report.py /ruta/navidrome.db -o navidrome_report

Opcional:
    --user gonzalo
    --top 30
    --session-gap 30
    --pair-window 20
    --resurrection-days 180
    --album-complete 0.80
    --album-thread-hours 48
    --resume-slack 2
    --inspect
"""

from __future__ import annotations

import argparse
import difflib
import unicodedata
from bisect import bisect_left
import csv
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------- Utilidades ----------

def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def norm(s: Any) -> str:
    return "" if s is None else str(s).strip()


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        n = float(value)
        if n > 10_000_000_000_000:  # microsegundos
            n /= 1_000_000
        elif n > 10_000_000_000:    # milisegundos
            n /= 1000
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc).astimezone()
        except (ValueError, OSError, OverflowError):
            return None

    s = str(value).strip()
    if not s:
        return None

    # Unix almacenado como texto
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        try:
            return parse_dt(float(s))
        except ValueError:
            pass

    # SQLite/Navidrome ISO
    candidates = [
        s,
        s.replace("Z", "+00:00"),
        s.replace(" ", "T", 1),
        s.replace(" ", "T", 1).replace("Z", "+00:00"),
    ]
    for c in candidates:
        try:
            dt = datetime.fromisoformat(c)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc).astimezone()
            return dt.astimezone()
        except ValueError:
            continue

    # Fallbacks comunes
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).astimezone()
        except ValueError:
            pass
    return None


def fmt_duration(seconds: float | int) -> str:
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days} d {hours:02d} h {minutes:02d} min"
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def pct(a: int | float, b: int | float) -> str:
    return "0,0%" if not b else f"{100 * a / b:.1f}%".replace(".", ",")


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def split_genres(value: str) -> set[str]:
    s = norm(value).lower()
    if not s:
        return set()
    return {
        x.strip()
        for x in re.split(r"[;,/|]+", s)
        if x.strip() and len(x.strip()) > 1
    }


# ---------- Inspección de esquema ----------

class Schema:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.tables = [
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        self.columns: dict[str, list[str]] = {}
        for table in self.tables:
            self.columns[table] = [
                r[1] for r in db.execute(f"PRAGMA table_info({qident(table)})")
            ]

    def has(self, table: str) -> bool:
        return table in self.columns

    def col(self, table: str, candidates: Iterable[str]) -> str | None:
        cols = {c.lower(): c for c in self.columns.get(table, [])}
        for c in candidates:
            if c.lower() in cols:
                return cols[c.lower()]
        return None

    def dump(self) -> str:
        out = []
        for t in self.tables:
            out.append(f"{t}: {', '.join(self.columns[t])}")
        return "\n".join(out)


@dataclass
class HistorySource:
    table: str
    media_col: str
    user_col: str | None
    time_col: str
    item_type_col: str | None = None


def detect_history_source(schema: Schema) -> HistorySource | None:
    media_candidates = (
        "media_file_id", "mediafile_id", "song_id", "track_id",
        "item_id", "media_id"
    )
    user_candidates = ("user_id", "userid")
    time_candidates = (
        "play_date", "played_at", "scrobbled_at", "submission_time",
        "timestamp", "time_stamp", "played_time", "created_at", "date"
    )

    scored: list[tuple[int, HistorySource]] = []

    for table in schema.tables:
        tl = table.lower()
        if tl in {"annotation", "player", "play_queue", "playlist", "playlist_tracks"}:
            continue

        media_col = schema.col(table, media_candidates)
        time_col = schema.col(table, time_candidates)
        if not media_col or not time_col:
            continue

        user_col = schema.col(table, user_candidates)
        item_type_col = schema.col(table, ("item_type", "type"))

        score = 0
        if "scrob" in tl:
            score += 12
        if "listen" in tl:
            score += 10
        if "history" in tl:
            score += 8
        if "play" in tl:
            score += 5
        if media_col.lower() in {"media_file_id", "song_id", "track_id"}:
            score += 4
        if user_col:
            score += 2
        if time_col.lower() in {"play_date", "played_at", "scrobbled_at"}:
            score += 3

        # Evita escoger tablas genéricas por accidente.
        if score >= 7:
            scored.append(
                (score, HistorySource(
                    table=table,
                    media_col=media_col,
                    user_col=user_col,
                    time_col=time_col,
                    item_type_col=item_type_col,
                ))
            )

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


# ---------- Metadatos ----------

MEDIA_TABLE = "media_file"


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


def load_tracks(db: sqlite3.Connection, schema: Schema) -> dict[str, Track]:
    if not schema.has(MEDIA_TABLE):
        raise RuntimeError("No encuentro la tabla media_file en la base de Navidrome.")

    def c(*names: str) -> str | None:
        return schema.col(MEDIA_TABLE, names)

    idc = c("id")
    if not idc:
        raise RuntimeError("media_file no tiene columna id.")

    fields = {
        "title": c("title", "name"),
        "artist": c("artist", "artist_name"),
        "album": c("album", "album_name"),
        "album_id": c("album_id"),
        "album_artist": c("album_artist", "albumartist"),
        "genre": c("genre", "genres"),
        "year": c("year", "release_year", "date"),
        "duration": c("duration", "length"),
        "track_number": c("track_number", "track"),
        "disc_number": c("disc_number", "disc"),
        "path": c("path", "file_path"),
        "mbid": c("mbz_recording_id", "mbz_track_id", "musicbrainz_id"),
    }

    select = [f"{qident(idc)} AS id"]
    for alias, col in fields.items():
        if col:
            select.append(f"{qident(col)} AS {qident(alias)}")
        else:
            select.append(f"NULL AS {qident(alias)}")

    tracks: dict[str, Track] = {}
    for r in db.execute(f"SELECT {', '.join(select)} FROM {qident(MEDIA_TABLE)}"):
        d = dict(r)
        year = None
        try:
            if d["year"] is not None:
                m = re.search(r"\d{4}", str(d["year"]))
                if m:
                    year = int(m.group())
        except (TypeError, ValueError):
            pass

        try:
            duration = float(d["duration"] or 0)
        except (TypeError, ValueError):
            duration = 0

        def as_int(v: Any, default: int = 0) -> int:
            try:
                return int(v or default)
            except (TypeError, ValueError):
                m = re.search(r"\d+", str(v or ""))
                return int(m.group()) if m else default

        tid = norm(d["id"])
        tracks[tid] = Track(
            id=tid,
            title=norm(d["title"]) or "(sin título)",
            artist=norm(d["artist"]) or "(artista desconocido)",
            album=norm(d["album"]) or "(álbum desconocido)",
            album_id=norm(d["album_id"]),
            album_artist=norm(d["album_artist"]),
            genre=norm(d["genre"]),
            year=year,
            duration=duration,
            track_number=as_int(d["track_number"]),
            disc_number=max(1, as_int(d["disc_number"], 1)),
            path=norm(d["path"]),
            mbid=norm(d["mbid"]),
        )
    return tracks


def load_users(db: sqlite3.Connection, schema: Schema) -> dict[str, str]:
    if not schema.has("user"):
        return {}
    idc = schema.col("user", ("id",))
    namec = schema.col("user", ("user_name", "username", "name", "email"))
    if not idc:
        return {}
    if namec:
        sql = f"SELECT {qident(idc)}, {qident(namec)} FROM {qident('user')}"
        return {norm(r[0]): norm(r[1]) or norm(r[0]) for r in db.execute(sql)}
    sql = f"SELECT {qident(idc)} FROM {qident('user')}"
    return {norm(r[0]): norm(r[0]) for r in db.execute(sql)}


def resolve_user_ids(users: dict[str, str], requested: str | None) -> set[str] | None:
    if not requested:
        return None
    rq = requested.casefold()
    matches = {
        uid for uid, name in users.items()
        if uid.casefold() == rq or name.casefold() == rq
    }
    if not matches:
        # Permite pasar ID aunque la tabla user no sea legible
        matches = {requested}
    return matches


# ---------- Carga de historial ----------

def load_plays(
    db: sqlite3.Connection,
    schema: Schema,
    source: HistorySource,
    tracks: dict[str, Track],
    users: dict[str, str],
    selected_users: set[str] | None,
) -> list[Play]:
    cols = [
        f"{qident(source.media_col)} AS media_id",
        f"{qident(source.time_col)} AS played_at",
    ]
    if source.user_col:
        cols.append(f"{qident(source.user_col)} AS user_id")
    else:
        cols.append("'' AS user_id")

    where: list[str] = []
    args: list[Any] = []

    # Si item_id es genérico, filtrar cuando la tabla lo permite.
    if source.item_type_col and source.media_col.lower() == "item_id":
        vals = [
            norm(r[0]) for r in db.execute(
                f"SELECT DISTINCT {qident(source.item_type_col)} "
                f"FROM {qident(source.table)} LIMIT 30"
            )
        ]
        accepted = next(
            (v for v in vals if v.lower() in {"media_file", "song", "track"}),
            None,
        )
        if accepted:
            where.append(f"{qident(source.item_type_col)} = ?")
            args.append(accepted)

    if selected_users and source.user_col:
        marks = ",".join("?" for _ in selected_users)
        where.append(f"{qident(source.user_col)} IN ({marks})")
        args.extend(sorted(selected_users))

    sql = f"SELECT {', '.join(cols)} FROM {qident(source.table)}"
    if where:
        sql += " WHERE " + " AND ".join(where)

    plays: list[Play] = []
    missing_track = 0
    bad_date = 0

    for r in db.execute(sql, args):
        tid = norm(r["media_id"])
        tr = tracks.get(tid)
        if not tr:
            missing_track += 1
            continue

        dt = parse_dt(r["played_at"])
        if not dt:
            bad_date += 1
            continue

        uid = norm(r["user_id"])
        plays.append(
            Play(
                at=dt,
                track_id=tid,
                user_id=uid,
                user_name=users.get(uid, uid or "(usuario desconocido)"),
                track=tr,
            )
        )

    plays.sort(key=lambda p: p.at)
    if missing_track:
        print(f"Aviso: {missing_track} reproducciones apuntan a canciones no presentes.", file=sys.stderr)
    if bad_date:
        print(f"Aviso: {bad_date} reproducciones tenían fecha no reconocible.", file=sys.stderr)
    return plays


# ---------- Análisis profundo ----------

def build_sessions(plays: list[Play], gap_minutes: int) -> list[dict[str, Any]]:
    if not plays:
        return []
    gap = timedelta(minutes=gap_minutes)
    sessions: list[list[Play]] = []
    cur = [plays[0]]

    for p in plays[1:]:
        prev = cur[-1]
        # No mezclar usuarios en una misma sesión.
        if p.user_id != prev.user_id or p.at - prev.at > gap:
            sessions.append(cur)
            cur = [p]
        else:
            cur.append(p)
    sessions.append(cur)

    rows = []
    for i, sess in enumerate(sessions, 1):
        start, end = sess[0].at, sess[-1].at
        listening_seconds = sum(max(0, p.track.duration) for p in sess)
        artists = Counter(p.track.artist for p in sess)
        albums = Counter(p.track.album for p in sess)
        genres = Counter(g for p in sess for g in split_genres(p.track.genre))
        rows.append({
            "session_id": i,
            "user": sess[0].user_name,
            "start": start.isoformat(sep=" "),
            "end": end.isoformat(sep=" "),
            "span_minutes": round((end - start).total_seconds() / 60, 1),
            "tracks": len(sess),
            "unique_tracks": len({p.track_id for p in sess}),
            "estimated_music_minutes": round(listening_seconds / 60, 1),
            "top_artist": artists.most_common(1)[0][0] if artists else "",
            "top_album": albums.most_common(1)[0][0] if albums else "",
            "top_genre": genres.most_common(1)[0][0] if genres else "",
        })
    return rows


def weekly_obsessions(plays: list[Play]) -> list[dict[str, Any]]:
    by_week: dict[tuple[int, int, str, str], list[Play]] = defaultdict(list)
    week_totals: Counter[tuple[int, int, str]] = Counter()

    for p in plays:
        iso = p.at.isocalendar()
        key = (iso.year, iso.week, p.user_name)
        week_totals[key] += 1
        by_week[(iso.year, iso.week, p.user_name, p.track_id)].append(p)

    rows = []
    for (year, week, user, tid), ps in by_week.items():
        n = len(ps)
        total = week_totals[(year, week, user)]
        # Umbral flexible: >=3, o >=2 si supone una parte significativa de una semana pequeña.
        if n < 3 and not (n >= 2 and total <= 12):
            continue
        tr = ps[0].track
        rows.append({
            "year": year,
            "week": week,
            "user": user,
            "plays": n,
            "week_total": total,
            "share": round(100 * n / total, 1) if total else 0,
            "artist": tr.artist,
            "title": tr.title,
            "album": tr.album,
            "track_id": tid,
        })
    rows.sort(key=lambda r: (r["year"], r["week"], r["plays"]), reverse=True)
    return rows


def resurrections(plays: list[Play], days: int) -> list[dict[str, Any]]:
    by_track: dict[tuple[str, str], list[Play]] = defaultdict(list)
    for p in plays:
        by_track[(p.user_id, p.track_id)].append(p)

    rows = []
    for (_, tid), ps in by_track.items():
        if len(ps) < 2:
            continue
        ps.sort(key=lambda p: p.at)
        for a, b in zip(ps, ps[1:]):
            gap = (b.at - a.at).total_seconds() / 86400
            if gap >= days:
                tr = b.track
                rows.append({
                    "user": b.user_name,
                    "silent_days": round(gap, 1),
                    "before": a.at.isoformat(sep=" "),
                    "return": b.at.isoformat(sep=" "),
                    "artist": tr.artist,
                    "title": tr.title,
                    "album": tr.album,
                    "track_id": tid,
                })
    rows.sort(key=lambda r: r["silent_days"], reverse=True)
    return rows


def track_pairs(plays: list[Play], window_minutes: int) -> list[dict[str, Any]]:
    win = timedelta(minutes=window_minutes)
    c: Counter[tuple[str, str, str]] = Counter()
    sample: dict[tuple[str, str, str], tuple[Track, Track, str]] = {}

    for a, b in zip(plays, plays[1:]):
        if a.user_id != b.user_id:
            continue
        if b.at - a.at > win:
            continue
        if a.track_id == b.track_id:
            continue
        key = (a.user_id, a.track_id, b.track_id)
        c[key] += 1
        sample[key] = (a.track, b.track, a.user_name)

    rows = []
    for key, n in c.items():
        if n < 2:
            continue
        ta, tb, user = sample[key]
        rows.append({
            "user": user,
            "times": n,
            "from_artist": ta.artist,
            "from_title": ta.title,
            "to_artist": tb.artist,
            "to_title": tb.title,
            "from_track_id": ta.id,
            "to_track_id": tb.id,
        })
    rows.sort(key=lambda r: r["times"], reverse=True)
    return rows


def context_jumps(plays: list[Play], window_minutes: int) -> list[dict[str, Any]]:
    """
    Heurística 0..100.
    Sube con cambio de artista, géneros sin solapamiento y salto grande de año.
    Es más fiable cuanto mejores sean los tags de género/año.
    """
    win = timedelta(minutes=window_minutes)
    rows = []

    for a, b in zip(plays, plays[1:]):
        if a.user_id != b.user_id or b.at - a.at > win:
            continue
        if a.track_id == b.track_id:
            continue

        ga, gb = split_genres(a.track.genre), split_genres(b.track.genre)
        score = 0.0
        evidence = []

        if a.track.artist.casefold() != b.track.artist.casefold():
            score += 25
            evidence.append("artista")

        if ga and gb:
            inter = len(ga & gb)
            union = len(ga | gb)
            genre_distance = 1 - inter / union if union else 0
            score += 50 * genre_distance
            evidence.append("género")
        elif ga or gb:
            score += 18
            evidence.append("género parcial")

        if a.track.year and b.track.year:
            dy = abs(a.track.year - b.track.year)
            year_score = min(25, dy / 2)
            score += year_score
            if dy >= 10:
                evidence.append(f"{dy} años")

        # Sin tags suficientes, no fingir precisión.
        confidence = "alta" if ga and gb and a.track.year and b.track.year else (
            "media" if ga and gb else "baja"
        )

        rows.append({
            "user": a.user_name,
            "at": b.at.isoformat(sep=" "),
            "score": round(min(100, score), 1),
            "confidence": confidence,
            "from_artist": a.track.artist,
            "from_title": a.track.title,
            "from_genre": a.track.genre,
            "from_year": a.track.year or "",
            "to_artist": b.track.artist,
            "to_title": b.track.title,
            "to_genre": b.track.genre,
            "to_year": b.track.year or "",
            "evidence": ", ".join(evidence),
        })

    rows.sort(key=lambda r: (r["score"], r["confidence"] == "alta"), reverse=True)
    return rows


def discoveries(plays: list[Play]) -> list[dict[str, Any]]:
    by_track: dict[tuple[str, str], list[Play]] = defaultdict(list)
    for p in plays:
        by_track[(p.user_id, p.track_id)].append(p)

    rows = []
    for (_, tid), ps in by_track.items():
        ps.sort(key=lambda p: p.at)
        first, last = ps[0], ps[-1]
        lifespan = (last.at - first.at).total_seconds() / 86400
        if len(ps) < 3:
            continue
        rows.append({
            "user": first.user_name,
            "plays": len(ps),
            "lifespan_days": round(lifespan, 1),
            "first_play": first.at.isoformat(sep=" "),
            "last_play": last.at.isoformat(sep=" "),
            "artist": first.track.artist,
            "title": first.track.title,
            "album": first.track.album,
            "track_id": tid,
        })
    rows.sort(key=lambda r: (r["lifespan_days"], r["plays"]), reverse=True)
    return rows



# ---------- V3: análisis centrado en álbumes ----------

def album_key(track: Track) -> str:
    if track.album_id:
        return track.album_id
    owner = track.album_artist or track.artist
    return f"{owner.casefold()}\x1f{track.album.casefold()}"


def album_catalog(tracks: dict[str, Track]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Track]] = defaultdict(list)
    for tr in tracks.values():
        grouped[album_key(tr)].append(tr)

    out: dict[str, dict[str, Any]] = {}
    for key, ts in grouped.items():
        ts = sorted(
            ts,
            key=lambda t: (
                t.disc_number or 1,
                t.track_number if t.track_number > 0 else 10_000,
                t.title.casefold(),
                t.id,
            ),
        )
        positions = {t.id: i + 1 for i, t in enumerate(ts)}
        first = ts[0]
        out[key] = {
            "album_key": key,
            "album_id": first.album_id,
            "artist": first.album_artist or first.artist,
            "album": first.album,
            "tracks": ts,
            "positions": positions,
            "track_count": len(ts),
            "duration": sum(t.duration for t in ts),
            "year": min((t.year for t in ts if t.year), default=None),
        }
    return out


def build_album_runs(
    plays: list[Play],
    tracks: dict[str, Track],
    gap_minutes: int,
    complete_threshold: float,
) -> list[dict[str, Any]]:
    """Reconstruye bloques contiguos del mismo álbum dentro de una sesión."""
    if not plays:
        return []
    catalog = album_catalog(tracks)
    max_gap = timedelta(minutes=gap_minutes)
    chunks: list[list[Play]] = []
    cur = [plays[0]]

    for p in plays[1:]:
        prev = cur[-1]
        same_user = p.user_id == prev.user_id
        same_album = album_key(p.track) == album_key(prev.track)
        close = p.at - prev.at <= max_gap
        restarted_album = False
        if same_album:
            cat = catalog[album_key(p.track)]
            prev_pos = cat["positions"].get(prev.track_id, 0)
            next_pos = cat["positions"].get(p.track_id, 0)
            total_pos = max(1, int(cat["track_count"]))
            restarted_album = bool(
                next_pos and next_pos <= 2 and prev_pos >= max(3, math.ceil(total_pos * 0.60))
            )
        if same_user and same_album and close and not restarted_album:
            cur.append(p)
        else:
            chunks.append(cur)
            cur = [p]
    chunks.append(cur)

    rows: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, 1):
        first = chunk[0]
        key = album_key(first.track)
        cat = catalog[key]
        total = max(1, int(cat["track_count"]))
        positions = cat["positions"]

        unique_ids: list[str] = []
        seen: set[str] = set()
        for p in chunk:
            if p.track_id not in seen:
                seen.add(p.track_id)
                unique_ids.append(p.track_id)

        pos = [positions[x] for x in unique_ids if x in positions]
        transitions = list(zip(pos, pos[1:]))
        exact_next = sum(1 for a, b in transitions if b == a + 1)
        forward = sum(1 for a, b in transitions if b > a)
        seq_score = exact_next / len(transitions) if transitions else 1.0
        forward_score = forward / len(transitions) if transitions else 1.0
        completion = len(unique_ids) / total
        first_pos = pos[0] if pos else 0
        last_pos = pos[-1] if pos else 0
        starts_at_beginning = bool(first_pos and first_pos <= 2)
        reaches_end = bool(last_pos and last_pos >= max(1, total - 1))
        complete_like = completion >= complete_threshold
        sequential = seq_score >= 0.60 or (len(pos) <= 2 and forward_score >= 0.5)
        full_pass = complete_like and starts_at_beginning and reaches_end and sequential
        meaningful = len(unique_ids) >= min(3, total) or completion >= 0.50

        if full_pass:
            status = "completo_probable"
        elif complete_like:
            status = "casi_completo"
        elif meaningful and starts_at_beginning:
            status = "parcial_desde_inicio"
        elif meaningful:
            status = "parcial"
        else:
            status = "pista_suelta"

        if len(pos) <= 2:
            listening_mode = "indeterminado"
        elif seq_score >= 0.65:
            listening_mode = "orden"
        elif forward_score < 0.50:
            listening_mode = "shuffle_o_manual"
        else:
            listening_mode = "mixto"

        stop_track = chunk[-1].track
        rows.append({
            "run_id": idx,
            "user": first.user_name,
            "start": first.at.isoformat(sep=" "),
            "end": chunk[-1].at.isoformat(sep=" "),
            "span_minutes": round((chunk[-1].at - first.at).total_seconds() / 60, 1),
            "album_key": key,
            "album_id": cat["album_id"],
            "artist": cat["artist"],
            "album": cat["album"],
            "release_year": cat["year"] or "",
            "library_tracks": total,
            "scrobbles": len(chunk),
            "unique_tracks": len(unique_ids),
            "completion_pct": round(100 * completion, 1),
            "sequential_pct": round(100 * seq_score, 1),
            "forward_pct": round(100 * forward_score, 1),
            "first_position": first_pos,
            "last_position": last_pos,
            "starts_at_beginning": int(starts_at_beginning),
            "reaches_end": int(reaches_end),
            "complete_like": int(complete_like),
            "full_pass": int(full_pass),
            "meaningful": int(meaningful),
            "status": status,
            "listening_mode": listening_mode,
            "stop_track": stop_track.title,
        })
    return rows


def aggregate_album_completion(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        if r["meaningful"]:
            grouped[(r["user"], r["album_key"])].append(r)

    rows: list[dict[str, Any]] = []
    for (_, _), rs in grouped.items():
        completions = [float(r["completion_pct"]) for r in rs]
        sequential = [float(r["sequential_pct"]) for r in rs]
        sample = rs[0]
        full = sum(int(r["full_pass"]) for r in rs)
        near = sum(int(r["complete_like"]) for r in rs)
        starts = [r for r in rs if r["starts_at_beginning"]]
        abandons = sum(
            1 for r in starts
            if not r["complete_like"] and float(r["completion_pct"]) >= 15
        )
        stop_counter = Counter(
            r["stop_track"] for r in starts if not r["complete_like"]
        )
        rows.append({
            "user": sample["user"],
            "album_key": sample["album_key"],
            "album_id": sample["album_id"],
            "artist": sample["artist"],
            "album": sample["album"],
            "release_year": sample["release_year"],
            "library_tracks": sample["library_tracks"],
            "album_runs": len(rs),
            "probable_full_plays": full,
            "near_complete_runs": near,
            "started_from_beginning": len(starts),
            "probable_abandons": abandons,
            "avg_completion_pct": round(statistics.mean(completions), 1),
            "median_completion_pct": round(statistics.median(completions), 1),
            "avg_sequential_pct": round(statistics.mean(sequential), 1),
            "favorite_stop_track": stop_counter.most_common(1)[0][0] if stop_counter else "",
            "full_play_rate_pct": round(100 * full / len(rs), 1),
        })
    rows.sort(
        key=lambda r: (r["probable_full_plays"], r["album_runs"], r["avg_completion_pct"]),
        reverse=True,
    )
    return rows


def album_resurrections(
    runs: list[dict[str, Any]], resurrection_days: int
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        if r["meaningful"]:
            grouped[(r["user"], r["album_key"])].append(r)

    rows = []
    for (_, _), rs in grouped.items():
        rs.sort(key=lambda r: r["start"])
        for a, b in zip(rs, rs[1:]):
            da = parse_dt(a["start"])
            db = parse_dt(b["start"])
            if not da or not db:
                continue
            gap = (db - da).total_seconds() / 86400
            if gap >= resurrection_days:
                rows.append({
                    "user": b["user"],
                    "silent_days": round(gap, 1),
                    "before": a["start"],
                    "return": b["start"],
                    "artist": b["artist"],
                    "album": b["album"],
                    "before_completion_pct": a["completion_pct"],
                    "return_completion_pct": b["completion_pct"],
                    "album_key": b["album_key"],
                })
    rows.sort(key=lambda r: r["silent_days"], reverse=True)
    return rows


def album_transitions(
    runs: list[dict[str, Any]], window_minutes: int
) -> list[dict[str, Any]]:
    meaningful = [r for r in runs if r["meaningful"]]
    meaningful.sort(key=lambda r: (r["user"], r["start"]))
    c: Counter[tuple[str, str, str]] = Counter()
    samples: dict[tuple[str, str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    window = timedelta(minutes=window_minutes)

    for a, b in zip(meaningful, meaningful[1:]):
        if a["user"] != b["user"] or a["album_key"] == b["album_key"]:
            continue
        end_a = parse_dt(a["end"])
        start_b = parse_dt(b["start"])
        if not end_a or not start_b or start_b - end_a > window:
            continue
        key = (a["user"], a["album_key"], b["album_key"])
        c[key] += 1
        samples[key] = (a, b)

    rows = []
    for key, n in c.items():
        a, b = samples[key]
        rows.append({
            "user": a["user"],
            "times": n,
            "from_artist": a["artist"],
            "from_album": a["album"],
            "to_artist": b["artist"],
            "to_album": b["album"],
            "from_album_key": a["album_key"],
            "to_album_key": b["album_key"],
        })
    rows.sort(key=lambda r: r["times"], reverse=True)
    return rows


def weekly_dominance(plays: list[Play], level: str) -> list[dict[str, Any]]:
    by_week: dict[tuple[str, int, int], Counter[str]] = defaultdict(Counter)
    labels: dict[str, tuple[str, str]] = {}
    totals: Counter[tuple[str, int, int]] = Counter()

    for p in plays:
        iso = p.at.isocalendar()
        wk = (p.user_name, iso.year, iso.week)
        if level == "album":
            key = album_key(p.track)
            labels[key] = (p.track.album_artist or p.track.artist, p.track.album)
        else:
            key = (p.track.album_artist or p.track.artist).casefold()
            labels[key] = (p.track.album_artist or p.track.artist, "")
        by_week[wk][key] += 1
        totals[wk] += 1

    rows = []
    for (user, year, week), counter in sorted(by_week.items()):
        key, n = counter.most_common(1)[0]
        artist, album = labels[key]
        rows.append({
            "user": user,
            "year": year,
            "week": week,
            "week_start": datetime.fromisocalendar(year, week, 1).date().isoformat(),
            "plays": n,
            "week_total": totals[(user, year, week)],
            "share_pct": round(100 * n / totals[(user, year, week)], 1),
            "artist": artist,
            "album": album,
            "entity_key": key,
        })
    return rows


def dominance_eras(weekly: list[dict[str, Any]], level: str) -> list[dict[str, Any]]:
    if not weekly:
        return []
    rows: list[dict[str, Any]] = []
    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in weekly:
        by_user[r["user"]].append(r)

    for user, wrs in by_user.items():
        wrs.sort(key=lambda r: r["week_start"])
        cur = [wrs[0]]
        for r in wrs[1:]:
            prev = cur[-1]
            prev_date = datetime.fromisoformat(prev["week_start"]).date()
            this_date = datetime.fromisoformat(r["week_start"]).date()
            consecutive = (this_date - prev_date).days == 7
            if consecutive and r["entity_key"] == prev["entity_key"]:
                cur.append(r)
            else:
                rows.append(_finish_era(cur, level))
                cur = [r]
        rows.append(_finish_era(cur, level))

    rows.sort(key=lambda r: (r["weeks"], r["dominant_plays"], r["avg_share_pct"]), reverse=True)
    return rows


def _finish_era(rs: list[dict[str, Any]], level: str) -> dict[str, Any]:
    first, last = rs[0], rs[-1]
    return {
        "user": first["user"],
        "level": level,
        "start_week": f'{first["year"]}-W{int(first["week"]):02d}',
        "end_week": f'{last["year"]}-W{int(last["week"]):02d}',
        "weeks": len(rs),
        "artist": first["artist"],
        "album": first["album"],
        "dominant_plays": sum(int(r["plays"]) for r in rs),
        "period_total": sum(int(r["week_total"]) for r in rs),
        "avg_share_pct": round(statistics.mean(float(r["share_pct"]) for r in rs), 1),
    }


def load_annotation_playcounts(
    db: sqlite3.Connection,
    schema: Schema,
    selected_users: set[str] | None,
) -> dict[tuple[str, str], int]:
    if not schema.has("annotation"):
        return {}
    t = "annotation"
    uid = schema.col(t, ("user_id",))
    item = schema.col(t, ("item_id",))
    typ = schema.col(t, ("item_type",))
    cnt = schema.col(t, ("play_count", "playcount"))
    if not uid or not item or not cnt:
        return {}

    where = [f"COALESCE({qident(cnt)},0) > 0"]
    args: list[Any] = []
    if typ:
        where.append(f"{qident(typ)} = ?")
        args.append("media_file")
    if selected_users:
        marks = ",".join("?" for _ in selected_users)
        where.append(f"{qident(uid)} IN ({marks})")
        args.extend(sorted(selected_users))
    sql = (
        f"SELECT {qident(uid)} AS user_id, {qident(item)} AS item_id, "
        f"{qident(cnt)} AS play_count FROM {qident(t)} WHERE " + " AND ".join(where)
    )
    out: dict[tuple[str, str], int] = {}
    for r in db.execute(sql, args):
        try:
            out[(norm(r["user_id"]), norm(r["item_id"]))] = int(r["play_count"] or 0)
        except (TypeError, ValueError):
            pass
    return out


def historical_vs_recent(
    plays: list[Play],
    tracks: dict[str, Track],
    annotations: dict[tuple[str, str], int],
    users: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    recent_track: Counter[tuple[str, str]] = Counter((p.user_id, p.track_id) for p in plays)
    album_acc: dict[tuple[str, str], dict[str, Any]] = {}
    artist_acc: dict[tuple[str, str], dict[str, Any]] = {}

    all_keys = set(annotations) | set(recent_track)
    for uid, tid in all_keys:
        tr = tracks.get(tid)
        if not tr:
            continue
        total = int(annotations.get((uid, tid), 0))
        recent = int(recent_track.get((uid, tid), 0))
        older = max(0, total - recent)
        mismatch = int(recent > total and total > 0)
        user = users.get(uid, uid or "(usuario desconocido)")

        ak = (uid, album_key(tr))
        aa = album_acc.setdefault(ak, {
            "user": user, "artist": tr.album_artist or tr.artist, "album": tr.album,
            "album_key": album_key(tr), "historical_total": 0, "recent_scrobbles": 0,
            "estimated_before_history": 0, "mismatch_tracks": 0,
        })
        aa["historical_total"] += total
        aa["recent_scrobbles"] += recent
        aa["estimated_before_history"] += older
        aa["mismatch_tracks"] += mismatch

        artist_name = tr.album_artist or tr.artist
        ar = artist_acc.setdefault((uid, artist_name.casefold()), {
            "user": user, "artist": artist_name, "historical_total": 0,
            "recent_scrobbles": 0, "estimated_before_history": 0, "mismatch_tracks": 0,
        })
        ar["historical_total"] += total
        ar["recent_scrobbles"] += recent
        ar["estimated_before_history"] += older
        ar["mismatch_tracks"] += mismatch

    def finalize(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for r in items:
            total = int(r["historical_total"])
            recent = int(r["recent_scrobbles"])
            older = int(r["estimated_before_history"])
            r = dict(r)
            r["recent_share_pct"] = round(100 * recent / total, 1) if total else ""
            if older >= max(10, 2 * recent):
                label = "histórica"
            elif recent >= max(10, 2 * older):
                label = "reciente"
            elif older >= 10 and recent >= 10:
                label = "persistente"
            else:
                label = "mixta"
            r["profile"] = label
            rows.append(r)
        rows.sort(key=lambda r: (r["historical_total"], r["recent_scrobbles"]), reverse=True)
        return rows

    return finalize(album_acc.values()), finalize(artist_acc.values())


def artist_depth(plays: list[Play], tracks: dict[str, Track]) -> list[dict[str, Any]]:
    lib_tracks: dict[str, set[str]] = defaultdict(set)
    lib_albums: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, str] = {}
    for t in tracks.values():
        artist = t.album_artist or t.artist
        k = artist.casefold()
        labels[k] = artist
        lib_tracks[k].add(t.id)
        lib_albums[k].add(album_key(t))

    play_counts: Counter[str] = Counter()
    heard_tracks: dict[str, set[str]] = defaultdict(set)
    heard_albums: dict[str, set[str]] = defaultdict(set)
    album_counts: dict[str, Counter[str]] = defaultdict(Counter)
    track_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for p in plays:
        artist = p.track.album_artist or p.track.artist
        k = artist.casefold()
        labels[k] = artist
        play_counts[k] += 1
        heard_tracks[k].add(p.track_id)
        heard_albums[k].add(album_key(p.track))
        album_counts[k][album_key(p.track)] += 1
        track_counts[k][p.track_id] += 1

    rows = []
    for k, plays_n in play_counts.items():
        lt = len(lib_tracks[k])
        la = len(lib_albums[k])
        ht = len(heard_tracks[k])
        ha = len(heard_albums[k])
        top_album = album_counts[k].most_common(1)[0][1] if album_counts[k] else 0
        top_track = track_counts[k].most_common(1)[0][1] if track_counts[k] else 0
        rows.append({
            "artist": labels[k],
            "plays": plays_n,
            "library_tracks": lt,
            "heard_tracks": ht,
            "track_breadth_pct": round(100 * ht / lt, 1) if lt else 0,
            "library_albums": la,
            "heard_albums": ha,
            "album_breadth_pct": round(100 * ha / la, 1) if la else 0,
            "top_album_share_pct": round(100 * top_album / plays_n, 1) if plays_n else 0,
            "top_track_share_pct": round(100 * top_track / plays_n, 1) if plays_n else 0,
        })
    rows.sort(key=lambda r: (r["plays"], r["track_breadth_pct"]), reverse=True)
    return rows


# ---------- V4: hilos lógicos de álbum y calidad de metadatos ----------

def _text_key(value: str) -> str:
    s = unicodedata.normalize("NFKD", norm(value)).encode("ascii", "ignore").decode("ascii")
    s = s.casefold()
    s = re.sub(r"\.[a-z0-9]{2,5}$", "", s)
    s = re.sub(r"^\s*\d{1,3}(?:\s*[-._)\]]+\s*|\s+)", "", s)
    s = re.sub(r"\b(?:disc|cd)\s*\d+\b", " ", s)
    s = re.sub(r"\b(?:official|audio|video|remaster(?:ed)?|version)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def path_track_number(path: str) -> int:
    stem = Path(norm(path)).stem
    m = re.match(r"^\s*(\d{1,3})(?:\s*[-._)\]]+\s*|\s+)", stem)
    return int(m.group(1)) if m else 0


def metadata_quality(track: Track) -> dict[str, Any]:
    """Heurística conservadora: sólo marca contradicciones visibles."""
    issues: list[str] = []
    path_num = path_track_number(track.path)
    if path_num and track.track_number and path_num != track.track_number:
        issues.append("track_number_vs_path")

    stem = Path(track.path).stem if track.path else ""
    title_key = _text_key(track.title)
    file_key = _text_key(stem)
    similarity: float | None = None
    if title_key and file_key and len(title_key) >= 4 and len(file_key) >= 4:
        similarity = difflib.SequenceMatcher(None, title_key, file_key).ratio()
        # Evitamos marcar nombres de fichero puramente numéricos o crípticos.
        alpha_tokens = [x for x in file_key.split() if any(c.isalpha() for c in x)]
        if alpha_tokens and similarity < 0.28:
            issues.append("title_vs_path_low_similarity")

    if len(issues) >= 2:
        confidence = "baja"
    elif issues:
        confidence = "media"
    else:
        confidence = "alta"

    return {
        "track_id": track.id,
        "artist": track.artist,
        "album": track.album,
        "disc_number": track.disc_number,
        "track_number": track.track_number,
        "path_track_number": path_num or "",
        "title": track.title,
        "path": track.path,
        "title_path_similarity": "" if similarity is None else round(similarity, 3),
        "issues": ",".join(issues),
        "confidence": confidence,
    }


def metadata_issues(tracks: dict[str, Track]) -> list[dict[str, Any]]:
    rows = [metadata_quality(t) for t in tracks.values()]
    rows = [r for r in rows if r["issues"]]
    severity = {"baja": 0, "media": 1, "alta": 2}
    rows.sort(key=lambda r: (severity.get(r["confidence"], 9), r["artist"], r["album"], r["track_number"]))
    return rows


def _lis_length(values: list[int]) -> int:
    """Longitud de subsecuencia estrictamente creciente; mide orden aproximado."""
    tails: list[int] = []
    for x in values:
        if x <= 0:
            continue
        i = bisect_left(tails, x)
        if i == len(tails):
            tails.append(x)
        else:
            tails[i] = x
    return len(tails)


def build_album_threads(
    plays: list[Play],
    tracks: dict[str, Track],
    session_gap_minutes: int,
    thread_hours: float,
    resume_slack: int,
    complete_threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Reconstruye recorridos lógicos de álbum independientemente de las sesiones.

    Una pausa no rompe el hilo si, dentro de `thread_hours`, la siguiente escucha
    del mismo álbum continúa por la pista siguiente o muy próxima. Las escuchas
    de otros álbumes intercaladas no rompen por sí solas el hilo: submission_time
    es tiempo de scrobble, no necesariamente inicio exacto de reproducción.
    """
    if not plays:
        return [], []

    catalog = album_catalog(tracks)
    global_index = {id(p): i for i, p in enumerate(plays)}
    grouped: dict[tuple[str, str], list[Play]] = defaultdict(list)
    for p in plays:
        grouped[(p.user_id, album_key(p.track))].append(p)

    thread_limit = timedelta(hours=max(1.0, thread_hours))
    session_gap = timedelta(minutes=max(1, session_gap_minutes))
    thread_chunks: list[list[Play]] = []

    for (_, key), ps in grouped.items():
        ps.sort(key=lambda x: x.at)
        cat = catalog[key]
        positions = cat["positions"]
        total = max(1, int(cat["track_count"]))
        cur = [ps[0]]

        for p in ps[1:]:
            prev = cur[-1]
            gap = p.at - prev.at
            prev_pos = positions.get(prev.track_id, 0)
            next_pos = positions.get(p.track_id, 0)
            gprev = global_index[id(prev)]
            gnext = global_index[id(p)]
            globally_adjacent = gnext == gprev + 1

            # Reinicio claro: vuelve al principio después de haber avanzado bastante.
            clear_restart = bool(
                next_pos and prev_pos
                and next_pos <= 2
                and prev_pos >= max(3, math.ceil(total * 0.55))
            )

            # Retroceso amplio también suele señalar nueva pasada.
            backward_restart = bool(
                next_pos and prev_pos and next_pos < prev_pos - max(1, resume_slack)
            )

            # Continuación lógica: siguiente pista o una muy próxima hacia delante.
            near_forward = bool(
                next_pos and prev_pos
                and next_pos >= prev_pos
                and next_pos <= prev_pos + 1 + max(0, resume_slack)
            )

            # Dentro de una sesión, si los eventos son globalmente adyacentes,
            # permitimos algo de shuffle/manual aunque el número de pista no avance.
            same_session_local = globally_adjacent and gap <= session_gap

            continuation = (
                gap <= thread_limit
                and not clear_restart
                and not backward_restart
                and (near_forward or same_session_local or next_pos == prev_pos)
            )

            if continuation:
                cur.append(p)
            else:
                thread_chunks.append(cur)
                cur = [p]
        thread_chunks.append(cur)

    # Orden temporal global para IDs estables.
    thread_chunks.sort(key=lambda ch: ch[0].at)
    threads: list[dict[str, Any]] = []
    resumes: list[dict[str, Any]] = []

    for idx, chunk in enumerate(thread_chunks, 1):
        first = chunk[0]
        key = album_key(first.track)
        cat = catalog[key]
        positions = cat["positions"]
        total = max(1, int(cat["track_count"]))

        seq_positions = [positions.get(p.track_id, 0) for p in chunk]
        unique_positions = sorted({x for x in seq_positions if x > 0})
        first_pos = seq_positions[0] if seq_positions else 0
        last_pos = seq_positions[-1] if seq_positions else 0
        starts = bool(first_pos and first_pos <= 2)
        reaches = bool(last_pos and last_pos >= max(1, total - 1))

        coverage = len(unique_positions) / total
        lis = _lis_length(seq_positions)
        ordered_coverage = lis / total

        start_dt = first.at
        p24 = [positions.get(p.track_id, 0) for p in chunk if p.at - start_dt <= timedelta(hours=24)]
        p48 = [positions.get(p.track_id, 0) for p in chunk if p.at - start_dt <= timedelta(hours=48)]
        cov24 = len({x for x in p24 if x > 0}) / total
        cov48 = len({x for x in p48 if x > 0}) / total
        ord24 = _lis_length(p24) / total
        ord48 = _lis_length(p48) / total

        gaps = [(b.at - a.at) for a, b in zip(chunk, chunk[1:])]
        resume_gaps = [g for g in gaps if g > session_gap]
        max_pause_h = max((g.total_seconds() / 3600 for g in gaps), default=0.0)

        # Confianza de metadatos de las pistas que sustentan este hilo.
        qualities = [metadata_quality(p.track)["confidence"] for p in chunk]
        confidence = "baja" if "baja" in qualities else ("media" if "media" in qualities else "alta")

        approx_order = ordered_coverage >= max(0.50, complete_threshold - 0.20)
        complete_like = coverage >= complete_threshold
        full_thread = complete_like and starts and reaches and approx_order
        meaningful = len(unique_positions) >= min(3, total) or coverage >= 0.50

        if full_thread:
            status = "completo_logico"
        elif complete_like and approx_order:
            status = "casi_completo_logico"
        elif meaningful and starts:
            status = "parcial_desde_inicio"
        elif meaningful:
            status = "parcial"
        else:
            status = "pista_suelta"

        threads.append({
            "thread_id": idx,
            "user": first.user_name,
            "start": first.at.isoformat(sep=" "),
            "end": chunk[-1].at.isoformat(sep=" "),
            "span_hours": round((chunk[-1].at - first.at).total_seconds() / 3600, 2),
            "album_key": key,
            "album_id": cat["album_id"],
            "artist": cat["artist"],
            "album": cat["album"],
            "release_year": cat["year"] or "",
            "library_tracks": total,
            "scrobbles": len(chunk),
            "unique_tracks": len(unique_positions),
            "coverage_pct": round(100 * coverage, 1),
            "ordered_coverage_pct": round(100 * ordered_coverage, 1),
            "coverage_24h_pct": round(100 * cov24, 1),
            "ordered_coverage_24h_pct": round(100 * ord24, 1),
            "coverage_48h_pct": round(100 * cov48, 1),
            "ordered_coverage_48h_pct": round(100 * ord48, 1),
            "first_position": first_pos,
            "last_position": last_pos,
            "starts_at_beginning": int(starts),
            "reaches_end": int(reaches),
            "resume_count": len(resume_gaps),
            "max_pause_hours": round(max_pause_h, 2),
            "complete_like": int(complete_like),
            "full_thread": int(full_thread),
            "meaningful": int(meaningful),
            "status": status,
            "metadata_confidence": confidence,
        })

        for a, b in zip(chunk, chunk[1:]):
            gap = b.at - a.at
            if gap <= session_gap:
                continue
            ia = global_index[id(a)]
            ib = global_index[id(b)]
            interleaved = max(0, ib - ia - 1)
            resumes.append({
                "thread_id": idx,
                "user": first.user_name,
                "artist": cat["artist"],
                "album": cat["album"],
                "pause_hours": round(gap.total_seconds() / 3600, 2),
                "from_time": a.at.isoformat(sep=" "),
                "to_time": b.at.isoformat(sep=" "),
                "from_position": positions.get(a.track_id, 0),
                "to_position": positions.get(b.track_id, 0),
                "from_title": a.track.title,
                "to_title": b.track.title,
                "interleaved_scrobbles": interleaved,
                "metadata_confidence": confidence,
            })

    resumes.sort(key=lambda r: r["pause_hours"], reverse=True)
    return threads, resumes


def aggregate_album_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in threads:
        if r["meaningful"]:
            grouped[(r["user"], r["album_key"])].append(r)

    rows: list[dict[str, Any]] = []
    for _, rs in grouped.items():
        sample = rs[0]
        full = sum(int(r["full_thread"]) for r in rs)
        resumed = sum(1 for r in rs if int(r["resume_count"]) > 0)
        starts = [r for r in rs if r["starts_at_beginning"]]
        # "Abandono" sólo tras agotar la ventana lógica; ya no depende de 30 min.
        abandons = sum(
            1 for r in starts
            if not r["complete_like"] and float(r["coverage_pct"]) >= 15
        )
        rows.append({
            "user": sample["user"],
            "album_key": sample["album_key"],
            "album_id": sample["album_id"],
            "artist": sample["artist"],
            "album": sample["album"],
            "release_year": sample["release_year"],
            "library_tracks": sample["library_tracks"],
            "logical_threads": len(rs),
            "probable_full_plays": full,
            "resumed_threads": resumed,
            "probable_abandons_after_window": abandons,
            "avg_coverage_pct": round(statistics.mean(float(r["coverage_pct"]) for r in rs), 1),
            "median_coverage_pct": round(statistics.median(float(r["coverage_pct"]) for r in rs), 1),
            "avg_ordered_coverage_pct": round(statistics.mean(float(r["ordered_coverage_pct"]) for r in rs), 1),
            "avg_coverage_24h_pct": round(statistics.mean(float(r["coverage_24h_pct"]) for r in rs), 1),
            "avg_coverage_48h_pct": round(statistics.mean(float(r["coverage_48h_pct"]) for r in rs), 1),
            "full_play_rate_pct": round(100 * full / len(rs), 1),
        })
    rows.sort(key=lambda r: (r["probable_full_plays"], r["logical_threads"], r["avg_coverage_pct"]), reverse=True)
    return rows


def album_thread_resurrections(
    threads: list[dict[str, Any]], resurrection_days: int
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in threads:
        if r["meaningful"]:
            grouped[(r["user"], r["album_key"])].append(r)
    rows: list[dict[str, Any]] = []
    for rs in grouped.values():
        rs.sort(key=lambda r: r["start"])
        for a, b in zip(rs, rs[1:]):
            da, db = parse_dt(a["end"]), parse_dt(b["start"])
            if not da or not db:
                continue
            gap = (db - da).total_seconds() / 86400
            if gap >= resurrection_days:
                rows.append({
                    "user": b["user"],
                    "silent_days": round(gap, 1),
                    "before": a["end"],
                    "return": b["start"],
                    "artist": b["artist"],
                    "album": b["album"],
                    "before_coverage_pct": a["coverage_pct"],
                    "return_coverage_pct": b["coverage_pct"],
                    "album_key": b["album_key"],
                })
    rows.sort(key=lambda r: r["silent_days"], reverse=True)
    return rows


def history_report_v4(
    plays: list[Play],
    tracks: dict[str, Track],
    top: int,
    session_gap: int,
    pair_window: int,
    resurrection_days: int,
    album_complete: float,
    album_thread_hours: float,
    resume_slack: int,
    outdir: Path,
    source: HistorySource,
    annotation_counts: dict[tuple[str, str], int],
    users: dict[str, str],
) -> str:
    # Mantiene todos los CSV y análisis de V3 por compatibilidad.
    base = history_report(
        plays=plays,
        tracks=tracks,
        top=top,
        session_gap=session_gap,
        pair_window=pair_window,
        resurrection_days=resurrection_days,
        album_complete=album_complete,
        outdir=outdir,
        source=source,
        annotation_counts=annotation_counts,
        users=users,
    )

    threads, resumes = build_album_threads(
        plays=plays,
        tracks=tracks,
        session_gap_minutes=session_gap,
        thread_hours=album_thread_hours,
        resume_slack=resume_slack,
        complete_threshold=album_complete,
    )
    completion = aggregate_album_threads(threads)
    thread_res = album_thread_resurrections(threads, resurrection_days)
    issues = metadata_issues(tracks)

    write_csv(outdir / "album_threads.csv", threads, [
        "thread_id", "user", "start", "end", "span_hours", "album_key", "album_id",
        "artist", "album", "release_year", "library_tracks", "scrobbles", "unique_tracks",
        "coverage_pct", "ordered_coverage_pct", "coverage_24h_pct",
        "ordered_coverage_24h_pct", "coverage_48h_pct", "ordered_coverage_48h_pct",
        "first_position", "last_position", "starts_at_beginning", "reaches_end",
        "resume_count", "max_pause_hours", "complete_like", "full_thread",
        "meaningful", "status", "metadata_confidence"
    ])
    write_csv(outdir / "album_thread_completion.csv", completion, [
        "user", "album_key", "album_id", "artist", "album", "release_year",
        "library_tracks", "logical_threads", "probable_full_plays", "resumed_threads",
        "probable_abandons_after_window", "avg_coverage_pct", "median_coverage_pct",
        "avg_ordered_coverage_pct", "avg_coverage_24h_pct", "avg_coverage_48h_pct",
        "full_play_rate_pct"
    ])
    write_csv(outdir / "album_resume_events.csv", resumes, [
        "thread_id", "user", "artist", "album", "pause_hours", "from_time", "to_time",
        "from_position", "to_position", "from_title", "to_title",
        "interleaved_scrobbles", "metadata_confidence"
    ])
    write_csv(outdir / "album_thread_resurrections.csv", thread_res, [
        "user", "silent_days", "before", "return", "artist", "album",
        "before_coverage_pct", "return_coverage_pct", "album_key"
    ])
    write_csv(outdir / "metadata_issues.csv", issues, [
        "track_id", "artist", "album", "disc_number", "track_number",
        "path_track_number", "title", "path", "title_path_similarity",
        "issues", "confidence"
    ])

    meaningful = [r for r in threads if r["meaningful"]]
    full = [r for r in meaningful if r["full_thread"]]
    resumed = [r for r in meaningful if r["resume_count"]]
    low_meta = [r for r in meaningful if r["metadata_confidence"] == "baja"]

    lines: list[str] = []
    add = lines.append
    add("")
    add("V4 — HILOS LÓGICOS DE ÁLBUM")
    add("=" * 72)
    add(
        f"La sesión temporal sigue usando {session_gap} min, pero NO decide si un álbum "
        f"fue abandonado."
    )
    add(
        f"Un hilo de álbum puede reanudarse durante {album_thread_hours:g} h si vuelve "
        f"por la pista siguiente o muy próxima (holgura: {resume_slack})."
    )
    add(
        "Las escuchas de otros álbumes intercaladas no rompen automáticamente el hilo, "
        "porque submission_time es el momento del scrobble y no un reloj perfecto de inicio."
    )
    add("")
    add(f"Hilos de álbum significativos: {len(meaningful):,}".replace(",", "."))
    add(
        f"Completos probables: {len(full):,} ({pct(len(full), len(meaningful))})".replace(",", ".")
    )
    add(
        f"Hilos con al menos una pausa > {session_gap} min: "
        f"{len(resumed):,} ({pct(len(resumed), len(meaningful))})".replace(",", ".")
    )
    add(f"Incidencias de metadatos detectadas en biblioteca: {len(issues):,}".replace(",", "."))
    if low_meta:
        add(f"Hilos con confianza baja de metadatos: {len(low_meta):,}".replace(",", "."))
    add("")

    add(f"TOP {top} ÁLBUMES — HILOS COMPLETOS PROBABLES")
    add("-" * 72)
    shown = 0
    for r in completion:
        if r["probable_full_plays"] <= 0:
            continue
        add(
            f'{r["probable_full_plays"]:4d} completas | {r["logical_threads"]:3d} hilos | '
            f'{r["resumed_threads"]:3d} retomados | {r["avg_coverage_pct"]:5.1f}% cobertura | '
            f'{r["artist"]} — {r["album"]}'
        )
        shown += 1
        if shown >= top:
            break
    if not shown:
        add("(ninguno con el criterio actual)")
    add("")

    add(f"PAUSAS Y REANUDACIONES DE ÁLBUM — mayores {top}")
    add("-" * 72)
    for r in resumes[:top]:
        add(
            f'{r["pause_hours"]:6.2f} h | pista {r["from_position"]} → {r["to_position"]} | '
            f'{r["artist"]} — {r["album"]}\n'
            f'          {r["from_title"]} → {r["to_title"]} '
            f'| scrobbles intercalados: {r["interleaved_scrobbles"]}'
        )
    if not resumes:
        add("(ninguna pausa superior al corte de sesión)")
    add("")

    add(f"POSIBLES ABANDONOS REALES — tras ventana de {album_thread_hours:g} h")
    add("-" * 72)
    aband = [r for r in completion if r["probable_abandons_after_window"] > 0]
    aband.sort(key=lambda r: (r["probable_abandons_after_window"], r["logical_threads"]), reverse=True)
    for r in aband[:top]:
        add(
            f'{r["probable_abandons_after_window"]:3d} posibles / {r["logical_threads"]:3d} hilos | '
            f'{r["median_coverage_pct"]:5.1f}% mediana | {r["artist"]} — {r["album"]}'
        )
    if not aband:
        add("(ninguno)")
    add("")

    add("COBERTURA 24/48 H")
    add("-" * 72)
    for r in sorted(
        completion,
        key=lambda x: (x["avg_coverage_48h_pct"], x["logical_threads"]),
        reverse=True,
    )[:top]:
        add(
            f'{r["avg_coverage_24h_pct"]:5.1f}% / {r["avg_coverage_48h_pct"]:5.1f}% '
            f'| orden {r["avg_ordered_coverage_pct"]:5.1f}% | '
            f'{r["artist"]} — {r["album"]}'
        )
    add("")

    if issues:
        add(f"METADATOS SOSPECHOSOS — primeros {min(top, len(issues))}")
        add("-" * 72)
        for r in issues[:top]:
            nums = ""
            if r["path_track_number"]:
                nums = f' track={r["track_number"]}, fichero={r["path_track_number"]};'
            add(
                f'[{r["confidence"]}] {r["artist"]} — {r["album"]} — {r["title"]}\n'
                f'          {r["issues"]};{nums} {r["path"]}'
            )
        add("")

    add(
        "Nota V4: album_runs.csv y las secciones V3 siguen siendo útiles para sesiones "
        "temporales, pero album_threads.csv / album_thread_completion.csv son la referencia "
        "para decidir continuidad o abandono de un disco."
    )

    return base + "\n".join(lines) + "\n"


# ---------- Informe con historial ----------

def history_report(
    plays: list[Play],
    tracks: dict[str, Track],
    top: int,
    session_gap: int,
    pair_window: int,
    resurrection_days: int,
    album_complete: float,
    outdir: Path,
    source: HistorySource,
    annotation_counts: dict[tuple[str, str], int],
    users: dict[str, str],
) -> str:
    if not plays:
        return "No se encontraron reproducciones para los filtros indicados.\n"

    track_counts = Counter(p.track_id for p in plays)
    artist_counts = Counter(p.track.artist for p in plays)
    album_counts = Counter((p.track.artist, p.track.album) for p in plays)
    genre_counts = Counter(g for p in plays for g in split_genres(p.track.genre))
    year_counts = Counter(p.at.year for p in plays)
    month_counts = Counter(p.at.strftime("%Y-%m") for p in plays)
    weekday_counts = Counter(p.at.strftime("%A") for p in plays)
    hour_counts = Counter(p.at.hour for p in plays)
    user_counts = Counter(p.user_name for p in plays)

    first, last = plays[0].at, plays[-1].at
    unique_tracks = len(track_counts)
    total_duration = sum(p.track.duration for p in plays)
    repeat_plays = len(plays) - unique_tracks

    sessions = build_sessions(plays, session_gap)
    weekly = weekly_obsessions(plays)
    res = resurrections(plays, resurrection_days)
    pairs = track_pairs(plays, pair_window)
    jumps = context_jumps(plays, pair_window)
    disc = discoveries(plays)

    # V3: álbumes como unidad principal
    album_runs = build_album_runs(plays, tracks, session_gap, album_complete)
    album_completion = aggregate_album_completion(album_runs)
    album_res = album_resurrections(album_runs, resurrection_days)
    album_trans = album_transitions(album_runs, session_gap)
    weekly_album = weekly_dominance(plays, "album")
    weekly_artist = weekly_dominance(plays, "artist")
    album_eras = dominance_eras(weekly_album, "album")
    artist_eras = dominance_eras(weekly_artist, "artist")
    historical_albums, historical_artists = historical_vs_recent(
        plays, tracks, annotation_counts, users
    )
    depth = artist_depth(plays, tracks)

    # CSV principal
    play_rows = []
    for p in plays:
        play_rows.append({
            "datetime": p.at.isoformat(sep=" "),
            "date": p.at.date().isoformat(),
            "year": p.at.year,
            "month": p.at.strftime("%Y-%m"),
            "weekday": p.at.strftime("%A"),
            "hour": p.at.hour,
            "user": p.user_name,
            "track_id": p.track_id,
            "artist": p.track.artist,
            "album_artist": p.track.album_artist,
            "album": p.track.album,
            "album_id": p.track.album_id,
            "disc_number": p.track.disc_number,
            "track_number": p.track.track_number,
            "title": p.track.title,
            "genre": p.track.genre,
            "release_year": p.track.year or "",
            "duration": p.track.duration,
            "path": p.track.path,
            "mbid": p.track.mbid,
        })

    write_csv(outdir / "plays.csv", play_rows, [
        "datetime", "date", "year", "month", "weekday", "hour", "user",
        "track_id", "artist", "album_artist", "album", "album_id",
        "disc_number", "track_number", "title", "genre",
        "release_year", "duration", "path", "mbid"
    ])
    write_csv(outdir / "weekly_obsessions.csv", weekly, [
        "year", "week", "user", "plays", "week_total", "share",
        "artist", "title", "album", "track_id"
    ])
    write_csv(outdir / "resurrections.csv", res, [
        "user", "silent_days", "before", "return", "artist", "title", "album", "track_id"
    ])
    write_csv(outdir / "track_pairs.csv", pairs, [
        "user", "times", "from_artist", "from_title", "to_artist", "to_title",
        "from_track_id", "to_track_id"
    ])
    write_csv(outdir / "sessions.csv", sessions, [
        "session_id", "user", "start", "end", "span_minutes", "tracks",
        "unique_tracks", "estimated_music_minutes", "top_artist", "top_album", "top_genre"
    ])
    write_csv(outdir / "discoveries.csv", disc, [
        "user", "plays", "lifespan_days", "first_play", "last_play",
        "artist", "title", "album", "track_id"
    ])
    write_csv(outdir / "context_jumps.csv", jumps, [
        "user", "at", "score", "confidence",
        "from_artist", "from_title", "from_genre", "from_year",
        "to_artist", "to_title", "to_genre", "to_year", "evidence"
    ])

    top_track_rows = []
    for tid, n in track_counts.most_common():
        tr = tracks[tid]
        top_track_rows.append({
            "plays": n, "artist": tr.artist, "title": tr.title, "album": tr.album,
            "genre": tr.genre, "release_year": tr.year or "", "track_id": tid
        })
    write_csv(outdir / "top_tracks.csv", top_track_rows, [
        "plays", "artist", "title", "album", "genre", "release_year", "track_id"
    ])

    write_csv(outdir / "monthly.csv", [
        {"month": m, "plays": n} for m, n in sorted(month_counts.items())
    ], ["month", "plays"])

    write_csv(outdir / "album_runs.csv", album_runs, [
        "run_id", "user", "start", "end", "span_minutes", "album_key", "album_id",
        "artist", "album", "release_year", "library_tracks", "scrobbles",
        "unique_tracks", "completion_pct", "sequential_pct", "forward_pct",
        "first_position", "last_position", "starts_at_beginning", "reaches_end",
        "complete_like", "full_pass", "meaningful", "status", "listening_mode", "stop_track"
    ])
    write_csv(outdir / "album_completion.csv", album_completion, [
        "user", "album_key", "album_id", "artist", "album", "release_year",
        "library_tracks", "album_runs", "probable_full_plays", "near_complete_runs",
        "started_from_beginning", "probable_abandons", "avg_completion_pct",
        "median_completion_pct", "avg_sequential_pct", "favorite_stop_track",
        "full_play_rate_pct"
    ])
    write_csv(outdir / "album_resurrections.csv", album_res, [
        "user", "silent_days", "before", "return", "artist", "album",
        "before_completion_pct", "return_completion_pct", "album_key"
    ])
    write_csv(outdir / "album_transitions.csv", album_trans, [
        "user", "times", "from_artist", "from_album", "to_artist", "to_album",
        "from_album_key", "to_album_key"
    ])
    write_csv(outdir / "weekly_album_dominance.csv", weekly_album, [
        "user", "year", "week", "week_start", "plays", "week_total", "share_pct",
        "artist", "album", "entity_key"
    ])
    write_csv(outdir / "album_eras.csv", album_eras, [
        "user", "level", "start_week", "end_week", "weeks", "artist", "album",
        "dominant_plays", "period_total", "avg_share_pct"
    ])
    write_csv(outdir / "artist_eras.csv", artist_eras, [
        "user", "level", "start_week", "end_week", "weeks", "artist", "album",
        "dominant_plays", "period_total", "avg_share_pct"
    ])
    write_csv(outdir / "historical_vs_recent_albums.csv", historical_albums, [
        "user", "artist", "album", "album_key", "historical_total",
        "recent_scrobbles", "estimated_before_history", "recent_share_pct",
        "mismatch_tracks", "profile"
    ])
    write_csv(outdir / "historical_vs_recent_artists.csv", historical_artists, [
        "user", "artist", "historical_total", "recent_scrobbles",
        "estimated_before_history", "recent_share_pct", "mismatch_tracks", "profile"
    ])
    write_csv(outdir / "artist_depth.csv", depth, [
        "artist", "plays", "library_tracks", "heard_tracks", "track_breadth_pct",
        "library_albums", "heard_albums", "album_breadth_pct",
        "top_album_share_pct", "top_track_share_pct"
    ])

    lines: list[str] = []
    add = lines.append
    add("NAVIDROME — RADIOGRAFÍA DE ESCUCHA")
    add("=" * 72)
    add(f"Fuente temporal: {source.table}.{source.time_col}")
    add(f"Periodo: {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M}")
    add(f"Reproducciones registradas: {len(plays):,}".replace(",", "."))
    add(f"Canciones únicas escuchadas: {unique_tracks:,}".replace(",", "."))
    add(f"Reescuchas: {repeat_plays:,} ({pct(repeat_plays, len(plays))})".replace(",", "."))
    add(f"Tiempo musical teórico: {fmt_duration(total_duration)}")
    add(f"Sesiones estimadas (corte {session_gap} min): {len(sessions):,}".replace(",", "."))
    if sessions:
        med = statistics.median(s["tracks"] for s in sessions)
        add(f"Mediana de canciones por sesión: {med:g}")
    meaningful_album_runs = [r for r in album_runs if r["meaningful"]]
    full_album_runs = [r for r in meaningful_album_runs if r["full_pass"]]
    add(f"Pasadas de álbum detectadas: {len(meaningful_album_runs):,}".replace(",", "."))
    add(
        f"Escuchas completas probables: {len(full_album_runs):,} "
        f"({pct(len(full_album_runs), len(meaningful_album_runs))})".replace(",", ".")
    )
    add(f"Umbral de álbum casi/completo: {album_complete*100:.0f}%")
    add("")

    if len(user_counts) > 1:
        add("USUARIOS")
        add("-" * 72)
        for name, n in user_counts.most_common():
            add(f"{n:7d}  {name}")
        add("")

    add(f"TOP {top} ÁLBUMES POR ESCUCHAS COMPLETAS PROBABLES")
    add("-" * 72)
    shown = 0
    for r in album_completion:
        if r["probable_full_plays"] <= 0:
            continue
        add(
            f'{r["probable_full_plays"]:4d} completas | {r["album_runs"]:3d} pasadas | '
            f'{r["avg_completion_pct"]:5.1f}% media | {r["artist"]} — {r["album"]}'
        )
        shown += 1
        if shown >= top:
            break
    if not shown:
        add("(ningún álbum supera todavía los criterios de escucha completa)")
    add("")

    add(f"TOP {top} ÁLBUMES POR NÚMERO DE PASADAS")
    add("-" * 72)
    for r in sorted(album_completion, key=lambda x: (x["album_runs"], x["avg_completion_pct"]), reverse=True)[:top]:
        add(
            f'{r["album_runs"]:4d} pasadas | {r["probable_full_plays"]:3d} completas | '
            f'{r["median_completion_pct"]:5.1f}% mediana | {r["artist"]} — {r["album"]}'
        )
    if not album_completion:
        add("(sin pasadas de álbum significativas)")
    add("")

    add(f"ÁLBUMES QUE MÁS EMPIEZAS Y DEJAS A MEDIAS — primeras {top}")
    add("-" * 72)
    aband = [r for r in album_completion if r["probable_abandons"] > 0]
    aband.sort(key=lambda r: (r["probable_abandons"], r["started_from_beginning"]), reverse=True)
    for r in aband[:top]:
        stop = f' | corte típico: {r["favorite_stop_track"]}' if r["favorite_stop_track"] else ""
        add(
            f'{r["probable_abandons"]:3d} abandonos / {r["started_from_beginning"]:3d} inicios | '
            f'{r["artist"]} — {r["album"]}{stop}'
        )
    if not aband:
        add("(ninguno con el criterio actual)")
    add("")

    add(f"TRANSICIONES ENTRE ÁLBUMES — primeras {top}")
    add("-" * 72)
    repeated_album_trans = [r for r in album_trans if r["times"] >= 2]
    for r in repeated_album_trans[:top]:
        add(
            f'{r["times"]:3d}x  {r["from_artist"]} — {r["from_album"]}\n'
            f'      → {r["to_artist"]} — {r["to_album"]}'
        )
    if not repeated_album_trans:
        add("(no hay transiciones entre discos repetidas todavía)")
    add("")

    add(f"RESURRECCIONES DE ÁLBUM tras ≥ {resurrection_days} días — primeras {top}")
    add("-" * 72)
    for r in album_res[:top]:
        add(
            f'{int(r["silent_days"]):5d} días | {r["artist"]} — {r["album"]}\n'
            f'            {r["before"][:10]} → {r["return"][:10]}'
        )
    if not album_res:
        add("(ninguna en el periodo detallado)")
    add("")

    add(f"ERAS DE ARTISTA — primeras {top}")
    add("-" * 72)
    interesting_artist_eras = [r for r in artist_eras if r["weeks"] >= 2]
    for r in interesting_artist_eras[:top]:
        add(
            f'{r["start_week"]} → {r["end_week"]} | {r["weeks"]:2d} semanas | '
            f'{r["avg_share_pct"]:5.1f}% medio | {r["artist"]}'
        )
    if not interesting_artist_eras:
        add("(ningún artista domina dos semanas consecutivas en el periodo)")
    add("")

    add(f"ERAS DE ÁLBUM — primeras {top}")
    add("-" * 72)
    interesting_album_eras = [r for r in album_eras if r["weeks"] >= 2]
    for r in interesting_album_eras[:top]:
        add(
            f'{r["start_week"]} → {r["end_week"]} | {r["weeks"]:2d} semanas | '
            f'{r["avg_share_pct"]:5.1f}% medio | {r["artist"]} — {r["album"]}'
        )
    if not interesting_album_eras:
        add("(ningún álbum domina dos semanas consecutivas en el periodo)")
    add("")

    if historical_albums:
        add(f"HISTÓRICO VS PERIODO DETALLADO — ÁLBUMES, primeras {top}")
        add("-" * 72)
        for r in historical_albums[:top]:
            share = r["recent_share_pct"] if r["recent_share_pct"] != "" else 0
            add(
                f'{r["historical_total"]:5d} total | {r["recent_scrobbles"]:4d} recientes | '
                f'{str(share).replace(".", ","):>5}% | {r["profile"]:10s} | '
                f'{r["artist"]} — {r["album"]}'
            )
        add("")
        add(
            "Nota histórico/reciente: annotation.play_count y scrobbles no son "
            "contadores perfectamente equivalentes; la parte anterior al historial es una estimación."
        )
        add("")

    add(f"TOP {top} CANCIONES")
    add("-" * 72)
    for tid, n in track_counts.most_common(top):
        tr = tracks[tid]
        add(f"{n:7d}  {tr.artist} — {tr.title}")
    add("")

    add(f"TOP {top} ARTISTAS")
    add("-" * 72)
    for artist, n in artist_counts.most_common(top):
        add(f"{n:7d}  {artist}")
    add("")

    add(f"TOP {top} ÁLBUMES POR SCROBBLES")
    add("-" * 72)
    for (artist, album), n in album_counts.most_common(top):
        add(f"{n:7d}  {artist} — {album}")
    add("")

    if genre_counts:
        add(f"TOP {top} GÉNEROS (según tags)")
        add("-" * 72)
        for genre, n in genre_counts.most_common(top):
            add(f"{n:7d}  {genre}")
        add("")

    add("ACTIVIDAD POR AÑO")
    add("-" * 72)
    for y, n in sorted(year_counts.items()):
        add(f"{y}: {n:,}".replace(",", "."))
    add("")

    add("HORAS DEL DÍA")
    add("-" * 72)
    for h in range(24):
        n = hour_counts[h]
        if n:
            add(f"{h:02d}:00  {n:7d}  {'█' * max(1, round(30*n/max(hour_counts.values())))}")
    add("")

    add(f"OBSESIONES SEMANALES — primeras {top}")
    add("-" * 72)
    for r in sorted(weekly, key=lambda x: x["plays"], reverse=True)[:top]:
        add(
            f'{r["year"]}-W{int(r["week"]):02d}  {r["plays"]:3d}x '
            f'({str(r["share"]).replace(".", ",")}%)  {r["artist"]} — {r["title"]}'
        )
    if not weekly:
        add("(sin casos con el umbral actual)")
    add("")

    add(f"RESURRECCIONES tras ≥ {resurrection_days} días — primeras {top}")
    add("-" * 72)
    for r in res[:top]:
        add(
            f'{int(r["silent_days"]):5d} días  {r["artist"]} — {r["title"]}\n'
            f'            {r["before"][:10]} → {r["return"][:10]}'
        )
    if not res:
        add("(ninguna)")
    add("")

    add(f"PAREJAS DE PISTAS (secundario) dentro de {pair_window} min — primeras {top}")
    add("-" * 72)
    for r in pairs[:top]:
        add(
            f'{r["times"]:4d}x  {r["from_artist"]} — {r["from_title"]}\n'
            f'       → {r["to_artist"]} — {r["to_title"]}'
        )
    if not pairs:
        add("(ninguna repetida)")
    add("")

    add(f"SESIONES MÁS INTENSAS — primeras {top}")
    add("-" * 72)
    for s in sorted(sessions, key=lambda x: x["tracks"], reverse=True)[:top]:
        add(
            f'{s["tracks"]:4d} pistas | {s["span_minutes"]:7.1f} min | '
            f'{s["start"][:16]} | {s["top_artist"]}'
        )
    add("")

    add("DESCUBRIMIENTOS / FIJACIONES DURADERAS")
    add("-" * 72)
    for r in disc[:top]:
        add(
            f'{r["lifespan_days"]:8.0f} días | {r["plays"]:4d}x | '
            f'{r["artist"]} — {r["title"]}'
        )
    if not disc:
        add("(sin suficientes repeticiones)")
    add("")

    if jumps:
        add("SALTOS DE CONTEXTO MÁS BRUSCOS")
        add("-" * 72)
        for r in jumps[:top]:
            add(
                f'{r["score"]:5.1f}/100 [{r["confidence"]}] '
                f'{r["from_artist"]} — {r["from_title"]}\n'
                f'              → {r["to_artist"]} — {r["to_title"]}'
            )
        add("")
        add(
            "Nota: el score de salto es heurístico. Es bastante más útil cuando "
            "la biblioteca tiene tags de género y año bien cuidados."
        )
        add("")

    return "\n".join(lines) + "\n"


# ---------- Fallback Navidrome antiguo: annotations ----------

def legacy_annotations(
    db: sqlite3.Connection,
    schema: Schema,
    tracks: dict[str, Track],
    users: dict[str, str],
    selected_users: set[str] | None,
    top: int,
    outdir: Path,
) -> str:
    if not schema.has("annotation"):
        return (
            "No encuentro historial temporal ni tabla annotation compatible.\n"
            "Ejecuta con --inspect y revisa schema.txt.\n"
        )

    t = "annotation"
    uidc = schema.col(t, ("user_id",))
    itemc = schema.col(t, ("item_id",))
    typec = schema.col(t, ("item_type",))
    countc = schema.col(t, ("play_count", "playcount"))
    datec = schema.col(t, ("play_date", "last_played", "played_at"))

    if not itemc or not countc:
        return (
            "Existe annotation, pero no reconozco sus columnas de reproducción.\n"
            "Ejecuta con --inspect y revisa schema.txt.\n"
        )

    select = [
        f"{qident(itemc)} AS item_id",
        f"{qident(countc)} AS play_count",
        f"{qident(datec)} AS play_date" if datec else "NULL AS play_date",
        f"{qident(uidc)} AS user_id" if uidc else "'' AS user_id",
    ]
    where = [f"COALESCE({qident(countc)}, 0) > 0"]
    args: list[Any] = []

    if typec:
        vals = [norm(r[0]) for r in db.execute(
            f"SELECT DISTINCT {qident(typec)} FROM {qident(t)} LIMIT 30"
        )]
        accepted = next((v for v in vals if v.lower() in {"media_file", "song", "track"}), None)
        if accepted:
            where.append(f"{qident(typec)} = ?")
            args.append(accepted)

    if selected_users and uidc:
        marks = ",".join("?" for _ in selected_users)
        where.append(f"{qident(uidc)} IN ({marks})")
        args.extend(sorted(selected_users))

    sql = f"SELECT {', '.join(select)} FROM {qident(t)} WHERE " + " AND ".join(where)

    rows = []
    for r in db.execute(sql, args):
        tid = norm(r["item_id"])
        tr = tracks.get(tid)
        if not tr:
            continue
        uid = norm(r["user_id"])
        try:
            n = int(r["play_count"] or 0)
        except (TypeError, ValueError):
            n = 0
        rows.append({
            "user": users.get(uid, uid or "(usuario desconocido)"),
            "play_count": n,
            "last_played": norm(r["play_date"]),
            "track_id": tid,
            "artist": tr.artist,
            "album": tr.album,
            "title": tr.title,
            "genre": tr.genre,
            "release_year": tr.year or "",
        })

    rows.sort(key=lambda x: x["play_count"], reverse=True)
    write_csv(outdir / "legacy_playcounts.csv", rows, [
        "user", "play_count", "last_played", "track_id",
        "artist", "album", "title", "genre", "release_year"
    ])

    artist_counts: Counter[str] = Counter()
    album_counts: Counter[tuple[str, str]] = Counter()
    total = 0
    for r in rows:
        n = r["play_count"]
        total += n
        artist_counts[r["artist"]] += n
        album_counts[(r["artist"], r["album"])] += n

    lines = [
        "NAVIDROME — RADIOGRAFÍA DE ESCUCHA (MODO LEGACY)",
        "=" * 72,
        "",
        "La base no contiene un historial temporal de cada reproducción reconocible.",
        "Puedo analizar contadores acumulados, pero NO reconstruir semanas, sesiones,",
        "parejas, resurrecciones ni secuencias históricas con fiabilidad.",
        "",
        f"Reproducciones acumuladas según annotation: {total:,}".replace(",", "."),
        f"Canciones con play_count > 0: {len(rows):,}".replace(",", "."),
        "",
        f"TOP {top} CANCIONES",
        "-" * 72,
    ]
    for r in rows[:top]:
        lines.append(f'{r["play_count"]:7d}  {r["artist"]} — {r["title"]}')

    lines += ["", f"TOP {top} ARTISTAS", "-" * 72]
    for artist, n in artist_counts.most_common(top):
        lines.append(f"{n:7d}  {artist}")

    lines += ["", f"TOP {top} ÁLBUMES", "-" * 72]
    for (artist, album), n in album_counts.most_common(top):
        lines.append(f"{n:7d}  {artist} — {album}")

    lines += [
        "",
        "Navidrome añadió historial nativo de scrobbles en la versión 0.59.",
        "Las reproducciones anteriores a esa función no pueden reconstruirse a partir",
        "del contador agregado.",
        "",
    ]
    return "\n".join(lines)


# ---------- Estadísticas de biblioteca ----------

def library_report(
    tracks: dict[str, Track],
    plays: list[Play] | None,
) -> str:
    total_tracks = len(tracks)
    artists = {t.artist for t in tracks.values()}
    albums = {(t.artist, t.album) for t in tracks.values()}
    genres = Counter(g for t in tracks.values() for g in split_genres(t.genre))
    total_duration = sum(t.duration for t in tracks.values())

    lines = [
        "BIBLIOTECA",
        "=" * 72,
        f"Canciones catalogadas: {total_tracks:,}".replace(",", "."),
        f"Artistas: {len(artists):,}".replace(",", "."),
        f"Álbumes: {len(albums):,}".replace(",", "."),
        f"Duración total catalogada: {fmt_duration(total_duration)}",
    ]

    if plays is not None:
        heard = {p.track_id for p in plays}
        unplayed = max(0, total_tracks - len(heard))
        lines += [
            f"Canciones escuchadas en el historial disponible: {len(heard):,}".replace(",", "."),
            f"Nunca vistas en ese historial: {unplayed:,} ({pct(unplayed, total_tracks)})".replace(",", "."),
        ]

    if genres:
        lines += ["", "Géneros más presentes en la biblioteca:"]
        for g, n in genres.most_common(20):
            lines.append(f"{n:7d}  {g}")

    return "\n".join(lines) + "\n"


# ---------- Main ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Genera una radiografía integral de escucha desde navidrome.db."
    )
    ap.add_argument("database", type=Path, help="Ruta a navidrome.db")
    ap.add_argument("-o", "--output", type=Path, default=Path("navidrome_report"))
    ap.add_argument("--user", help="Nombre o ID de usuario; por defecto analiza todos.")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--session-gap", type=int, default=30,
                    help="Minutos de inactividad para cortar sesión (default: 30).")
    ap.add_argument("--pair-window", type=int, default=30,
                    help="Máximo de minutos entre dos pistas asociadas (default: 30).")
    ap.add_argument("--resurrection-days", type=int, default=180,
                    help="Silencio mínimo para considerar resurrección (default: 180 días).")
    ap.add_argument("--album-complete", type=float, default=0.80,
                    help="Fracción de pistas para considerar un álbum casi/completo (default: 0.80).")
    ap.add_argument("--album-thread-hours", type=float, default=48.0,
                    help="Horas máximas para retomar el hilo lógico de un álbum (default: 48).")
    ap.add_argument("--resume-slack", type=int, default=2,
                    help="Pistas de holgura al reconocer una continuación (default: 2).")
    ap.add_argument("--inspect", action="store_true",
                    help="Además guarda el esquema SQLite completo en schema.txt.")
    args = ap.parse_args()

    db_path = args.database.expanduser().resolve()
    if not db_path.exists():
        print(f"No existe: {db_path}", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)

    # mode=ro: este script no escribe absolutamente nada en Navidrome.
    uri = db_path.as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row

    try:
        schema = Schema(db)
        if args.inspect:
            (args.output / "schema.txt").write_text(schema.dump() + "\n", encoding="utf-8")

        tracks = load_tracks(db, schema)
        users = load_users(db, schema)
        selected = resolve_user_ids(users, args.user)
        annotation_counts = load_annotation_playcounts(db, schema, selected)

        if users:
            print("Usuarios detectados:")
            for uid, name in users.items():
                mark = " *" if selected and uid in selected else ""
                print(f"  {name} [{uid}]{mark}")

        source = detect_history_source(schema)

        if source:
            print(
                f"Historial temporal detectado: {source.table} "
                f"(pista={source.media_col}, fecha={source.time_col})"
            )
            plays = load_plays(db, schema, source, tracks, users, selected)

            report = history_report_v4(
                plays=plays,
                tracks=tracks,
                top=max(1, args.top),
                session_gap=max(1, args.session_gap),
                pair_window=max(1, args.pair_window),
                resurrection_days=max(1, args.resurrection_days),
                album_complete=min(1.0, max(0.50, args.album_complete)),
                album_thread_hours=max(1.0, args.album_thread_hours),
                resume_slack=max(0, args.resume_slack),
                outdir=args.output,
                source=source,
                annotation_counts=annotation_counts,
                users=users,
            )
            report += "\n" + library_report(tracks, plays)
        else:
            print("No encuentro historial temporal; usando play_count de annotation.")
            report = legacy_annotations(
                db, schema, tracks, users, selected, max(1, args.top), args.output
            )
            report += "\n" + library_report(tracks, None)

        (args.output / "report.txt").write_text(report, encoding="utf-8")
        print(f"\nInforme: {args.output / 'report.txt'}")
        print(f"Salida:   {args.output}")
        return 0

    except sqlite3.DatabaseError as e:
        print(f"Error SQLite: {e}", file=sys.stderr)
        print(
            "Consejo: usa la ruta real de navidrome.db. Si es Docker, suele estar "
            "en el volumen del host mapeado a /data.",
            file=sys.stderr,
        )
        return 3
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
