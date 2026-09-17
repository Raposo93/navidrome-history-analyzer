from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from albums import album_key
from models import Play, Track
from utils import split_genres


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
        rows.append(
            {
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
            }
        )
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
        rows.append(
            {
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
            }
        )
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
        for a, b in pairwise(ps):
            gap = (b.at - a.at).total_seconds() / 86400
            if gap >= days:
                tr = b.track
                rows.append(
                    {
                        "user": b.user_name,
                        "silent_days": round(gap, 1),
                        "before": a.at.isoformat(sep=" "),
                        "return": b.at.isoformat(sep=" "),
                        "artist": tr.artist,
                        "title": tr.title,
                        "album": tr.album,
                        "track_id": tid,
                    }
                )
    rows.sort(key=lambda r: r["silent_days"], reverse=True)
    return rows


def track_pairs(plays: list[Play], window_minutes: int) -> list[dict[str, Any]]:
    win = timedelta(minutes=window_minutes)
    c: Counter[tuple[str, str, str]] = Counter()
    sample: dict[tuple[str, str, str], tuple[Track, Track, str]] = {}

    for a, b in pairwise(plays):
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
        rows.append(
            {
                "user": user,
                "times": n,
                "from_artist": ta.artist,
                "from_title": ta.title,
                "to_artist": tb.artist,
                "to_title": tb.title,
                "from_track_id": ta.id,
                "to_track_id": tb.id,
            }
        )
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

    for a, b in pairwise(plays):
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
        confidence = (
            "alta"
            if ga and gb and a.track.year and b.track.year
            else ("media" if ga and gb else "baja")
        )

        rows.append(
            {
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
            }
        )

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
        rows.append(
            {
                "user": first.user_name,
                "plays": len(ps),
                "lifespan_days": round(lifespan, 1),
                "first_play": first.at.isoformat(sep=" "),
                "last_play": last.at.isoformat(sep=" "),
                "artist": first.track.artist,
                "title": first.track.title,
                "album": first.track.album,
                "track_id": tid,
            }
        )
    rows.sort(key=lambda r: (r["lifespan_days"], r["plays"]), reverse=True)
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
        rows.append(
            {
                "user": user,
                "year": year,
                "week": week,
                "week_start": datetime.fromisocalendar(year, week, 1)
                .date()
                .isoformat(),
                "plays": n,
                "week_total": totals[(user, year, week)],
                "share_pct": round(100 * n / totals[(user, year, week)], 1),
                "artist": artist,
                "album": album,
                "entity_key": key,
            }
        )
    return rows


def dominance_eras(weekly: list[dict[str, Any]], level: str) -> list[dict[str, Any]]:
    if not weekly:
        return []
    rows: list[dict[str, Any]] = []
    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in weekly:
        by_user[r["user"]].append(r)

    for wrs in by_user.values():
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

    rows.sort(
        key=lambda r: (r["weeks"], r["dominant_plays"], r["avg_share_pct"]),
        reverse=True,
    )
    return rows


def _finish_era(rs: list[dict[str, Any]], level: str) -> dict[str, Any]:
    first, last = rs[0], rs[-1]
    return {
        "user": first["user"],
        "level": level,
        "start_week": f"{first['year']}-W{int(first['week']):02d}",
        "end_week": f"{last['year']}-W{int(last['week']):02d}",
        "weeks": len(rs),
        "artist": first["artist"],
        "album": first["album"],
        "dominant_plays": sum(int(r["plays"]) for r in rs),
        "period_total": sum(int(r["week_total"]) for r in rs),
        "avg_share_pct": round(statistics.mean(float(r["share_pct"]) for r in rs), 1),
    }


def historical_vs_recent(
    plays: list[Play],
    tracks: dict[str, Track],
    annotations: dict[tuple[str, str], int],
    users: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    recent_track: Counter[tuple[str, str]] = Counter(
        (p.user_id, p.track_id) for p in plays
    )
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
        mismatch = int(recent > total > 0)
        user = users.get(uid, uid or "(usuario desconocido)")

        ak = (uid, album_key(tr))
        aa = album_acc.setdefault(
            ak,
            {
                "user": user,
                "artist": tr.album_artist or tr.artist,
                "album": tr.album,
                "album_key": album_key(tr),
                "historical_total": 0,
                "recent_scrobbles": 0,
                "estimated_before_history": 0,
                "mismatch_tracks": 0,
            },
        )
        aa["historical_total"] += total
        aa["recent_scrobbles"] += recent
        aa["estimated_before_history"] += older
        aa["mismatch_tracks"] += mismatch

        artist_name = tr.album_artist or tr.artist
        ar = artist_acc.setdefault(
            (uid, artist_name.casefold()),
            {
                "user": user,
                "artist": artist_name,
                "historical_total": 0,
                "recent_scrobbles": 0,
                "estimated_before_history": 0,
                "mismatch_tracks": 0,
            },
        )
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
        rows.sort(
            key=lambda r: (r["historical_total"], r["recent_scrobbles"]), reverse=True
        )
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
        rows.append(
            {
                "artist": labels[k],
                "plays": plays_n,
                "library_tracks": lt,
                "heard_tracks": ht,
                "track_breadth_pct": round(100 * ht / lt, 1) if lt else 0,
                "library_albums": la,
                "heard_albums": ha,
                "album_breadth_pct": round(100 * ha / la, 1) if la else 0,
                "top_album_share_pct": round(100 * top_album / plays_n, 1)
                if plays_n
                else 0,
                "top_track_share_pct": round(100 * top_track / plays_n, 1)
                if plays_n
                else 0,
            }
        )
    rows.sort(key=lambda r: (r["plays"], r["track_breadth_pct"]), reverse=True)
    return rows
