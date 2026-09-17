from __future__ import annotations

import difflib
import math
import re
import statistics
import unicodedata
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from models import Play, Track
from utils import norm, parse_dt


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
                next_pos
                and next_pos <= 2
                and prev_pos >= max(3, math.ceil(total_pos * 0.60))
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
        transitions = list(pairwise(pos))
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
        rows.append(
            {
                "run_id": idx,
                "user": first.user_name,
                "start": first.at.isoformat(sep=" "),
                "end": chunk[-1].at.isoformat(sep=" "),
                "span_minutes": round(
                    (chunk[-1].at - first.at).total_seconds() / 60, 1
                ),
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
            }
        )
    return rows


def aggregate_album_completion(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        if r["meaningful"]:
            grouped[(r["user"], r["album_key"])].append(r)

    rows: list[dict[str, Any]] = []
    for rs in grouped.values():
        completions = [float(r["completion_pct"]) for r in rs]
        sequential = [float(r["sequential_pct"]) for r in rs]
        sample = rs[0]
        full = sum(int(r["full_pass"]) for r in rs)
        near = sum(int(r["complete_like"]) for r in rs)
        starts = [r for r in rs if r["starts_at_beginning"]]
        abandons = sum(
            1
            for r in starts
            if not r["complete_like"] and float(r["completion_pct"]) >= 15
        )
        stop_counter = Counter(
            r["stop_track"] for r in starts if not r["complete_like"]
        )
        rows.append(
            {
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
                "favorite_stop_track": stop_counter.most_common(1)[0][0]
                if stop_counter
                else "",
                "full_play_rate_pct": round(100 * full / len(rs), 1),
            }
        )
    rows.sort(
        key=lambda r: (
            r["probable_full_plays"],
            r["album_runs"],
            r["avg_completion_pct"],
        ),
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
    for rs in grouped.values():
        rs.sort(key=lambda r: r["start"])
        for a, b in pairwise(rs):
            da = parse_dt(a["start"])
            db = parse_dt(b["start"])
            if not da or not db:
                continue
            gap = (db - da).total_seconds() / 86400
            if gap >= resurrection_days:
                rows.append(
                    {
                        "user": b["user"],
                        "silent_days": round(gap, 1),
                        "before": a["start"],
                        "return": b["start"],
                        "artist": b["artist"],
                        "album": b["album"],
                        "before_completion_pct": a["completion_pct"],
                        "return_completion_pct": b["completion_pct"],
                        "album_key": b["album_key"],
                    }
                )
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

    for a, b in pairwise(meaningful):
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
        rows.append(
            {
                "user": a["user"],
                "times": n,
                "from_artist": a["artist"],
                "from_album": a["album"],
                "to_artist": b["artist"],
                "to_album": b["album"],
                "from_album_key": a["album_key"],
                "to_album_key": b["album_key"],
            }
        )
    rows.sort(key=lambda r: r["times"], reverse=True)
    return rows


def _text_key(value: str) -> str:
    s = (
        unicodedata.normalize("NFKD", norm(value))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
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
    rows.sort(
        key=lambda r: (
            severity.get(r["confidence"], 9),
            r["artist"],
            r["album"],
            r["track_number"],
        )
    )
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
                next_pos
                and prev_pos
                and next_pos <= 2
                and prev_pos >= max(3, math.ceil(total * 0.55))
            )

            # Retroceso amplio también suele señalar nueva pasada.
            backward_restart = bool(
                next_pos and prev_pos and next_pos < prev_pos - max(1, resume_slack)
            )

            # Continuación lógica: siguiente pista o una muy próxima hacia delante.
            near_forward = bool(
                next_pos
                and prev_pos
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
        p24 = [
            positions.get(p.track_id, 0)
            for p in chunk
            if p.at - start_dt <= timedelta(hours=24)
        ]
        p48 = [
            positions.get(p.track_id, 0)
            for p in chunk
            if p.at - start_dt <= timedelta(hours=48)
        ]
        cov24 = len({x for x in p24 if x > 0}) / total
        cov48 = len({x for x in p48 if x > 0}) / total
        ord24 = _lis_length(p24) / total
        ord48 = _lis_length(p48) / total

        gaps = [(b.at - a.at) for a, b in pairwise(chunk)]
        resume_gaps = [g for g in gaps if g > session_gap]
        max_pause_h = max((g.total_seconds() / 3600 for g in gaps), default=0.0)

        # Confianza de metadatos de las pistas que sustentan este hilo.
        qualities = [metadata_quality(p.track)["confidence"] for p in chunk]
        confidence = (
            "baja"
            if "baja" in qualities
            else ("media" if "media" in qualities else "alta")
        )

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

        threads.append(
            {
                "thread_id": idx,
                "user": first.user_name,
                "start": first.at.isoformat(sep=" "),
                "end": chunk[-1].at.isoformat(sep=" "),
                "span_hours": round(
                    (chunk[-1].at - first.at).total_seconds() / 3600, 2
                ),
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
            }
        )

        for a, b in pairwise(chunk):
            gap = b.at - a.at
            if gap <= session_gap:
                continue
            ia = global_index[id(a)]
            ib = global_index[id(b)]
            interleaved = max(0, ib - ia - 1)
            resumes.append(
                {
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
                }
            )

    resumes.sort(key=lambda r: r["pause_hours"], reverse=True)
    return threads, resumes


def aggregate_album_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in threads:
        if r["meaningful"]:
            grouped[(r["user"], r["album_key"])].append(r)

    rows: list[dict[str, Any]] = []
    for rs in grouped.values():
        sample = rs[0]
        full = sum(int(r["full_thread"]) for r in rs)
        resumed = sum(1 for r in rs if int(r["resume_count"]) > 0)
        starts = [r for r in rs if r["starts_at_beginning"]]
        # "Abandono" sólo tras agotar la ventana lógica; ya no depende de 30 min.
        abandons = sum(
            1
            for r in starts
            if not r["complete_like"] and float(r["coverage_pct"]) >= 15
        )
        rows.append(
            {
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
                "avg_coverage_pct": round(
                    statistics.mean(float(r["coverage_pct"]) for r in rs), 1
                ),
                "median_coverage_pct": round(
                    statistics.median(float(r["coverage_pct"]) for r in rs), 1
                ),
                "avg_ordered_coverage_pct": round(
                    statistics.mean(float(r["ordered_coverage_pct"]) for r in rs), 1
                ),
                "avg_coverage_24h_pct": round(
                    statistics.mean(float(r["coverage_24h_pct"]) for r in rs), 1
                ),
                "avg_coverage_48h_pct": round(
                    statistics.mean(float(r["coverage_48h_pct"]) for r in rs), 1
                ),
                "full_play_rate_pct": round(100 * full / len(rs), 1),
            }
        )
    rows.sort(
        key=lambda r: (
            r["probable_full_plays"],
            r["logical_threads"],
            r["avg_coverage_pct"],
        ),
        reverse=True,
    )
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
        for a, b in pairwise(rs):
            da, db = parse_dt(a["end"]), parse_dt(b["start"])
            if not da or not db:
                continue
            gap = (db - da).total_seconds() / 86400
            if gap >= resurrection_days:
                rows.append(
                    {
                        "user": b["user"],
                        "silent_days": round(gap, 1),
                        "before": a["end"],
                        "return": b["start"],
                        "artist": b["artist"],
                        "album": b["album"],
                        "before_coverage_pct": a["coverage_pct"],
                        "return_coverage_pct": b["coverage_pct"],
                        "album_key": b["album_key"],
                    }
                )
    rows.sort(key=lambda r: r["silent_days"], reverse=True)
    return rows
