from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from db import (
    Schema,
    detect_history_source,
    load_annotation_playcounts,
    load_legacy_playcounts,
    load_plays,
    load_tracks,
    load_users,
    resolve_user_ids,
)
from report import history_report_v4, legacy_annotations, library_report


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Genera una radiografía integral de escucha desde navidrome.db."
    )
    ap.add_argument("database", type=Path, help="Ruta a navidrome.db")
    ap.add_argument("-o", "--output", type=Path, default=Path("navidrome_report"))
    ap.add_argument("--user", help="Nombre o ID de usuario; por defecto analiza todos.")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument(
        "--session-gap",
        type=int,
        default=30,
        help="Minutos de inactividad para cortar sesión (default: 30).",
    )
    ap.add_argument(
        "--pair-window",
        type=int,
        default=30,
        help="Máximo de minutos entre dos pistas asociadas (default: 30).",
    )
    ap.add_argument(
        "--resurrection-days",
        type=int,
        default=180,
        help="Silencio mínimo para considerar resurrección (default: 180 días).",
    )
    ap.add_argument(
        "--album-complete",
        type=float,
        default=0.80,
        help="Fracción de pistas para considerar un álbum casi/completo (default: 0.80).",
    )
    ap.add_argument(
        "--album-thread-hours",
        type=float,
        default=48.0,
        help="Horas máximas para retomar el hilo lógico de un álbum (default: 48).",
    )
    ap.add_argument(
        "--resume-slack",
        type=int,
        default=2,
        help="Pistas de holgura al reconocer una continuación (default: 2).",
    )
    ap.add_argument(
        "--inspect",
        action="store_true",
        help="Además guarda el esquema SQLite completo en schema.txt.",
    )
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
            (args.output / "schema.txt").write_text(
                schema.dump() + "\n", encoding="utf-8"
            )

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
            try:
                legacy_rows = load_legacy_playcounts(
                    db, schema, tracks, users, selected
                )
            except RuntimeError as error:
                report = str(error)
            else:
                report = legacy_annotations(legacy_rows, max(1, args.top), args.output)
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
