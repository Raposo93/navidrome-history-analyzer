from __future__ import annotations

import re
import sqlite3
import sys
from collections.abc import Iterable
from typing import Any

from models import AnnotationPlay, HistorySource, Play, Track
from utils import norm, parse_dt, qident


class Schema:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.tables = [
            r[0]
            for r in db.execute(
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


def detect_history_source(schema: Schema) -> HistorySource | None:
    media_candidates = (
        "media_file_id",
        "mediafile_id",
        "song_id",
        "track_id",
        "item_id",
        "media_id",
    )
    user_candidates = ("user_id", "userid")
    time_candidates = (
        "play_date",
        "played_at",
        "scrobbled_at",
        "submission_time",
        "timestamp",
        "time_stamp",
        "played_time",
        "created_at",
        "date",
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
                (
                    score,
                    HistorySource(
                        table=table,
                        media_col=media_col,
                        user_col=user_col,
                        time_col=time_col,
                        item_type_col=item_type_col,
                    ),
                )
            )

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


# ---------- Metadatos ----------

MEDIA_TABLE = "media_file"


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
        uid
        for uid, name in users.items()
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
            norm(r[0])
            for r in db.execute(
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
        print(
            f"Aviso: {missing_track} reproducciones apuntan a canciones no presentes.",
            file=sys.stderr,
        )
    if bad_date:
        print(
            f"Aviso: {bad_date} reproducciones tenían fecha no reconocible.",
            file=sys.stderr,
        )
    return plays


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


def load_annotation_history(
    db: sqlite3.Connection,
    schema: Schema,
    selected_users: set[str] | None,
) -> dict[tuple[str, str], AnnotationPlay]:
    if not schema.has("annotation"):
        return {}

    table = "annotation"
    user_column = schema.col(table, ("user_id",))
    item_column = schema.col(table, ("item_id",))
    type_column = schema.col(table, ("item_type",))
    count_column = schema.col(table, ("play_count", "playcount"))
    date_column = schema.col(table, ("play_date", "last_played", "played_at"))
    if not item_column or not count_column:
        return {}

    select = [
        f"{qident(item_column)} AS item_id",
        f"{qident(count_column)} AS play_count",
        f"{qident(user_column)} AS user_id" if user_column else "'' AS user_id",
        f"{qident(date_column)} AS play_date" if date_column else "NULL AS play_date",
    ]
    where = [f"COALESCE({qident(count_column)}, 0) > 0"]
    arguments: list[Any] = []

    if type_column:
        values = [
            norm(row[0])
            for row in db.execute(
                f"SELECT DISTINCT {qident(type_column)} FROM {qident(table)} LIMIT 30"
            )
        ]
        accepted = next(
            (
                value
                for value in values
                if value.lower() in {"media_file", "song", "track"}
            ),
            None,
        )
        if accepted:
            where.append(f"{qident(type_column)} = ?")
            arguments.append(accepted)

    if selected_users and user_column:
        placeholders = ",".join("?" for _ in selected_users)
        where.append(f"{qident(user_column)} IN ({placeholders})")
        arguments.extend(sorted(selected_users))

    sql = f"SELECT {', '.join(select)} FROM {qident(table)} WHERE " + " AND ".join(
        where
    )
    history: dict[tuple[str, str], AnnotationPlay] = {}
    for row in db.execute(sql, arguments):
        try:
            play_count = int(row["play_count"] or 0)
        except (TypeError, ValueError):
            continue
        key = (norm(row["user_id"]), norm(row["item_id"]))
        history[key] = AnnotationPlay(
            play_count=play_count,
            last_played=parse_dt(row["play_date"]),
        )
    return history


def load_legacy_playcounts(
    db: sqlite3.Connection,
    schema: Schema,
    tracks: dict[str, Track],
    users: dict[str, str],
    selected_users: set[str] | None,
) -> list[dict[str, Any]]:
    if not schema.has("annotation"):
        raise RuntimeError(
            "No encuentro historial temporal ni tabla annotation compatible.\n"
            "Ejecuta con --inspect y revisa schema.txt.\n"
        )

    table = "annotation"
    user_column = schema.col(table, ("user_id",))
    item_column = schema.col(table, ("item_id",))
    type_column = schema.col(table, ("item_type",))
    count_column = schema.col(table, ("play_count", "playcount"))
    date_column = schema.col(table, ("play_date", "last_played", "played_at"))

    if not item_column or not count_column:
        raise RuntimeError(
            "Existe annotation, pero no reconozco sus columnas de reproducción.\n"
            "Ejecuta con --inspect y revisa schema.txt.\n"
        )

    select = [
        f"{qident(item_column)} AS item_id",
        f"{qident(count_column)} AS play_count",
        f"{qident(date_column)} AS play_date" if date_column else "NULL AS play_date",
        f"{qident(user_column)} AS user_id" if user_column else "'' AS user_id",
    ]
    where = [f"COALESCE({qident(count_column)}, 0) > 0"]
    arguments: list[Any] = []

    if type_column:
        values = [
            norm(row[0])
            for row in db.execute(
                f"SELECT DISTINCT {qident(type_column)} FROM {qident(table)} LIMIT 30"
            )
        ]
        accepted = next(
            (
                value
                for value in values
                if value.lower() in {"media_file", "song", "track"}
            ),
            None,
        )
        if accepted:
            where.append(f"{qident(type_column)} = ?")
            arguments.append(accepted)

    if selected_users and user_column:
        placeholders = ",".join("?" for _ in selected_users)
        where.append(f"{qident(user_column)} IN ({placeholders})")
        arguments.extend(sorted(selected_users))

    sql = f"SELECT {', '.join(select)} FROM {qident(table)} WHERE " + " AND ".join(
        where
    )

    rows: list[dict[str, Any]] = []
    for row in db.execute(sql, arguments):
        track_id = norm(row["item_id"])
        track = tracks.get(track_id)
        if not track:
            continue
        user_id = norm(row["user_id"])
        try:
            play_count = int(row["play_count"] or 0)
        except (TypeError, ValueError):
            play_count = 0
        rows.append(
            {
                "user": users.get(user_id, user_id or "(usuario desconocido)"),
                "play_count": play_count,
                "last_played": norm(row["play_date"]),
                "track_id": track_id,
                "artist": track.artist,
                "album": track.album,
                "title": track.title,
                "genre": track.genre,
                "release_year": track.year or "",
            }
        )

    rows.sort(key=lambda item: item["play_count"], reverse=True)
    return rows
