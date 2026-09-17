from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from albums import (
    aggregate_album_completion,
    aggregate_album_threads,
    album_resurrections,
    album_thread_resurrections,
    album_transitions,
    build_album_runs,
    build_album_threads,
    metadata_issues,
)
from analysis import (
    artist_depth,
    build_sessions,
    context_jumps,
    discoveries,
    dominance_eras,
    historical_vs_recent,
    resurrections,
    track_pairs,
    weekly_dominance,
    weekly_obsessions,
)
from exports import write_export
from models import HistorySource, Play, Track
from utils import fmt_duration, pct, split_genres


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

    write_export(outdir / "album_threads.csv", threads)
    write_export(outdir / "album_thread_completion.csv", completion)
    write_export(outdir / "album_resume_events.csv", resumes)
    write_export(outdir / "album_thread_resurrections.csv", thread_res)
    write_export(outdir / "metadata_issues.csv", issues)

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
        f"Completos probables: {len(full):,} ({pct(len(full), len(meaningful))})".replace(
            ",", "."
        )
    )
    add(
        f"Hilos con al menos una pausa > {session_gap} min: "
        f"{len(resumed):,} ({pct(len(resumed), len(meaningful))})".replace(",", ".")
    )
    add(
        f"Incidencias de metadatos detectadas en biblioteca: {len(issues):,}".replace(
            ",", "."
        )
    )
    if low_meta:
        add(
            f"Hilos con confianza baja de metadatos: {len(low_meta):,}".replace(
                ",", "."
            )
        )
    add("")

    add(f"TOP {top} ÁLBUMES — HILOS COMPLETOS PROBABLES")
    add("-" * 72)
    shown = 0
    for r in completion:
        if r["probable_full_plays"] <= 0:
            continue
        add(
            f"{r['probable_full_plays']:4d} completas | {r['logical_threads']:3d} hilos | "
            f"{r['resumed_threads']:3d} retomados | {r['avg_coverage_pct']:5.1f}% cobertura | "
            f"{r['artist']} — {r['album']}"
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
            f"{r['pause_hours']:6.2f} h | pista {r['from_position']} → {r['to_position']} | "
            f"{r['artist']} — {r['album']}\n"
            f"          {r['from_title']} → {r['to_title']} "
            f"| scrobbles intercalados: {r['interleaved_scrobbles']}"
        )
    if not resumes:
        add("(ninguna pausa superior al corte de sesión)")
    add("")

    add(f"POSIBLES ABANDONOS REALES — tras ventana de {album_thread_hours:g} h")
    add("-" * 72)
    aband = [r for r in completion if r["probable_abandons_after_window"] > 0]
    aband.sort(
        key=lambda r: (r["probable_abandons_after_window"], r["logical_threads"]),
        reverse=True,
    )
    for r in aband[:top]:
        add(
            f"{r['probable_abandons_after_window']:3d} posibles / {r['logical_threads']:3d} hilos | "
            f"{r['median_coverage_pct']:5.1f}% mediana | {r['artist']} — {r['album']}"
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
            f"{r['avg_coverage_24h_pct']:5.1f}% / {r['avg_coverage_48h_pct']:5.1f}% "
            f"| orden {r['avg_ordered_coverage_pct']:5.1f}% | "
            f"{r['artist']} — {r['album']}"
        )
    add("")

    if issues:
        add(f"METADATOS SOSPECHOSOS — primeros {min(top, len(issues))}")
        add("-" * 72)
        for r in issues[:top]:
            nums = ""
            if r["path_track_number"]:
                nums = f" track={r['track_number']}, fichero={r['path_track_number']};"
            add(
                f"[{r['confidence']}] {r['artist']} — {r['album']} — {r['title']}\n"
                f"          {r['issues']};{nums} {r['path']}"
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
        play_rows.append(
            {
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
            }
        )

    write_export(outdir / "plays.csv", play_rows)
    write_export(outdir / "weekly_obsessions.csv", weekly)
    write_export(outdir / "resurrections.csv", res)
    write_export(outdir / "track_pairs.csv", pairs)
    write_export(outdir / "sessions.csv", sessions)
    write_export(outdir / "discoveries.csv", disc)
    write_export(outdir / "context_jumps.csv", jumps)

    top_track_rows = []
    for tid, n in track_counts.most_common():
        tr = tracks[tid]
        top_track_rows.append(
            {
                "plays": n,
                "artist": tr.artist,
                "title": tr.title,
                "album": tr.album,
                "genre": tr.genre,
                "release_year": tr.year or "",
                "track_id": tid,
            }
        )
    write_export(outdir / "top_tracks.csv", top_track_rows)

    write_export(
        outdir / "monthly.csv",
        [{"month": m, "plays": n} for m, n in sorted(month_counts.items())],
    )

    write_export(outdir / "album_runs.csv", album_runs)
    write_export(outdir / "album_completion.csv", album_completion)
    write_export(outdir / "album_resurrections.csv", album_res)
    write_export(outdir / "album_transitions.csv", album_trans)
    write_export(outdir / "weekly_album_dominance.csv", weekly_album)
    write_export(outdir / "album_eras.csv", album_eras)
    write_export(outdir / "artist_eras.csv", artist_eras)
    write_export(outdir / "historical_vs_recent_albums.csv", historical_albums)
    write_export(outdir / "historical_vs_recent_artists.csv", historical_artists)
    write_export(outdir / "artist_depth.csv", depth)

    lines: list[str] = []
    add = lines.append
    add("NAVIDROME — RADIOGRAFÍA DE ESCUCHA")
    add("=" * 72)
    add(f"Fuente temporal: {source.table}.{source.time_col}")
    add(f"Periodo: {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M}")
    add(f"Reproducciones registradas: {len(plays):,}".replace(",", "."))
    add(f"Canciones únicas escuchadas: {unique_tracks:,}".replace(",", "."))
    add(
        f"Reescuchas: {repeat_plays:,} ({pct(repeat_plays, len(plays))})".replace(
            ",", "."
        )
    )
    add(f"Tiempo musical teórico: {fmt_duration(total_duration)}")
    add(
        f"Sesiones estimadas (corte {session_gap} min): {len(sessions):,}".replace(
            ",", "."
        )
    )
    if sessions:
        med = statistics.median(s["tracks"] for s in sessions)
        add(f"Mediana de canciones por sesión: {med:g}")
    meaningful_album_runs = [r for r in album_runs if r["meaningful"]]
    full_album_runs = [r for r in meaningful_album_runs if r["full_pass"]]
    add(
        f"Pasadas de álbum detectadas: {len(meaningful_album_runs):,}".replace(",", ".")
    )
    add(
        f"Escuchas completas probables: {len(full_album_runs):,} "
        f"({pct(len(full_album_runs), len(meaningful_album_runs))})".replace(",", ".")
    )
    add(f"Umbral de álbum casi/completo: {album_complete * 100:.0f}%")
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
            f"{r['probable_full_plays']:4d} completas | {r['album_runs']:3d} pasadas | "
            f"{r['avg_completion_pct']:5.1f}% media | {r['artist']} — {r['album']}"
        )
        shown += 1
        if shown >= top:
            break
    if not shown:
        add("(ningún álbum supera todavía los criterios de escucha completa)")
    add("")

    add(f"TOP {top} ÁLBUMES POR NÚMERO DE PASADAS")
    add("-" * 72)
    for r in sorted(
        album_completion,
        key=lambda x: (x["album_runs"], x["avg_completion_pct"]),
        reverse=True,
    )[:top]:
        add(
            f"{r['album_runs']:4d} pasadas | {r['probable_full_plays']:3d} completas | "
            f"{r['median_completion_pct']:5.1f}% mediana | {r['artist']} — {r['album']}"
        )
    if not album_completion:
        add("(sin pasadas de álbum significativas)")
    add("")

    add(f"ÁLBUMES QUE MÁS EMPIEZAS Y DEJAS A MEDIAS — primeras {top}")
    add("-" * 72)
    aband = [r for r in album_completion if r["probable_abandons"] > 0]
    aband.sort(
        key=lambda r: (r["probable_abandons"], r["started_from_beginning"]),
        reverse=True,
    )
    for r in aband[:top]:
        stop = (
            f" | corte típico: {r['favorite_stop_track']}"
            if r["favorite_stop_track"]
            else ""
        )
        add(
            f"{r['probable_abandons']:3d} abandonos / {r['started_from_beginning']:3d} inicios | "
            f"{r['artist']} — {r['album']}{stop}"
        )
    if not aband:
        add("(ninguno con el criterio actual)")
    add("")

    add(f"TRANSICIONES ENTRE ÁLBUMES — primeras {top}")
    add("-" * 72)
    repeated_album_trans = [r for r in album_trans if r["times"] >= 2]
    for r in repeated_album_trans[:top]:
        add(
            f"{r['times']:3d}x  {r['from_artist']} — {r['from_album']}\n"
            f"      → {r['to_artist']} — {r['to_album']}"
        )
    if not repeated_album_trans:
        add("(no hay transiciones entre discos repetidas todavía)")
    add("")

    add(f"RESURRECCIONES DE ÁLBUM tras ≥ {resurrection_days} días — primeras {top}")
    add("-" * 72)
    for r in album_res[:top]:
        add(
            f"{int(r['silent_days']):5d} días | {r['artist']} — {r['album']}\n"
            f"            {r['before'][:10]} → {r['return'][:10]}"
        )
    if not album_res:
        add("(ninguna en el periodo detallado)")
    add("")

    add(f"ERAS DE ARTISTA — primeras {top}")
    add("-" * 72)
    interesting_artist_eras = [r for r in artist_eras if r["weeks"] >= 2]
    for r in interesting_artist_eras[:top]:
        add(
            f"{r['start_week']} → {r['end_week']} | {r['weeks']:2d} semanas | "
            f"{r['avg_share_pct']:5.1f}% medio | {r['artist']}"
        )
    if not interesting_artist_eras:
        add("(ningún artista domina dos semanas consecutivas en el periodo)")
    add("")

    add(f"ERAS DE ÁLBUM — primeras {top}")
    add("-" * 72)
    interesting_album_eras = [r for r in album_eras if r["weeks"] >= 2]
    for r in interesting_album_eras[:top]:
        add(
            f"{r['start_week']} → {r['end_week']} | {r['weeks']:2d} semanas | "
            f"{r['avg_share_pct']:5.1f}% medio | {r['artist']} — {r['album']}"
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
                f"{r['historical_total']:5d} total | {r['recent_scrobbles']:4d} recientes | "
                f"{str(share).replace('.', ','):>5}% | {r['profile']:10s} | "
                f"{r['artist']} — {r['album']}"
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
            add(
                f"{h:02d}:00  {n:7d}  {'█' * max(1, round(30 * n / max(hour_counts.values())))}"
            )
    add("")

    add(f"OBSESIONES SEMANALES — primeras {top}")
    add("-" * 72)
    for r in sorted(weekly, key=lambda x: x["plays"], reverse=True)[:top]:
        add(
            f"{r['year']}-W{int(r['week']):02d}  {r['plays']:3d}x "
            f"({str(r['share']).replace('.', ',')}%)  {r['artist']} — {r['title']}"
        )
    if not weekly:
        add("(sin casos con el umbral actual)")
    add("")

    add(f"RESURRECCIONES tras ≥ {resurrection_days} días — primeras {top}")
    add("-" * 72)
    for r in res[:top]:
        add(
            f"{int(r['silent_days']):5d} días  {r['artist']} — {r['title']}\n"
            f"            {r['before'][:10]} → {r['return'][:10]}"
        )
    if not res:
        add("(ninguna)")
    add("")

    add(f"PAREJAS DE PISTAS (secundario) dentro de {pair_window} min — primeras {top}")
    add("-" * 72)
    for r in pairs[:top]:
        add(
            f"{r['times']:4d}x  {r['from_artist']} — {r['from_title']}\n"
            f"       → {r['to_artist']} — {r['to_title']}"
        )
    if not pairs:
        add("(ninguna repetida)")
    add("")

    add(f"SESIONES MÁS INTENSAS — primeras {top}")
    add("-" * 72)
    for s in sorted(sessions, key=lambda x: x["tracks"], reverse=True)[:top]:
        add(
            f"{s['tracks']:4d} pistas | {s['span_minutes']:7.1f} min | "
            f"{s['start'][:16]} | {s['top_artist']}"
        )
    add("")

    add("DESCUBRIMIENTOS / FIJACIONES DURADERAS")
    add("-" * 72)
    for r in disc[:top]:
        add(
            f"{r['lifespan_days']:8.0f} días | {r['plays']:4d}x | "
            f"{r['artist']} — {r['title']}"
        )
    if not disc:
        add("(sin suficientes repeticiones)")
    add("")

    if jumps:
        add("SALTOS DE CONTEXTO MÁS BRUSCOS")
        add("-" * 72)
        for r in jumps[:top]:
            add(
                f"{r['score']:5.1f}/100 [{r['confidence']}] "
                f"{r['from_artist']} — {r['from_title']}\n"
                f"              → {r['to_artist']} — {r['to_title']}"
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
    rows: list[dict[str, Any]],
    top: int,
    outdir: Path,
) -> str:
    write_export(outdir / "legacy_playcounts.csv", rows)

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
        lines.append(f"{r['play_count']:7d}  {r['artist']} — {r['title']}")

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
            f"Canciones escuchadas en el historial disponible: {len(heard):,}".replace(
                ",", "."
            ),
            f"Nunca vistas en ese historial: {unplayed:,} ({pct(unplayed, total_tracks)})".replace(
                ",", "."
            ),
        ]

    if genres:
        lines += ["", "Géneros más presentes en la biblioteca:"]
        for g, n in genres.most_common(20):
            lines.append(f"{n:7d}  {g}")

    return "\n".join(lines) + "\n"
